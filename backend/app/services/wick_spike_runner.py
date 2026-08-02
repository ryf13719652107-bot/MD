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
    extract_wallet_balance,
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
    mark_bar_triggered,
    on_tick,
    release_bar_trigger,
)
from .strategy_engine import Signal

logger = logging.getLogger(__name__)

_SYMBOL_REFRESH_SEC = 15.0
_POLL_IDLE_SEC = 0.02
_KLINE_MIN_BARS = 80


class WickSpikeRunner:
    def __init__(self):
        self._tasks: dict[int, asyncio.Task] = {}
        self._position_mgr = PositionManager()

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
        await price_stream_manager.shutdown()

    async def _run_strategy(self, strategy_id: int) -> None:
        strategy_log_service.info(strategy_id, "毫秒接针价流循环已启动")
        states: dict[str, WickSymbolState] = {}
        last_seq: dict[str, int] = {}
        symbols: list[str] = []
        next_refresh = 0.0
        total_margin = 0.0
        tick_ctx = TickContext()
        auth = None
        public = None
        leverage = 10.0

        try:
            while True:
                now = time.time()
                if now >= next_refresh:
                    packed = await self._refresh_context(strategy_id)
                    if packed is None:
                        await asyncio.sleep(2.0)
                        continue
                    (
                        strategy,
                        symbols,
                        tick_ctx,
                        total_margin,
                        auth,
                        public,
                        leverage,
                    ) = packed
                    await price_stream_manager.set_wanted(public, set(symbols))
                    # Pre-warm klines for ATR/volume
                    for sym in symbols:
                        try:
                            await kline_stream_manager.get(
                                public, sym, strategy.timeframe, _KLINE_MIN_BARS
                            )
                        except Exception:
                            pass
                    next_refresh = now + _SYMBOL_REFRESH_SEC

                if not symbols or auth is None or public is None:
                    await asyncio.sleep(_POLL_IDLE_SEC)
                    continue

                # Reload strategy fields each cycle (keep attrs after session closes)
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
                    # Detach a filter stub with only fields used by entry filters
                    filter_strategy = strategy
                    session.expunge(filter_strategy)

                any_update = False
                for sym in symbols:
                    sym_key = _norm_sym(sym)
                    got = price_stream_manager.get(sym)
                    if not got:
                        continue
                    price, _ts = got
                    seq = price_stream_manager.seq(sym_key)
                    if last_seq.get(sym_key) == seq:
                        continue
                    last_seq[sym_key] = seq
                    any_update = True

                    if not self._position_mgr._passes_new_entry_filters(
                        sym, filter_strategy, tick_ctx
                    ):
                        continue

                    # Skip if already have open leg this direction
                    side = "long" if direction == "long" else "short"
                    if tick_ctx.exchange_legs.get((sym_key, side), 0) > 0:
                        continue

                    try:
                        klines = await kline_stream_manager.get(
                            public, sym, timeframe, _KLINE_MIN_BARS
                        )
                    except Exception as e:
                        logger.debug("wick_spike klines %s: %s", sym, e)
                        continue
                    snap = build_bar_snapshot(
                        klines,
                        atr_period=atr_period,
                        volume_sma_period=vol_period,
                    )
                    if snap is None:
                        continue

                    st = states.setdefault(sym_key, WickSymbolState())
                    now_ms = int(time.time() * 1000)
                    signal = on_tick(st, params, snap, price, now_ms)
                    if signal is None:
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
            total_margin = float(extract_wallet_balance(bal, "binance") or 0)
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
    ) -> str:
        """Returns: opened | busy | has_pos | retryable_fail | committed_fail"""
        from .scheduler import strategy_scheduler

        lock = strategy_scheduler._get_strategy_lock(strategy_id)
        if lock.locked():
            # 不长时间死等 tick（管理池子可能数秒），避免接到过时飞刀；调用方会 release 后重试
            strategy_log_service.info(
                strategy_id,
                f"{symbol} 接针触发但 :00/:30 调度占锁，稍后重试",
            )
            return "busy"

        vol_ratio = (snap.vol_now / snap.vol_sma) if snap.vol_sma > 0 else 0.0
        n = snap.atr * params.atr_mult
        strategy_log_service.info(
            strategy_id,
            f"{symbol} 毫秒接针触发 → {signal.value} "
            f"价={price:.6g} open={snap.bar_open:.6g} N={n:.6g} "
            f"ATR={snap.atr:.6g} vol×={vol_ratio:.1f}",
        )

        async with lock:
            async with async_session() as session:
                strategy = await session.get(Strategy, strategy_id)
                if not strategy or strategy.status != "running":
                    return "retryable_fail"
                # Re-check no open position in DB
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
                    # 未成交（黑名单/杠杆/交易所拒单）— 同根可再试
                    return "retryable_fail"
                try:
                    await self._position_mgr.execute_open_db(session, strategy, api_res)
                    await session.commit()
                    # Update in-memory legs so we don't double-fire before next refresh
                    side = signal.value
                    tick_ctx.exchange_legs[(sym_key, side)] = (
                        tick_ctx.exchange_legs.get((sym_key, side), 0)
                        + float(api_res.filled_qty or 0)
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
        return "retryable_fail"


wick_spike_runner = WickSpikeRunner()
