"""Per-strategy millisecond wick-spike open loop (Binance only).

独立于收盘 tick：仅 signal_source=wick_spike 时启动。开仓复用 PositionManager
execute_wick_open_market / place_open_tp_limit / execute_open_db，
并与策略锁 / 账户下单 Semaphore 协调。
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
from .kline_stream import kline_stream_manager, _timeframe_ms
from .price_stream import price_stream_manager
from .account_position_stream import account_position_stream
from .position_manager import PositionManager, _norm_sym, RECONCILE_CREATED
from .tick_context import SignalCandidate, TickContext, exchange_legs_from_positions
from .account_concurrency import account_order_sem
from .leverage_prewarm import prewarm_symbols_leverage
from .wick_spike_engine import (
    WickSpikeParams,
    WickSymbolState,
    build_bar_snapshot,
    enrich_snap_with_trades,
    effective_volume_mult,
    is_arm_active,
    near_miss_diag,
    on_tick,
    pierce_vol_view,
    mark_bar_triggered,
    release_bar_trigger,
    spike_progress,
    snapshot_extreme,
    tip_gap_pct,
)
from .strategy_engine import Signal

logger = logging.getLogger(__name__)

_SYMBOL_REFRESH_SEC = 15.0
_POLL_IDLE_SEC = 0.005
_KLINE_MIN_BARS = 80
# 策略参数/DB 重载间隔（后台 task，热路径只消费）
_STRATEGY_RELOAD_SEC = 2.0
# 近阈值诊断写入 bot.log 的节流（秒）；不进前端策略日志
_NEAR_MISS_LOG_SEC = 8.0
# 武装窗无新成交时强制重判最小间隔（毫秒）
_ARM_FORCE_RETRY_MS = 80
# 无成交时最长等待（武装强制重判 / 刷新）
_WAKE_TIMEOUT_SEC = 0.08
# 缓冲不足时后台 REST 纠偏节流
_BG_REST_SEC = 5.0
# 武装且等量时后台 REST 补强本根 K 线间隔（秒）；解决 WS 量能/极值滞后
_ARM_REST_SEC = 1.0
# 抢策略锁最长等待（毫秒级 TP 检测），超时则 release 重试
_LOCK_WAIT_SEC = 0.12
# 成交后写库重试
_DB_WRITE_RETRIES = 3
_DB_WRITE_RETRY_DELAY_SEC = 0.35


def _wick_params_from_strategy(strategy: Strategy) -> tuple[WickSpikeParams, int, int, str, str]:
    """从 Strategy ORM 抽出接针参数（调用方须已保证字段可读）。"""
    relax_flag = getattr(strategy, "wick_amp_vol_relax_enabled", None)
    params = WickSpikeParams(
        direction=strategy.direction,
        volume_mult=float(getattr(strategy, "wick_volume_mult", 8.0) or 0),
        atr_mult=float(getattr(strategy, "wick_spike_atr_mult", 5.0) or 5.0),
        cooldown_sec=float(getattr(strategy, "wick_cooldown_sec", 0) or 0),
        vol_relax_enabled=True if relax_flag is None else bool(relax_flag),
        vol_relax_progress_start=float(
            getattr(strategy, "wick_vol_relax_progress_start", 1.0) or 1.0
        ),
        vol_relax_progress_full=float(
            getattr(strategy, "wick_vol_relax_progress_full", 1.5) or 1.5
        ),
        vol_relax_mult=float(getattr(strategy, "wick_vol_relax_mult", 5.0) or 5.0),
        min_move_pct=float(
            getattr(strategy, "wick_min_move_pct", 3.0)
            if getattr(strategy, "wick_min_move_pct", None) is not None
            else 3.0
        ),
        max_retrace_pct=float(
            getattr(strategy, "wick_max_retrace_pct", 50.0)
            if getattr(strategy, "wick_max_retrace_pct", None) is not None
            else 50.0
        ),
        arm_wait_sec=float(
            getattr(strategy, "wick_arm_wait_sec", 12.0)
            if getattr(strategy, "wick_arm_wait_sec", None) is not None
            else 12.0
        ),
        arm_retrace_grace_sec=float(
            getattr(strategy, "wick_arm_retrace_grace_sec", 3.0)
            if getattr(strategy, "wick_arm_retrace_grace_sec", None) is not None
            else 3.0
        ),
        arm_grace_max_tip_gap_pct=float(
            getattr(strategy, "wick_arm_grace_max_tip_gap_pct", 2.0)
            if getattr(strategy, "wick_arm_grace_max_tip_gap_pct", None) is not None
            else 2.0
        ),
    )
    atr_period = int(getattr(strategy, "wick_atr_period", 14) or 14)
    vol_period = int(getattr(strategy, "wick_volume_sma_period", 20) or 20)
    timeframe = strategy.timeframe
    direction = strategy.direction
    return params, atr_period, vol_period, timeframe, direction


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
        try:
            await price_stream_manager.clear_wanted(f"wick:{strategy_id}")
        except Exception as e:
            logger.warning("wick_spike clear_wanted %d: %s", strategy_id, e)

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
        last_arm_rest: dict[str, float] = {}
        arm_rest_inflight: set[str] = set()
        last_near_miss_log: dict[str, float] = {}
        last_arm_force_ms: dict[str, int] = {}
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
        # 策略参数重载槽：热路径绝不 await DB
        strategy_slot: dict = {"task": None, "packed": None, "stop": False}

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

        async def _load_strategy_pack() -> Optional[dict]:
            async with async_session() as session:
                strategy = await session.get(Strategy, strategy_id)
                if not strategy or strategy.status != "running":
                    return None
                if strategy.signal_source != "wick_spike":
                    return None
                p, ap, vp, tf, d = _wick_params_from_strategy(strategy)
                session.expunge(strategy)
                return {
                    "params": p,
                    "atr_period": ap,
                    "vol_period": vp,
                    "timeframe": tf,
                    "direction": d,
                    "filter_strategy": strategy,
                }

        async def _bg_strategy_reload() -> None:
            try:
                pack = await _load_strategy_pack()
                if pack is None:
                    strategy_slot["stop"] = True
                else:
                    strategy_slot["packed"] = pack
            except Exception as e:
                logger.warning("wick_spike bg strategy reload %d: %s", strategy_id, e)
            finally:
                strategy_slot["task"] = None

        async def _bg_prewarm_klines(pub, syms: list[str], tf: str) -> None:
            for sym in syms:
                try:
                    await kline_stream_manager.get(pub, sym, tf, _KLINE_MIN_BARS)
                except Exception:
                    pass
                await asyncio.sleep(0)

        async def _bg_prewarm_leverage(auth_client, syms: list[str], lev: float) -> None:
            if auth_client is None or not syms:
                return
            try:
                await prewarm_symbols_leverage(
                    auth_client, list(syms), max(1, int(lev or 10))
                )
            except Exception as e:
                logger.debug("wick_spike leverage prewarm: %s", e)

        async def _bg_rest_one(pub, sym: str, tf: str) -> None:
            try:
                await kline_stream_manager.refresh_rest(pub, sym, tf, _KLINE_MIN_BARS)
            except Exception:
                pass

        async def _bg_arm_rest(pub, sym: str, tf: str, sym_key: str) -> None:
            """武装后轻量 REST 补强本根 K 线（只拉 2 根），覆盖 WS 量能/极值滞后。"""
            try:
                await kline_stream_manager.refresh_forming(pub, sym, tf, limit=2)
            except Exception:
                pass
            finally:
                arm_rest_inflight.discard(sym_key)

        def _apply_strategy_pack(pack: dict) -> None:
            nonlocal params, atr_period, vol_period, timeframe, direction, filter_strategy
            params = pack["params"]
            atr_period = pack["atr_period"]
            vol_period = pack["vol_period"]
            timeframe = pack["timeframe"]
            direction = pack["direction"]
            filter_strategy = pack["filter_strategy"]

        wake_owner = f"wick:{strategy_id}"
        wake_ev = price_stream_manager.subscribe_wake(wake_owner)
        account_id_watching: int | None = None

        try:
            # 启动时同步加载一次策略，之后一律后台重载
            boot = await _load_strategy_pack()
            if boot is None:
                strategy_log_service.info(strategy_id, "毫秒接针：策略未运行或非 wick_spike，退出")
                return
            _apply_strategy_pack(boot)
            next_strategy_reload = time.time() + _STRATEGY_RELOAD_SEC

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
                        public,
                        set(symbols),
                        timeframe=timeframe,
                        owner=wake_owner,
                    )
                    self._fire_bg(_bg_prewarm_klines(public, list(symbols), timeframe))
                    self._fire_bg(_bg_prewarm_leverage(auth, list(symbols), leverage))
                    acc_id = int(getattr(_strategy_stub, "account_id", 0) or 0)
                    if auth is not None and acc_id > 0:
                        if account_id_watching and account_id_watching != acc_id:
                            await account_position_stream.release(
                                account_id_watching, owner=wake_owner
                            )
                        account_id_watching = acc_id
                        await account_position_stream.ensure_watching(
                            acc_id, auth, owner=wake_owner
                        )
                        # 新鲜推送整表覆盖 legs（update 不会删已平仓腿，会假 has_pos）
                        if account_position_stream.is_fresh(acc_id):
                            tick_ctx.exchange_legs = (
                                account_position_stream.get_legs(acc_id) or {}
                            )
                        else:
                            self._fire_bg(
                                account_position_stream.seed_from_rest(acc_id, auth)
                            )

                # 应用后台策略重载（不阻塞）
                spack = strategy_slot.get("packed")
                if spack is not None:
                    strategy_slot["packed"] = None
                    _apply_strategy_pack(spack)
                if strategy_slot.get("stop"):
                    break

                if now >= next_refresh:
                    next_refresh = now + _SYMBOL_REFRESH_SEC
                    t = refresh_slot.get("task")
                    if t is None or t.done():
                        refresh_slot["task"] = asyncio.create_task(_bg_refresh())
                        self._bg_tasks.add(refresh_slot["task"])
                        refresh_slot["task"].add_done_callback(self._bg_tasks.discard)

                if now >= next_strategy_reload:
                    next_strategy_reload = now + _STRATEGY_RELOAD_SEC
                    stask = strategy_slot.get("task")
                    if stask is None or stask.done():
                        strategy_slot["task"] = asyncio.create_task(_bg_strategy_reload())
                        self._bg_tasks.add(strategy_slot["task"])
                        strategy_slot["task"].add_done_callback(self._bg_tasks.discard)

                if not symbols or auth is None or public is None:
                    if refresh_slot.get("failed") and (refresh_slot.get("task") is None):
                        await asyncio.sleep(2.0)
                        refresh_slot["failed"] = False
                        next_refresh = 0.0
                    else:
                        await asyncio.sleep(_POLL_IDLE_SEC)
                    continue

                # 武装窗优先扫描；仍保留 cold（否则 A 武装时 B 仅量变/K 变会被漏扫）
                now_ms_loop = int(time.time() * 1000)
                retry_syms: list[str] = []
                hot: list[str] = []
                cold: list[str] = []
                for sym in symbols:
                    sym_key = _norm_sym(sym)
                    st0 = states.get(sym_key)
                    if (
                        st0 is not None
                        and st0.armed_bar_ts is not None
                        and is_arm_active(st0, params, st0.armed_bar_ts, now_ms_loop)
                    ):
                        retry_syms.append(sym)
                    elif last_seq.get(sym_key) != price_stream_manager.seq(sym_key):
                        hot.append(sym)
                    else:
                        cold.append(sym)
                scan_order = retry_syms + hot + cold

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

                    st = states.setdefault(sym_key, WickSymbolState())
                    now_ms = int(time.time() * 1000)
                    arm_active = (
                        st.armed_bar_ts is not None
                        and is_arm_active(st, params, st.armed_bar_ts, now_ms)
                    )

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
                    # forming 停滞检测：WS 断连时 forming 停在旧根，会导致
                    # snap.bar_open_ts 与 price_stream.bar_open_ms 不一致，
                    # enrich 忽略成交聚合。检测到停滞则后台 REST 纠偏并跳过。
                    try:
                        forming_ts = int(last[0])
                    except (TypeError, ValueError, IndexError):
                        continue
                    tf_ms = _timeframe_ms(timeframe)
                    current_bar_ts = (now_ms // tf_ms) * tf_ms
                    if forming_ts < current_bar_ts:
                        if now - last_bg_rest.get(sym_key, 0.0) >= _BG_REST_SEC:
                            last_bg_rest[sym_key] = now
                            self._fire_bg(_bg_rest_one(public, sym, timeframe))
                        continue

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
                    # 武装窗：有价/K 变化立即重判；否则至少间隔 _ARM_FORCE_RETRY_MS
                    force_retry = False
                    if arm_active:
                        if price_changed or kline_changed:
                            force_retry = True
                        elif (
                            now_ms - int(last_arm_force_ms.get(sym_key, 0))
                            >= _ARM_FORCE_RETRY_MS
                        ):
                            force_retry = True
                    if not price_changed and not kline_changed and not force_retry:
                        continue
                    if force_retry and not price_changed and not kline_changed:
                        last_arm_force_ms[sym_key] = now_ms
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
                    kline_vol_raw = float(snap.vol_now or 0)
                    trade_vol_raw = price_stream_manager.bar_volume(sym_key)
                    # 成交流量/高低补强；bar 未对齐则忽略（防换根串量）
                    snap = enrich_snap_with_trades(
                        snap,
                        trade_vol=trade_vol_raw,
                        trade_high=price_stream_manager.bar_high(sym_key),
                        trade_low=price_stream_manager.bar_low(sym_key),
                        trade_bar_open_ts=price_stream_manager.bar_open_ms(sym_key),
                    )

                    prev_armed_at = st.armed_at_ms
                    t_signal0 = time.perf_counter()
                    signal = on_tick(st, params, snap, price, now_ms)
                    if (
                        st.armed_at_ms
                        and st.armed_at_ms != prev_armed_at
                        and st.armed_bar_ts == snap.bar_open_ts
                    ):
                        logger.info(
                            "wick_spike armed strategy=%d %s await_vol=%s "
                            "ext=%s arm_wait=%.1fs grace=%.1fs "
                            "kline_vol=%s trade_vol=%s vol_now=%s sma=%s",
                            strategy_id,
                            sym_key,
                            st.armed_awaiting_vol,
                            f"{st.armed_extreme:.6g}" if st.armed_extreme else "?",
                            float(params.arm_wait_sec or 0),
                            float(params.arm_retrace_grace_sec or 0),
                            f"{kline_vol_raw:.1f}",
                            f"{trade_vol_raw:.1f}",
                            f"{snap.vol_now:.1f}",
                            f"{snap.vol_sma:.1f}",
                        )
                    if signal is None:
                        # 武装且等量时后台 REST 补强本根 K 线，解决 WS 量能/极值滞后
                        if (
                            st.armed_bar_ts == snap.bar_open_ts
                            and st.armed_awaiting_vol
                            and sym_key not in arm_rest_inflight
                            and now - last_arm_rest.get(sym_key, 0.0) >= _ARM_REST_SEC
                        ):
                            last_arm_rest[sym_key] = now
                            arm_rest_inflight.add(sym_key)
                            self._fire_bg(
                                _bg_arm_rest(public, sym, timeframe, sym_key)
                            )
                        if now - last_near_miss_log.get(sym_key, 0.0) >= _NEAR_MISS_LOG_SEC:
                            diag = near_miss_diag(
                                params, snap, st, price, now_ms=now_ms
                            )
                            if diag:
                                last_near_miss_log[sym_key] = now
                                logger.info(
                                    "wick_spike near-miss strategy=%d %s %s "
                                    "kline_vol=%s trade_vol=%s vol_now=%s sma=%s",
                                    strategy_id,
                                    sym_key,
                                    diag,
                                    f"{kline_vol_raw:.1f}",
                                    f"{trade_vol_raw:.1f}",
                                    f"{snap.vol_now:.1f}",
                                    f"{snap.vol_sma:.1f}",
                                )
                        continue

                    armed_ext = (
                        float(st.armed_extreme)
                        if st.armed_extreme is not None
                        else None
                    )
                    try:
                        outcome = await self._try_open(
                            strategy_id=strategy_id,
                            strategy=filter_strategy,
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
                            extreme_override=armed_ext,
                        )
                    except Exception as e:
                        # 单币下单异常不得打崩整条接针循环
                        logger.exception(
                            "wick_spike _try_open strategy=%d %s: %s",
                            strategy_id,
                            sym_key,
                            e,
                        )
                        release_bar_trigger(st)
                        continue
                    if outcome == "opened":
                        mark_bar_triggered(st, params, snap.bar_open_ts, now_ms)
                        next_refresh = min(next_refresh, time.time() + 1.0)
                    elif outcome in ("busy", "retryable_fail"):
                        # 回滚触发标记；保留武装，武装窗内强制重判
                        release_bar_trigger(st)
                    else:
                        # has_pos / blocked / committed_fail：保持本根已触发
                        pass

                # 事件驱动：成交唤醒；超时仍跑（武装强制重判 / 后台刷新）
                wake_ev.clear()
                try:
                    await asyncio.wait_for(wake_ev.wait(), timeout=_WAKE_TIMEOUT_SEC)
                except asyncio.TimeoutError:
                    if not any_update:
                        await asyncio.sleep(0)
                else:
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            strategy_log_service.info(strategy_id, "毫秒接针价流循环已停止")
            raise
        except Exception as e:
            logger.exception("wick_spike runner %d error: %s", strategy_id, e)
            strategy_log_service.error(strategy_id, f"毫秒接针循环异常 — {e}")
            raise
        finally:
            try:
                await price_stream_manager.clear_wanted(wake_owner)
            except Exception:
                pass
            if account_id_watching:
                try:
                    await account_position_stream.release(
                        account_id_watching, owner=wake_owner
                    )
                except Exception:
                    pass

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
        strategy: Strategy,
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
        extreme_override: float | None = None,
    ) -> str:
        """Returns: opened | busy | has_pos | blocked | retryable_fail | committed_fail

        策略锁仅覆盖门禁 + 市价成交；挂止盈/写库在锁外，缩短占锁时间。
        """
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

        api_res = None
        early: Optional[str] = None
        sym_key = _norm_sym(symbol)
        side = (signal.value or "").lower()
        vol_ratio = (snap.vol_now / snap.vol_sma) if snap.vol_sma > 0 else 0.0
        n = snap.atr * params.atr_mult
        # 优先用武装极值，与 on_tick 决策一致
        if extreme_override is not None and float(extreme_override) > 0:
            extreme = float(extreme_override)
        else:
            extreme = snapshot_extreme(side, snap, price)
        progress = spike_progress(side, snap.bar_open, extreme, n)
        need = effective_volume_mult(params, progress)
        gap = tip_gap_pct(snap.bar_open, extreme, price)
        trade_age_ms = (
            max(0, int(time.time() * 1000) - int(trade_ts_ms)) if trade_ts_ms > 0 else -1
        )
        open_api_ms = -1.0
        signal_to_order_ms = -1.0

        acc_id = int(getattr(strategy, "account_id", 0) or 0)
        try:
            if strategy is None or getattr(strategy, "status", None) != "running":
                early = "retryable_fail"
                return early

            # 内存快照门禁
            if tick_ctx.exchange_legs.get((sym_key, side), 0) > 0:
                early = "has_pos"
                return early
            if sym_key in (tick_ctx.exclude_norm or frozenset()):
                strategy_log_service.info(
                    strategy_id, f"{symbol} 接针触发但命中排除/黑名单快照，跳过"
                )
                early = "blocked"
                return early

            # 优先 User Data Stream 腿缓存；过期/缺失才 REST
            stream_qty = (
                account_position_stream.leg_qty(acc_id, symbol, side)
                if acc_id > 0
                else None
            )
            if stream_qty is not None:
                if stream_qty > 0:
                    tick_ctx.exchange_legs[(sym_key, side)] = stream_qty
                    strategy_log_service.info(
                        strategy_id,
                        f"{symbol} 接针触发但账户流显示已有同向仓，跳过",
                    )
                    early = "has_pos"
                    return early
            else:
                try:
                    fresh = await auth.fetch_positions([symbol])
                    fresh_legs = exchange_legs_from_positions(fresh or [])
                    if fresh_legs.get((sym_key, side), 0) > 0:
                        tick_ctx.exchange_legs[(sym_key, side)] = fresh_legs[
                            (sym_key, side)
                        ]
                        if acc_id > 0:
                            account_position_stream.set_leg(
                                acc_id,
                                symbol,
                                side,
                                float(fresh_legs[(sym_key, side)]),
                            )
                        strategy_log_service.info(
                            strategy_id,
                            f"{symbol} 接针触发但交易所已有同向仓，跳过",
                        )
                        early = "has_pos"
                        return early
                except Exception as e:
                    logger.warning(
                        "wick_spike %d %s pre-open position recheck failed: %s",
                        strategy_id,
                        symbol,
                        e,
                    )

            # 黑名单热复检：命中须 blocked（保持本根触发），不可当 retryable 反复打
            try:
                if await self._position_mgr._is_blacklisted_now(strategy_id, symbol):
                    strategy_log_service.info(
                        strategy_id, f"{symbol} 接针下单前黑名单复检命中，跳过"
                    )
                    early = "blocked"
                    return early
            except Exception as e:
                strategy_log_service.error(
                    strategy_id,
                    f"{symbol} 接针下单前黑名单复检失败，已安全取消 — {e}",
                )
                early = "blocked"
                return early

            detect_ms = (
                (time.perf_counter() - signal_detect_perf) * 1000.0
                if signal_detect_perf > 0
                else -1.0
            )
            strategy_log_service.info(
                strategy_id,
                f"{symbol} 毫秒接针触发 → {signal.value} "
                f"价={price:.6g} open={snap.bar_open:.6g} ext={extreme:.6g} "
                f"N={n:.6g} progress={progress:.2f} tip_gap%={gap:.3f} "
                f"ATR={snap.atr:.6g} vol×={vol_ratio:.1f} need×={need:g}",
            )
            logger.info(
                "wick_spike trigger strategy=%d %s %s px=%.6g open=%.6g ext=%.6g "
                "atrN=%.6g progress=%.2f tip_gap%%=%.3f vol×=%.2f need×=%g "
                "trade_age_ms=%d detect_to_lock_ms=%.1f "
                "vol_now=%s sma=%s",
                strategy_id,
                symbol,
                signal.value,
                price,
                snap.bar_open,
                extreme,
                n,
                progress,
                gap,
                vol_ratio,
                need,
                trade_age_ms,
                detect_ms,
                f"{snap.vol_now:.1f}",
                f"{snap.vol_sma:.1f}",
            )

            base_qty = self._position_mgr._compute_base_qty(strategy, total_margin, price)
            if base_qty is None:
                strategy_log_service.warning(
                    strategy_id, f"{symbol} 接针无法开仓 — 余额无效"
                )
                early = "retryable_fail"
                return early

            candidate = SignalCandidate(
                symbol=symbol,
                signal=signal,
                klines=[],
                current_price=price,
                rsi=float(vol_ratio),
                signal_label="毫秒接针",
                base_qty=base_qty,
            )

            # 市价下单（含账户锁 + 必要时设杠杆 + 黑名单热复检）
            order_sem = account_order_sem(strategy.account_id)
            t_api0 = time.perf_counter()
            async with order_sem:
                api_res = await self._position_mgr.execute_wick_open_market(
                    candidate, strategy, auth, leverage
                )
            t_filled = time.perf_counter()
            open_api_ms = (t_filled - t_api0) * 1000.0
            signal_to_order_ms = (
                (t_filled - signal_detect_perf) * 1000.0
                if signal_detect_perf > 0
                else -1.0
            )
            if api_res is None:
                early = "retryable_fail"
                return early

            # 成交后立刻更新内存腿，防止锁外窗口重复开
            fill_q = float(api_res.filled_qty or 0)
            tick_ctx.exchange_legs[(sym_key, side)] = (
                tick_ctx.exchange_legs.get((sym_key, side), 0) + fill_q
            )
            if acc_id > 0 and fill_q > 0:
                account_position_stream.apply_local_fill(
                    acc_id, symbol, side, fill_q
                )
        finally:
            lock.release()

        if early is not None:
            return early
        if api_res is None:
            return "retryable_fail"

        # 锁外：挂止盈 + 写库（本根已由 on_tick 标记 triggered，防双开）
        api_res = await self._position_mgr.place_open_tp_limit(auth, strategy, api_res)

        last_db_err: Exception | None = None
        for attempt in range(_DB_WRITE_RETRIES):
            try:
                async with async_session() as session:
                    db_strategy = await session.get(Strategy, strategy_id)
                    if not db_strategy or db_strategy.status != "running":
                        strategy_log_service.error(
                            strategy_id,
                            f"{symbol} 接针已成交但策略状态异常 — 请手动检查交易所",
                        )
                        return "committed_fail"
                    db_strategy.last_signal = signal.value
                    db_strategy.last_signal_at = now_beijing()
                    db_strategy.last_rsi = round(vol_ratio, 2)
                    await self._position_mgr.execute_open_db(
                        session, db_strategy, api_res
                    )
                    await session.commit()
                fill_px = float(api_res.avg_price or 0) or float(price)
                logger.info(
                    "wick_spike opened strategy=%d %s open_api_ms=%.0f "
                    "signal_to_order_ms=%.0f trade_age_ms=%d "
                    "px=%.6g open=%.6g ext=%.6g atrN=%.6g "
                    "progress=%.2f tip_gap%%=%.3f vol×=%.2f need×=%g",
                    strategy_id,
                    symbol,
                    open_api_ms,
                    signal_to_order_ms,
                    trade_age_ms,
                    fill_px,
                    snap.bar_open,
                    extreme,
                    n,
                    progress,
                    gap,
                    vol_ratio,
                    need,
                )
                return "opened"
            except Exception as e:
                last_db_err = e
                logger.error(
                    "wick_spike open db %s attempt=%d/%d: %s",
                    symbol,
                    attempt + 1,
                    _DB_WRITE_RETRIES,
                    e,
                )
                if attempt + 1 < _DB_WRITE_RETRIES:
                    await asyncio.sleep(_DB_WRITE_RETRY_DELAY_SEC)

        # 重试耗尽：孤儿仓对账补写
        try:
            async with async_session() as session:
                db_strategy = await session.get(Strategy, strategy_id)
                if db_strategy is not None:
                    outcome = await self._position_mgr._reconcile_orphan_from_exchange(
                        session,
                        db_strategy,
                        symbol,
                        auth,
                        float(api_res.avg_price or price or 0),
                    )
                    await session.commit()
                    if outcome == RECONCILE_CREATED:
                        strategy_log_service.warning(
                            strategy_id,
                            f"{symbol} 写库重试失败后已通过对账补建仓位记录",
                        )
                        logger.warning(
                            "wick_spike %d %s DB fail recovered via orphan reconcile",
                            strategy_id,
                            symbol,
                        )
                        return "opened"
        except Exception as e:
            logger.error(
                "wick_spike %d %s orphan reconcile after DB fail: %s",
                strategy_id,
                symbol,
                e,
            )

        strategy_log_service.error(
            strategy_id,
            f"{symbol} 接针开仓已成交但写库失败（已重试{_DB_WRITE_RETRIES}次）"
            f" — {last_db_err}；请手动检查交易所，本根不再重试",
        )
        return "committed_fail"


wick_spike_runner = WickSpikeRunner()
