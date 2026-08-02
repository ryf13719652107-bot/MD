"""Per-strategy millisecond wick-spike open loop (Binance only).

独立于收盘 tick：仅 signal_source=wick_spike 时启动。开仓复用 PositionManager
execute_open_api / execute_open_db，并与策略锁 / 账户下单 Semaphore 协调。
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Optional

from sqlalchemy import select

from ..database import async_session
from ..models.account import Account
from ..models.position import Position
from ..models.strategy import Strategy
from ..models.strategy_blacklist import StrategySymbolBlacklist
from ..config import now_beijing
from .binance_service import (
    get_strategy_pool_exclude_symbols,
    filter_pool_symbols_by_funding,
    get_cached_last_funding_rates_pct,
)
from .exchange_factory import (
    account_exchange_id,
    extract_margin_balance,
    get_exchange_for_account,
    get_public_exchange,
)
from .strategy_flags import (
    exclude_delisting_enabled,
    exclude_mainstream_enabled,
    exclude_funding_enabled,
    normalize_coin_pool_source,
)
from .coin_pool_service import coin_pool_service
from .log_service import strategy_log_service
from .kline_stream import kline_stream_manager
from .price_stream import price_stream_manager
from .position_manager import PositionManager, _norm_sym
from .tick_context import SignalCandidate, TickContext, exchange_legs_from_positions
from .account_concurrency import account_order_sem
from .wick_spike_engine import (
    WickSpikeParams,
    WickSymbolState,
    build_bar_snapshot,
    enrich_snap_with_trades,
    mark_bar_triggered,
    near_miss_diag,
    on_tick,
    release_bar_trigger,
)
from .strategy_engine import Signal

logger = logging.getLogger(__name__)

_SYMBOL_REFRESH_SEC = 15.0
_POLL_IDLE_SEC = 0.005
_KLINE_MIN_BARS = 80
# 策略参数/DB 重载间隔（热路径不每圈查库）
_STRATEGY_RELOAD_SEC = 2.0
# 近阈值诊断写入 bot.log 的节流（秒）；不进前端策略日志
_NEAR_MISS_LOG_SEC = 8.0
# 缓冲不足时后台 REST 纠偏节流
_BG_REST_SEC = 5.0
# 抢策略锁最长等待（毫秒级 TP 检测），超时则 release 重试
_LOCK_WAIT_SEC = 0.12


class WickSpikeRunner:
    def __init__(self):
        self._tasks: dict[int, asyncio.Task] = {}
        self._bg_tasks: set[asyncio.Task] = set()
        self._position_mgr = PositionManager()

    def _fire_bg(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def is_running(self, strategy_id: int) -> bool:
        t = self._tasks.get(strategy_id)
        return t is not None and not t.done()

    async def start(self, strategy_id: int) -> None:
        if self.is_running(strategy_id):
            return
        task = asyncio.create_task(
            self._run_strategy(strategy_id),
            name=f"wick_spike:{strategy_id}",
        )
        self._tasks[strategy_id] = task

        def _done(t: asyncio.Task, sid: int = strategy_id):
            self._tasks.pop(sid, None)
            try:
                exc = t.exception()
            except asyncio.CancelledError:
                return
            if exc:
                logger.error("wick_spike runner %d crashed: %s", sid, exc)

        task.add_done_callback(_done)

    async def stop(self, strategy_id: int) -> None:
        task = self._tasks.pop(strategy_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def shutdown(self) -> None:
        ids = list(self._tasks.keys())
        for sid in ids:
            await self.stop(sid)
        bg = list(self._bg_tasks)
        self._bg_tasks.clear()
        for t in bg:
            t.cancel()
        for t in bg:
            try:
                await t
            except asyncio.CancelledError:
                pass
        await price_stream_manager.shutdown()

    async def _run_strategy(self, strategy_id: int) -> None:
        strategy_log_service.info(strategy_id, "毫秒接针价流循环已启动")
        states: dict[str, WickSymbolState] = {}
        last_seq: dict[str, int] = {}
        last_kline_fp: dict[str, tuple] = {}
        last_bg_rest: dict[str, float] = {}
        last_near_miss_log: dict[str, float] = {}
        symbols: list[str] = []
        next_refresh = 0.0
        next_strategy_reload = 0.0
        total_margin = 0.0
        tick_ctx = TickContext()
        auth = None
        public = None
        leverage = 10.0
        timeframe = "1m"
        params = WickSpikeParams(direction="short")
        atr_period = 14
        vol_period = 20
        direction = "short"
        filter_strategy = None
        # 后台刷新结果槽：热循环只消费，不 await 刷池/预热
        refresh_slot: dict = {"task": None, "packed": None, "failed": False}

        async def _bg_refresh() -> None:
            try:
                packed = await self._refresh_context(strategy_id)
                if packed is None:
                    refresh_slot["failed"] = True
                else:
                    refresh_slot["packed"] = packed
                    refresh_slot["failed"] = False
            except Exception as e:
                logger.warning("wick_spike bg refresh %d: %s", strategy_id, e)
                refresh_slot["failed"] = True
            finally:
                refresh_slot["task"] = None

        async def _bg_prewarm_klines(pub, syms: list[str], tf: str) -> None:
            for sym in syms:
                try:
                    await kline_stream_manager.get(pub, sym, tf, _KLINE_MIN_BARS)
                except Exception:
                    pass
                await asyncio.sleep(0)

        async def _bg_rest_one(pub, sym: str, tf: str) -> None:
            try:
                await kline_stream_manager.refresh_rest(pub, sym, tf, _KLINE_MIN_BARS)
            except Exception:
                pass

        try:
            while True:
                now = time.time()
                # 应用已完成的后台刷新（不阻塞）
                packed = refresh_slot.get("packed")
                if packed is not None:
                    refresh_slot["packed"] = None
                    (
                        _strategy_stub,
                        symbols,
                        tick_ctx,
                        total_margin,
                        auth,
                        public,
                        leverage,
                    ) = packed
                    timeframe = getattr(_strategy_stub, "timeframe", timeframe) or timeframe
                    await price_stream_manager.set_wanted(
                        public, set(symbols), timeframe=timeframe
                    )
                    self._fire_bg(_bg_prewarm_klines(public, list(symbols), timeframe))

                if now >= next_refresh:
                    next_refresh = now + _SYMBOL_REFRESH_SEC
                    t = refresh_slot.get("task")
                    if t is None or t.done():
                        refresh_slot["task"] = asyncio.create_task(_bg_refresh())
                        self._bg_tasks.add(refresh_slot["task"])
                        refresh_slot["task"].add_done_callback(self._bg_tasks.discard)

                if not symbols or auth is None or public is None:
                    if refresh_slot.get("failed") and (refresh_slot.get("task") is None):
                        await asyncio.sleep(2.0)
                        refresh_slot["failed"] = False
                        next_refresh = 0.0
                    else:
                        await asyncio.sleep(_POLL_IDLE_SEC)
                    continue

                # 策略参数低频重载，避免每圈打 DB
                if filter_strategy is None or now >= next_strategy_reload:
                    next_strategy_reload = now + _STRATEGY_RELOAD_SEC
                    async with async_session() as session:
                        strategy = await session.get(Strategy, strategy_id)
                        if not strategy or strategy.status != "running":
                            break
                        if strategy.signal_source != "wick_spike":
                            break
                        params = WickSpikeParams(
                            direction=strategy.direction,
                            volume_mult=float(getattr(strategy, "wick_volume_mult", 8.0) or 0),
                            atr_mult=float(getattr(strategy, "wick_spike_atr_mult", 5.0) or 5.0),
                            cooldown_sec=float(getattr(strategy, "wick_cooldown_sec", 0) or 0),
                        )
                        atr_period = int(getattr(strategy, "wick_atr_period", 14) or 14)
                        vol_period = int(getattr(strategy, "wick_volume_sma_period", 20) or 20)
                        timeframe = strategy.timeframe
                        direction = strategy.direction
                        filter_strategy = strategy
                        session.expunge(filter_strategy)

                # 优先处理刚有成交的币，降低池内扫尾延迟
                hot: list[str] = []
                cold: list[str] = []
                for sym in symbols:
                    sym_key = _norm_sym(sym)
                    seq = price_stream_manager.seq(sym_key)
                    if last_seq.get(sym_key) != seq:
                        hot.append(sym)
                    else:
                        cold.append(sym)
                scan_order = hot + cold

                any_update = False
                for sym in scan_order:
                    sym_key = _norm_sym(sym)
                    got = price_stream_manager.get(sym)
                    price = float(got[0]) if got else 0.0
                    trade_ts_ms = int(got[1]) if got else 0
                    seq = price_stream_manager.seq(sym_key)
                    price_changed = last_seq.get(sym_key) != seq
                    if price_changed:
                        last_seq[sym_key] = seq

                    if not self._position_mgr._passes_new_entry_filters(
                        sym, filter_strategy, tick_ctx
                    ):
                        continue

                    side = "long" if direction == "long" else "short"
                    if tick_ctx.exchange_legs.get((sym_key, side), 0) > 0:
                        continue

                    # 热路径：只读 WS 内存，绝不 await REST
                    klines = kline_stream_manager.peek(public, sym, timeframe)
                    if len(klines) < atr_period + 2:
                        if now - last_bg_rest.get(sym_key, 0.0) >= _BG_REST_SEC:
                            last_bg_rest[sym_key] = now
                            self._fire_bg(_bg_rest_one(public, sym, timeframe))
                        continue

                    last = klines[-1]
                    try:
                        fp = (
                            int(last[0]),
                            float(last[2]),
                            float(last[3]),
                            float(last[4]),
                            float(last[5]),
                        )
                    except (TypeError, ValueError, IndexError):
                        continue
                    kline_changed = last_kline_fp.get(sym_key) != fp
                    if not price_changed and not kline_changed:
                        continue
                    last_kline_fp[sym_key] = fp
                    any_update = True

                    if price <= 0:
                        try:
                            price = float(last[4])
                        except (TypeError, ValueError, IndexError):
                            continue
                    if price <= 0:
                        continue

                    snap = build_bar_snapshot(
                        klines,
                        atr_period=atr_period,
                        volume_sma_period=vol_period,
                    )
                    if snap is None:
                        continue
                    # 成交流量/高低补强（K 线 WS 量常滞后数秒）
                    snap = enrich_snap_with_trades(
                        snap,
                        trade_vol=price_stream_manager.bar_volume(sym_key),
                        trade_high=price_stream_manager.bar_high(sym_key),
                        trade_low=price_stream_manager.bar_low(sym_key),
                    )

                    st = states.setdefault(sym_key, WickSymbolState())
                    now_ms = int(time.time() * 1000)
                    t_signal0 = time.perf_counter()
                    signal = on_tick(st, params, snap, price, now_ms)
                    if signal is None:
                        if now - last_near_miss_log.get(sym_key, 0.0) >= _NEAR_MISS_LOG_SEC:
                            diag = near_miss_diag(params, snap, st, price)
                            if diag:
                                last_near_miss_log[sym_key] = now
                                logger.info(
                                    "wick_spike near-miss strategy=%d %s %s",
                                    strategy_id,
                                    sym_key,
                                    diag,
                                )
                        continue

                    outcome = await self._try_open(
                        strategy_id=strategy_id,
                        symbol=sym,
                        signal=signal,
                        price=price,
                        snap=snap,
                        params=params,
                        auth=auth,
                        public=public,
                        total_margin=total_margin,
                        leverage=leverage,
                        tick_ctx=tick_ctx,
                        trade_ts_ms=trade_ts_ms,
                        signal_detect_perf=t_signal0,
                    )
                    if outcome == "opened":
                        mark_bar_triggered(st, params, snap.bar_open_ts, now_ms)
                        next_refresh = min(next_refresh, time.time() + 1.0)
                    elif outcome == "busy":
                        # tick 占锁：回滚触发标记，价仍在极值下时可在后续成交里重试
                        release_bar_trigger(st)
                    elif outcome == "retryable_fail":
                        release_bar_trigger(st)
                    # has_pos / committed_fail：保持本根已触发，避免重复市价单

                if not any_update:
                    await asyncio.sleep(_POLL_IDLE_SEC)
                else:
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            strategy_log_service.info(strategy_id, "毫秒接针价流循环已停止")
            raise
        except Exception as e:
            logger.exception("wick_spike runner %d error: %s", strategy_id, e)
            strategy_log_service.error(strategy_id, f"毫秒接针循环异常 — {e}")
            raise

    async def _refresh_context(self, strategy_id: int):
        from .scheduler import strategy_scheduler

        async with async_session() as session:
            strategy = await session.get(Strategy, strategy_id)
            if not strategy or strategy.status != "running":
                return None
            if strategy.signal_source != "wick_spike":
                return None
            account = await session.get(Account, strategy.account_id)
            if not account:
                return None
            exchange = account_exchange_id(account)
            if exchange != "binance":
                strategy_log_service.error(
                    strategy_id,
                    "毫秒接针仅支持币安账户，价流循环退出",
                )
                return None

            bl_rows = (
                await session.execute(
                    select(StrategySymbolBlacklist.symbol_norm).where(
                        StrategySymbolBlacklist.strategy_id == strategy_id
                    )
                )
            ).scalars().all()
            blacklist = frozenset((s or "").upper() for s in bl_rows if s)

            open_rows = list(
                (
                    await session.execute(
                        select(Position).where(
                            Position.strategy_id == strategy_id,
                            Position.closed_at.is_(None),
                        )
                    )
                ).scalars().all()
            )
            open_syms = [_norm_sym(p.symbol) for p in open_rows if p.symbol]

            # Build exchange clients while account is still attached
            public = await get_public_exchange("binance")
            auth = await get_exchange_for_account(account)
            session.expunge(strategy)

        try:
            bal = await auth.fetch_balance()
            total_margin = float(extract_margin_balance(auth, bal) or 0)
        except Exception as e:
            logger.warning("wick_spike balance %d: %s", strategy_id, e)
            total_margin = 0.0
        wallet_ok = math.isfinite(total_margin) and total_margin > 0

        try:
            raw_pos = await auth.fetch_positions()
        except Exception as e:
            logger.warning("wick_spike positions %d: %s", strategy_id, e)
            raw_pos = []
        exchange_legs = exchange_legs_from_positions(raw_pos or [])

        pool_symbols: list[str] = []
        pool_entry_norms: frozenset[str] | None = None
        exclude_norm: frozenset[str] = blacklist

        if strategy.use_coin_pool:
            try:
                # scheduled 到期只投递后台刷池，不在此 await 完整刷新
                await coin_pool_service.ensure_scheduled_pool_if_due(
                    public, strategy, exchange="binance"
                )
                min_vol = float(getattr(strategy, "coin_pool_min_volume_24h", 0) or 0)
                excluded = await get_strategy_pool_exclude_symbols(
                    public,
                    exclude_tradefi=bool(strategy.exclude_tradefi),
                    exclude_delisting=exclude_delisting_enabled(strategy),
                    exclude_mainstream=exclude_mainstream_enabled(strategy),
                )
                merged = set(excluded) if excluded else set()
                merged.update(blacklist)
                exclude_norm = frozenset(merged)
                pool_symbols = await coin_pool_service.get_pool_symbols(
                    normalize_coin_pool_source(strategy.coin_pool_source),
                    strategy.coin_pool_top_n,
                    min_volume_24h=min_vol,
                    exclude_symbols_norm=set(exclude_norm) if exclude_norm else None,
                    strategy=strategy,
                    exchange="binance",
                )
                if exclude_funding_enabled(strategy):
                    pool_symbols = await filter_pool_symbols_by_funding(
                        public,
                        pool_symbols,
                        direction=strategy.direction,
                        threshold_pct=float(getattr(strategy, "funding_rate_threshold_pct", 0) or 0),
                    )
                pool_entry_norms = frozenset(_norm_sym(s) for s in pool_symbols if s)
            except Exception as e:
                logger.warning("wick_spike pool %d: %s", strategy_id, e)
                pool_symbols = []
        elif strategy.symbol:
            pool_symbols = [strategy.symbol]
            pool_entry_norms = frozenset({_norm_sym(strategy.symbol)})

        # Watch pool + open symbols
        watch = list(dict.fromkeys([*pool_symbols, *[s for s in open_syms if s]]))
        # Only open new on pool entries (or fixed symbol)
        allow_new = pool_entry_norms

        funding_rates = None
        funding_on = False
        if strategy.use_coin_pool and exclude_funding_enabled(strategy):
            funding_on = True
            try:
                funding_rates = await get_cached_last_funding_rates_pct(public)
            except Exception:
                funding_rates = {}

        tick_ctx = TickContext(
            exclude_norm=exclude_norm,
            funding_rates=funding_rates,
            funding_filter_enabled=funding_on,
            exchange_legs=exchange_legs,
            raw_exchange_positions=raw_pos or [],
            allow_new_norms=allow_new,
            wallet_balance_valid=wallet_ok,
        )
        leverage = float(strategy.leverage or 10)
        # Keep auth client in scheduler cache path if present
        if strategy.account_id not in strategy_scheduler._exchange_services:
            strategy_scheduler._exchange_services[strategy.account_id] = auth
        return strategy, watch, tick_ctx, total_margin, auth, public, leverage

    async def _try_open(
        self,
        *,
        strategy_id: int,
        symbol: str,
        signal: Signal,
        price: float,
        snap,
        params: WickSpikeParams,
        auth,
        public,
        total_margin: float,
        leverage: float,
        tick_ctx: TickContext,
        trade_ts_ms: int = 0,
        signal_detect_perf: float = 0.0,
    ) -> str:
        """Returns: opened | busy | has_pos | retryable_fail | committed_fail"""
        from .scheduler import strategy_scheduler

        lock = strategy_scheduler._get_strategy_lock(strategy_id)
        # 短等锁：躲过 TP 检测的短暂占用；manage 长占则超时重试
        try:
            await asyncio.wait_for(lock.acquire(), timeout=_LOCK_WAIT_SEC)
        except asyncio.TimeoutError:
            strategy_log_service.info(
                strategy_id,
                f"{symbol} 接针触发但调度占锁，稍后重试",
            )
            return "busy"

        vol_ratio = (snap.vol_now / snap.vol_sma) if snap.vol_sma > 0 else 0.0
        n = snap.atr * params.atr_mult
        trade_age_ms = (
            max(0, int(time.time() * 1000) - int(trade_ts_ms)) if trade_ts_ms > 0 else -1
        )
        detect_ms = (
            (time.perf_counter() - signal_detect_perf) * 1000.0
            if signal_detect_perf > 0
            else -1.0
        )
        strategy_log_service.info(
            strategy_id,
            f"{symbol} 毫秒接针触发 → {signal.value} "
            f"价={price:.6g} open={snap.bar_open:.6g} N={n:.6g} "
            f"ATR={snap.atr:.6g} vol×={vol_ratio:.1f}",
        )
        logger.info(
            "wick_spike trigger strategy=%d %s %s px=%.6g vol×=%.2f "
            "trade_age_ms=%d detect_to_lock_ms=%.1f",
            strategy_id,
            symbol,
            signal.value,
            price,
            vol_ratio,
            trade_age_ms,
            detect_ms,
        )

        t_open0 = time.perf_counter()
        try:
            async with async_session() as session:
                strategy = await session.get(Strategy, strategy_id)
                if not strategy or strategy.status != "running":
                    return "retryable_fail"
                sym_key = _norm_sym(symbol)
                existing = list(
                    (
                        await session.execute(
                            select(Position).where(
                                Position.strategy_id == strategy_id,
                                Position.closed_at.is_(None),
                            )
                        )
                    ).scalars().all()
                )
                if any(_norm_sym(p.symbol) == sym_key for p in existing):
                    return "has_pos"

                base_qty = self._position_mgr._compute_base_qty(strategy, total_margin, price)
                if base_qty is None:
                    strategy_log_service.warning(
                        strategy_id, f"{symbol} 接针无法开仓 — 余额无效"
                    )
                    return "retryable_fail"

                strategy.last_signal = signal.value
                strategy.last_signal_at = now_beijing()
                strategy.last_rsi = round(vol_ratio, 2)

                candidate = SignalCandidate(
                    symbol=symbol,
                    signal=signal,
                    klines=[],
                    current_price=price,
                    rsi=float(vol_ratio),
                    signal_label="毫秒接针",
                    base_qty=base_qty,
                )

                order_sem = account_order_sem(strategy.account_id)
                async with order_sem:
                    api_res = await self._position_mgr.execute_open_api(
                        candidate, strategy, auth, leverage
                    )
                if api_res is None:
                    await session.rollback()
                    return "retryable_fail"
                try:
                    await self._position_mgr.execute_open_db(session, strategy, api_res)
                    await session.commit()
                    side = signal.value
                    tick_ctx.exchange_legs[(sym_key, side)] = (
                        tick_ctx.exchange_legs.get((sym_key, side), 0)
                        + float(api_res.filled_qty or 0)
                    )
                    open_ms = (time.perf_counter() - t_open0) * 1000.0
                    logger.info(
                        "wick_spike opened strategy=%d %s open_api_db_ms=%.0f "
                        "trade_age_ms=%d vol×=%.2f",
                        strategy_id,
                        symbol,
                        open_ms,
                        trade_age_ms,
                        vol_ratio,
                    )
                    return "opened"
                except Exception as e:
                    logger.error("wick_spike open db %s: %s", symbol, e)
                    await session.rollback()
                    strategy_log_service.error(
                        strategy_id,
                        f"{symbol} 接针开仓已可能成交但写库失败 — 请手动检查交易所，本根不再重试",
                    )
                    return "committed_fail"
        finally:
            lock.release()


wick_spike_runner = WickSpikeRunner()
