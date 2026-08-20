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
from ..config import now_beijing, BEIJING_TZ
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
from .position_manager import PositionManager, _norm_sym
from .tick_context import SignalCandidate, TickContext, exchange_legs_from_positions
from .account_concurrency import account_order_sem
from .strategy_concurrency import hold_strategy_symbol
from .leverage_prewarm import prewarm_symbols_leverage
from .wick_spike_engine import (
    WickSpikeParams,
    WickSymbolState,
    apply_1m_ema_filter_fields,
    build_bar_snapshot,
    clear_rebound,
    enrich_snap_with_trades,
    effective_volume_mult,
    is_arm_active,
    merge_synthetic_forming_bar,
    near_miss_diag,
    on_tick,
    pierce_vol_view,
    mark_bar_triggered,
    release_bar_trigger,
    spike_progress,
    snapshot_extreme,
    take_diag_event,
    tip_gap_pct,
)
from .strategy_engine import Signal
from .trailing_tp_engine import (
    TrailingTpParams,
    TrailingTpMemState,
    apply_tick,
    STATE_ARMED,
    STATE_ACTIVE,
    STATE_EXPIRED,
)

logger = logging.getLogger(__name__)

_SYMBOL_REFRESH_SEC = 15.0
_POLL_IDLE_SEC = 0.005
# ATR 为 Wilder RMA，路径依赖强：历史过短时末值易与币安长图分叉。
# 预热/纠偏拉够 ATR 窗；热路径未就绪前不开仓（勿再用 atr_period+2≈16 根就判刺破）。
_KLINE_ATR_BARS = 300
_KLINE_MIN_BARS = _KLINE_ATR_BARS  # 兼容旧引用
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
_ARM_REST_SEC = 2.0
# forming 停滞 REST 纠偏间隔（秒）；过短会在换分钟时打爆 ccxt 限流队列
_FORMING_REST_SEC = 1.5
# 同策略同币同向腿锁等待；不再抢 :30/:40 任务锁
_SYMBOL_LOCK_WAIT_SEC = 0.5
# 成交后写库重试
_DB_WRITE_RETRIES = 3
_DB_WRITE_RETRY_DELAY_SEC = 0.35


def _wick_params_from_strategy(strategy: Strategy) -> tuple[WickSpikeParams, int, int, str, str]:
    """从 Strategy ORM 抽出接针参数（调用方须已保证字段可读）。"""
    relax_flag = getattr(strategy, "wick_amp_vol_relax_enabled", None)
    params = WickSpikeParams(
        direction=strategy.direction,
        volume_mult=float(getattr(strategy, "wick_volume_mult", 6.0) or 0),
        atr_mult=float(getattr(strategy, "wick_spike_atr_mult", 4.0) or 4.0),
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
            getattr(strategy, "wick_arm_wait_sec", 0.0)
            if getattr(strategy, "wick_arm_wait_sec", None) is not None
            else 0.0
        ),
        arm_retrace_grace_sec=float(
            getattr(strategy, "wick_arm_retrace_grace_sec", 5.0)
            if getattr(strategy, "wick_arm_retrace_grace_sec", None) is not None
            else 5.0
        ),
        arm_grace_max_tip_gap_pct=float(
            getattr(strategy, "wick_arm_grace_max_tip_gap_pct", 2.0)
            if getattr(strategy, "wick_arm_grace_max_tip_gap_pct", None) is not None
            else 2.0
        ),
        # 反弹追踪（方案J）：confirm后等价格从针尖反弹触发市价；默认开
        rebound_enabled=(
            True
            if getattr(strategy, "wick_rebound_enabled", None) is None
            else bool(strategy.wick_rebound_enabled)
        ),
        rebound_trigger_pct=float(
            getattr(strategy, "wick_rebound_trigger_pct", 20.0)
            if getattr(strategy, "wick_rebound_trigger_pct", None) is not None
            else 20.0
        ),
        rebound_abort_pct=float(
            getattr(strategy, "wick_rebound_abort_pct", 35.0)
            if getattr(strategy, "wick_rebound_abort_pct", None) is not None
            else 35.0
        ),
        rebound_wait_sec=float(
            getattr(strategy, "wick_rebound_wait_sec", 0.0)
            if getattr(strategy, "wick_rebound_wait_sec", None) is not None
            else 0.0
        ),
        # 1m 开盘 vs EMA25：产品默认开
        ema25_filter_enabled=(
            True
            if getattr(strategy, "wick_ema25_filter_enabled", None) is None
            else bool(strategy.wick_ema25_filter_enabled)
        ),
    )
    atr_period = int(getattr(strategy, "wick_atr_period", 14) or 14)
    vol_period = int(getattr(strategy, "wick_volume_sma_period", 20) or 20)
    timeframe = strategy.timeframe
    direction = strategy.direction
    return params, atr_period, vol_period, timeframe, direction


def _track_bar_ts(st: WickSymbolState) -> Optional[int]:
    """当前活跃追踪的 bar ts（武装窗或反弹窗）；无活跃追踪返回 None。

    反弹窗内 armed_bar_ts 已被 clear_arm 清空，须改用 rebound_bar_ts
    作为 is_arm_active 的 bar_open_ts 入参，否则反弹窗失去强制重判与超时检查。
    """
    if st.armed_bar_ts is not None:
        return st.armed_bar_ts
    if st.rebound_bar_ts is not None and st.rebound_at_ms > 0:
        return st.rebound_bar_ts
    return None


class WickSpikeRunner:
    def __init__(self):
        self._tasks: dict[int, asyncio.Task] = {}
        self._bg_tasks: set[asyncio.Task] = set()
        self._position_mgr = PositionManager()
        # 时间移动止盈：按 strategy_id 分桶的内存状态（_refresh_context 异步刷新）
        # trailing_params: strategy_id -> TrailingTpParams（开关关闭时为 None）
        # trailing_mems: strategy_id -> {sym_key -> list[TrailingTpMemState]}
        # trailing_auth: strategy_id -> auth client（后台平仓/撤单用）
        # trailing_close_inflight: strategy_id -> set[sym_key]（防同币并发平仓）
        self._trailing_params: dict[int, TrailingTpParams | None] = {}
        self._trailing_mems: dict[int, dict[str, list[TrailingTpMemState]]] = {}
        self._trailing_auth: dict[int, object] = {}
        self._trailing_close_inflight: dict[int, set[str]] = {}

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
        last_forming_rest: dict[str, float] = {}  # forming 停滞 REST 纠偏独立 1s 节流
        last_arm_rest: dict[str, float] = {}
        arm_rest_inflight: set[str] = set()
        open_inflight: set[str] = set()  # 后台市价开仓中的币，防双开且不堵扫描
        last_near_miss_log: dict[str, float] = {}
        last_arm_force_ms: dict[str, int] = {}
        last_resync_forming: dict[str, float] = {}
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
                    # 用更长窗口灌 ATR，减少与交易所图表的 RMA 分叉
                    await kline_stream_manager.get(pub, sym, tf, _KLINE_ATR_BARS)
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
                await kline_stream_manager.refresh_rest(pub, sym, tf, _KLINE_ATR_BARS)
            except Exception:
                pass

        async def _bg_forming_rest(
            pub, sym: str, tf: str, *, force: bool = False
        ) -> None:
            """forming 停滞 / 价流重连：只拉最近 2 根，避免全量 refresh_rest 打爆限流。"""
            try:
                rows = await kline_stream_manager.refresh_forming(
                    pub, sym, tf, limit=2, force=force
                )
                if not rows:
                    if force:
                        price_stream_manager.note_resync_needed(sym)
                    return
                # 把 REST 本根极值并入成交流聚合，弥补断连丢 tip
                forming = rows[-1]
                try:
                    ts = int(forming[0])
                    hi = float(forming[2])
                    lo = float(forming[3])
                    vol = float(forming[5]) if len(forming) > 5 else 0.0
                except (TypeError, ValueError, IndexError):
                    if force:
                        price_stream_manager.note_resync_needed(sym)
                    return
                price_stream_manager.ratchet_bar_from_kline(
                    sym, bar_open_ts=ts, high=hi, low=lo, volume=vol
                )
            except Exception:
                if force:
                    try:
                        price_stream_manager.note_resync_needed(sym)
                    except Exception:
                        pass

        async def _bg_arm_rest(pub, sym: str, tf: str, sym_key: str) -> None:
            """武装后轻量 REST 补强本根 K 线（只拉 2 根），覆盖 WS 量能/极值滞后。"""
            try:
                rows = await kline_stream_manager.refresh_forming(pub, sym, tf, limit=2)
                if rows:
                    forming = rows[-1]
                    try:
                        price_stream_manager.ratchet_bar_from_kline(
                            sym,
                            bar_open_ts=int(forming[0]),
                            high=float(forming[2]),
                            low=float(forming[3]),
                            volume=float(forming[5]) if len(forming) > 5 else 0.0,
                        )
                    except (TypeError, ValueError, IndexError):
                        pass
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
                    # EMA25 过滤看 1m：非 1m 策略后台预热 1m 缓冲（不进热路径）
                    if params.ema25_filter_enabled and str(timeframe or "").lower() not in (
                        "1m",
                        "1min",
                    ):
                        self._fire_bg(_bg_prewarm_klines(public, list(symbols), "1m"))
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

                # WS 断连重连后：强制轻量 REST 补本根高低/量，并并入成交流 tip
                # take_resync：只取本策略池内币，避免多 runner 全局 consume 丢掉其它策略币
                resync_set = price_stream_manager.take_resync_needed(symbols)
                if resync_set and public is not None:
                    for sym in symbols:
                        sk = _norm_sym(sym)
                        if sk not in resync_set:
                            continue
                        if now - last_resync_forming.get(sk, 0.0) < 0.5:
                            # 节流跳过时重新入队，避免丢失
                            price_stream_manager.note_resync_needed(sym)
                            continue
                        last_resync_forming[sk] = now
                        self._fire_bg(
                            _bg_forming_rest(public, sym, timeframe, force=True)
                        )

                # 武装窗优先扫描；仍保留 cold（否则 A 武装时 B 仅量变/K 变会被漏扫）
                now_ms_loop = int(time.time() * 1000)
                retry_syms: list[str] = []
                hot: list[str] = []
                cold: list[str] = []
                for sym in symbols:
                    sym_key = _norm_sym(sym)
                    st0 = states.get(sym_key)
                    st0_ts = _track_bar_ts(st0) if st0 is not None else None
                    if (
                        st0 is not None
                        and st0_ts is not None
                        and is_arm_active(st0, params, st0_ts, now_ms_loop)
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
                    st_ts = _track_bar_ts(st)
                    arm_active = (
                        st_ts is not None
                        and is_arm_active(st, params, st_ts, now_ms)
                    )

                    # --- 时间移动止盈：毫秒级追踪（开关关闭则 params=None 跳过，零影响）---
                    # 价格有变化或武装/反弹 force_retry 时执行；armed 超时也在此检查
                    if price > 0 and (price_changed or arm_active):
                        self._tick_trailing(
                            strategy_id, sym_key, price, now_ms
                        )

                    if not self._position_mgr._passes_new_entry_filters(
                        sym, filter_strategy, tick_ctx
                    ):
                        continue

                    side = "long" if direction == "long" else "short"
                    # 优先新鲜账户流腿；流缺失时不信过期 tick_ctx（开仓门禁再 REST）
                    acc_for_leg = int(getattr(filter_strategy, "account_id", 0) or 0)
                    stream_leg = (
                        account_position_stream.leg_qty(acc_for_leg, sym_key, side)
                        if acc_for_leg > 0
                        else None
                    )
                    if stream_leg is not None:
                        if stream_leg > 0:
                            tick_ctx.exchange_legs[(sym_key, side)] = stream_leg
                            continue
                        # 流显示已平：清掉可能过期的 tick_ctx 腿
                        tick_ctx.exchange_legs.pop((sym_key, side), None)

                    # 热路径：只读 WS 内存，绝不 await REST
                    klines = kline_stream_manager.peek(public, sym, timeframe)
                    # 缓冲不足 ATR 窗：后台拉长历史，本轮跳过（避免短窗 RMA 误判刺破）
                    if len(klines) < _KLINE_ATR_BARS:
                        if now - last_bg_rest.get(sym_key, 0.0) >= _BG_REST_SEC:
                            last_bg_rest[sym_key] = now
                            self._fire_bg(_bg_rest_one(public, sym, timeframe))
                        continue

                    last = klines[-1]
                    # forming 停滞：后台 REST 纠偏；若成交流已在新根则合成 forming 继续 on_tick
                    try:
                        forming_ts = int(last[0])
                    except (TypeError, ValueError, IndexError):
                        continue
                    tf_ms = _timeframe_ms(timeframe)
                    current_bar_ts = (now_ms // tf_ms) * tf_ms
                    if forming_ts < current_bar_ts:
                        if now - last_forming_rest.get(sym_key, 0.0) >= _FORMING_REST_SEC:
                            last_forming_rest[sym_key] = now
                            self._fire_bg(_bg_forming_rest(public, sym, timeframe))
                        trade_bar_ts = price_stream_manager.bar_open_ms(sym_key)
                        if (
                            price <= 0
                            or trade_bar_ts != current_bar_ts
                        ):
                            continue
                        synth = merge_synthetic_forming_bar(
                            klines,
                            current_bar_ts=current_bar_ts,
                            last_price=price,
                            trade_high=price_stream_manager.bar_high(sym_key),
                            trade_low=price_stream_manager.bar_low(sym_key),
                            trade_vol=price_stream_manager.bar_volume(sym_key),
                            trade_open=price_stream_manager.bar_open_px(sym_key),
                        )
                        if not synth:
                            continue
                        klines = synth
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
                        ema_period=int(params.ema25_period or 25),
                    )
                    if snap is None:
                        continue
                    # EMA25 过滤固定看 1m：非 1m 策略时同步 peek 1m 缓冲（无 REST）
                    if params.ema25_filter_enabled and str(timeframe or "").lower() not in (
                        "1m",
                        "1min",
                    ):
                        snap = apply_1m_ema_filter_fields(
                            snap,
                            kline_stream_manager.peek(public, sym, "1m"),
                            ema_period=int(params.ema25_period or 25),
                        )
                    kline_vol_raw = float(snap.vol_now or 0)
                    trade_vol_raw = price_stream_manager.bar_volume(sym_key)
                    # 成交流量/高低补强；bar 未对齐则忽略（防换根串量）
                    snap = enrich_snap_with_trades(
                        snap,
                        trade_vol=trade_vol_raw,
                        trade_high=price_stream_manager.bar_high(sym_key),
                        trade_low=price_stream_manager.bar_low(sym_key),
                        trade_bar_open_ts=price_stream_manager.bar_open_ms(sym_key),
                        trade_instant_vol=price_stream_manager.instant_vol_annualized(sym_key),
                    )

                    prev_armed_at = st.armed_at_ms
                    t_signal0 = time.perf_counter()
                    signal = on_tick(st, params, snap, price, now_ms)
                    rb_ev = take_diag_event(st)
                    if rb_ev:
                        logger.info(
                            "wick_spike %s strategy=%d %s px=%s "
                            "rb_ext=%s arm_age_ms=%s wait=%.1fs "
                            "trig%%=%g abort%%=%g",
                            rb_ev,
                            strategy_id,
                            sym_key,
                            f"{price:.6g}",
                            (
                                f"{st.rebound_extreme:.6g}"
                                if st.rebound_extreme is not None
                                else "?"
                            ),
                            (
                                (now_ms - st.rebound_at_ms)
                                if st.rebound_at_ms > 0
                                else 0
                            ),
                            float(params.rebound_wait_sec or 0),
                            float(params.rebound_trigger_pct or 0),
                            float(params.rebound_abort_pct or 0),
                        )
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
                        # 武装等量 / 反弹窗内：后台 REST 补强本根，解决 WS 高低/量滞后
                        # （反弹窗尤其需要：尖峰常比成交价流晚到 K 线）
                        in_rebound = (
                            st.rebound_bar_ts == snap.bar_open_ts
                            and st.rebound_at_ms > 0
                            and st.triggered_bar_ts != snap.bar_open_ts
                        )
                        need_forming_rest = in_rebound or (
                            st.armed_bar_ts == snap.bar_open_ts
                            and st.armed_awaiting_vol
                        )
                        if (
                            need_forming_rest
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

                    # 反弹触发后保留 rebound_extreme，供 tip_gap / 开仓失败重试
                    armed_ext = (
                        float(st.armed_extreme)
                        if st.armed_extreme is not None
                        else (
                            float(st.rebound_extreme)
                            if st.rebound_extreme is not None
                            else None
                        )
                    )
                    # 市价开仓丢后台：不阻塞同策略其它币 tip/开火；triggered 已置位防双信号
                    if sym_key in open_inflight:
                        continue
                    open_inflight.add(sym_key)
                    bar_ts_for_open = int(snap.bar_open_ts)
                    params_for_open = params
                    strategy_for_open = filter_strategy
                    auth_for_open = auth
                    public_for_open = public
                    margin_for_open = total_margin
                    lev_for_open = leverage
                    ctx_for_open = tick_ctx
                    px_for_open = price
                    snap_for_open = snap
                    signal_for_open = signal
                    trade_ts_for_open = trade_ts_ms
                    detect_perf_for_open = t_signal0
                    ext_for_open = armed_ext

                    async def _bg_try_open(
                        *,
                        _sym=sym,
                        _sym_key=sym_key,
                        _st=st,
                        _signal=signal_for_open,
                        _price=px_for_open,
                        _snap=snap_for_open,
                        _params=params_for_open,
                        _strategy=strategy_for_open,
                        _auth=auth_for_open,
                        _public=public_for_open,
                        _margin=margin_for_open,
                        _lev=lev_for_open,
                        _ctx=ctx_for_open,
                        _trade_ts=trade_ts_for_open,
                        _detect=detect_perf_for_open,
                        _ext=ext_for_open,
                        _bar_ts=bar_ts_for_open,
                    ) -> None:
                        nonlocal next_refresh
                        outcome = "retryable_fail"
                        try:
                            outcome = await self._try_open(
                                strategy_id=strategy_id,
                                strategy=_strategy,
                                symbol=_sym,
                                signal=_signal,
                                price=_price,
                                snap=_snap,
                                params=_params,
                                auth=_auth,
                                public=_public,
                                total_margin=_margin,
                                leverage=_lev,
                                tick_ctx=_ctx,
                                trade_ts_ms=_trade_ts,
                                signal_detect_perf=_detect,
                                extreme_override=_ext,
                            )
                        except Exception as e:
                            logger.exception(
                                "wick_spike _try_open strategy=%d %s: %s",
                                strategy_id,
                                _sym_key,
                                e,
                            )
                            release_bar_trigger(_st)
                            return
                        finally:
                            open_inflight.discard(_sym_key)

                        now_done = int(time.time() * 1000)
                        if outcome == "opened":
                            mark_bar_triggered(
                                _st, _params, _bar_ts, now_done
                            )
                            # 尽快刷新上下文，纳入新仓腿
                            next_refresh = min(next_refresh, time.time() + 1.0)
                        elif outcome in ("busy", "retryable_fail"):
                            release_bar_trigger(_st)
                        else:
                            # has_pos / blocked：锁定本根，清反弹态
                            clear_rebound(_st)

                    self._fire_bg(_bg_try_open())

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

        # REST 快照对账：清除 UDS 中已平仓但仍残留的腿（防假 has_pos 屏蔽币）
        acc_id_for_reconcile = int(getattr(strategy, "account_id", 0) or 0)
        if acc_id_for_reconcile > 0:
            account_position_stream.reconcile_from_rest(
                acc_id_for_reconcile, exchange_legs
            )

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

        # --- 时间移动止盈：刷新内存状态（开关关闭则清空，零影响）---
        self._refresh_trailing(strategy_id, strategy, open_rows, auth)

        return strategy, watch, tick_ctx, total_margin, auth, public, leverage

    def _refresh_trailing(
        self,
        strategy_id: int,
        strategy: Strategy,
        open_rows: list,
        auth,
    ) -> None:
        """从 DB Position 重建内存 trailing 状态。开关关闭则清空本策略桶。

        在 _refresh_context 后台任务里调用（单线程 asyncio，主循环读安全）。
        已平仓/缺失的 Position 自然从字典消失；新开仓的 armed 状态被加入。
        """
        if not getattr(strategy, "trailing_tp_enabled", False):
            self._trailing_params.pop(strategy_id, None)
            self._trailing_mems.pop(strategy_id, None)
            self._trailing_auth.pop(strategy_id, None)
            self._trailing_close_inflight.pop(strategy_id, None)
            return

        params = TrailingTpParams(
            enabled=True,
            window_sec=float(strategy.trailing_tp_window_sec or 300.0),
            activate_threshold_pct=float(strategy.take_profit_pct or 2.0),
            drawdown_base_pct=float(strategy.trailing_tp_drawdown_base_pct or 30.0),
            drawdown_tier1_pct=float(strategy.trailing_tp_drawdown_tier1_pct or 20.0),
            drawdown_tier2_pct=float(strategy.trailing_tp_drawdown_tier2_pct or 15.0),
            tier1_threshold=float(strategy.trailing_tp_tier1_threshold or 2.5),
            tier2_threshold=float(strategy.trailing_tp_tier2_threshold or 5.0),
        )
        self._trailing_params[strategy_id] = params
        self._trailing_auth[strategy_id] = auth
        self._trailing_close_inflight.setdefault(strategy_id, set())

        buckets: dict[str, list[TrailingTpMemState]] = {}
        # 旧内存状态：position_id -> mem（保留热路径更新的 peak_pct/state，避免 refresh 丢失）
        old_buckets = self._trailing_mems.get(strategy_id, {})
        old_by_pid: dict[int, TrailingTpMemState] = {}
        for _mems in old_buckets.values():
            for _m in _mems:
                old_by_pid[int(_m.position_id)] = _m

        for p in open_rows:
            state_str = (p.trailing_tp_state or "").strip()
            if state_str not in (STATE_ARMED, STATE_ACTIVE, STATE_EXPIRED):
                continue
            sym_key = _norm_sym(p.symbol)
            try:
                opened_ms = int(p.opened_at.replace(tzinfo=BEIJING_TZ).timestamp() * 1000)
            except Exception:
                opened_ms = 0
            pid = int(p.id)
            # 优先用旧 mem 的 state/peak_pct/peak_price（热路径最新值）
            old_mem = old_by_pid.get(pid)
            if old_mem is not None:
                state_str = old_mem.state
                peak_pct = old_mem.peak_pct
                peak_price = old_mem.peak_price
            else:
                peak_pct = float(p.trailing_tp_peak_pct or 0.0)
                peak_price = 0.0
            mem = TrailingTpMemState(
                position_id=pid,
                symbol=sym_key,
                side=(p.side or "").lower(),
                entry_price=float(p.entry_price or 0),
                opened_at_ms=opened_ms,
                state=state_str,
                peak_pct=peak_pct,
                peak_price=peak_price,
            )
            buckets.setdefault(sym_key, []).append(mem)
        self._trailing_mems[strategy_id] = buckets

    async def _inject_trailing_mem_after_open(
        self,
        session,
        strategy_id: int,
        symbol: str,
    ) -> None:
        """开仓成功后立即把新 Position 推入内存 mems，消除 15s 空窗。

        在 _post_open_async 的 session 里调用（execute_open_db 已 flush，id 已有）。
        用真实 position_id 构建 mem，确保 _refresh_trailing 的 old_by_pid 能匹配，
        peak_pct 不丢失。trailing 关闭或非 armed 状态时跳过。
        """
        params = self._trailing_params.get(strategy_id)
        if params is None or not params.enabled:
            return
        sym_key = _norm_sym(symbol)
        now_ms = int(time.time() * 1000)
        try:
            rows = list(
                (
                    await session.execute(
                        select(Position).where(
                            Position.strategy_id == strategy_id,
                            Position.closed_at.is_(None),
                            Position.trailing_tp_state == STATE_ARMED,
                        )
                    )
                ).scalars().all()
            )
        except Exception:
            return
        rows = [r for r in rows if _norm_sym(r.symbol) == sym_key]
        if not rows:
            return
        buckets = self._trailing_mems.setdefault(strategy_id, {})
        existing = buckets.setdefault(sym_key, [])
        existing_pids = {m.position_id for m in existing if m.position_id > 0}
        for r in rows:
            pid = int(r.id)
            if pid in existing_pids:
                continue
            try:
                opened_ms = int(
                    r.opened_at.replace(tzinfo=BEIJING_TZ).timestamp() * 1000
                )
            except Exception:
                opened_ms = now_ms
            existing.append(
                TrailingTpMemState(
                    position_id=pid,
                    symbol=sym_key,
                    side=(r.side or "").lower(),
                    entry_price=float(r.entry_price or 0),
                    opened_at_ms=opened_ms,
                    state=STATE_ARMED,
                    peak_pct=float(r.trailing_tp_peak_pct or 0.0),
                    peak_price=0.0,
                )
            )
            existing_pids.add(pid)
        logger.info(
            "wick_spike trailing_tp inject strategy=%d %s — 开仓后立即追踪（零空窗）",
            strategy_id, sym_key,
        )

    def get_trailing_status(
        self, strategy_id: int, sym_key: str
    ) -> list[dict]:
        """供前端 API 查询：返回本币种的 trailing 状态快照（实时，读内存不查 DB）。

        每条包含：position_id, side, state, peak_pct, remaining_sec, drawdown_limit。
        开关关闭或无持仓时返回空列表。
        """
        params = self._trailing_params.get(strategy_id)
        if params is None or not params.enabled:
            return []
        buckets = self._trailing_mems.get(strategy_id)
        if not buckets:
            return []
        mems = buckets.get(sym_key)
        if not mems:
            return []
        now_ms = int(time.time() * 1000)
        from .trailing_tp_engine import (
            remaining_window_sec,
            current_drawdown_limit,
        )
        out: list[dict] = []
        for m in mems:
            rem = (
                remaining_window_sec(m, now_ms, params.window_sec)
                if m.state == STATE_ARMED
                else None
            )
            ddlimit = (
                current_drawdown_limit(params, m.peak_pct)
                if m.state == STATE_ACTIVE
                else None
            )
            out.append({
                "position_id": m.position_id,
                "side": m.side,
                "state": m.state,
                "peak_pct": round(m.peak_pct, 4),
                "remaining_sec": round(rem, 1) if rem is not None else None,
                "drawdown_limit_pct": round(ddlimit, 2) if ddlimit is not None else None,
                "entry_price": m.entry_price,
            })
        return out

    def _tick_trailing(
        self,
        strategy_id: int,
        sym_key: str,
        price: float,
        now_ms: int,
    ) -> None:
        """热路径：对本 sym 的 trailing mems 调用 apply_tick，事件 fire-and-forget。

        开关关闭（params=None）或本 sym 无 mem 时 O(1) 跳过，零影响。
        平仓事件用 inflight 集合防同币并发；撤单/挂单事件可并发。
        """
        params = self._trailing_params.get(strategy_id)
        if params is None or not params.enabled:
            return
        buckets = self._trailing_mems.get(strategy_id)
        if not buckets:
            return
        mems = buckets.get(sym_key)
        if not mems:
            return

        close_inflight = self._trailing_close_inflight.setdefault(strategy_id, set())
        auth = self._trailing_auth.get(strategy_id)

        for mem in mems:
            if mem.state == STATE_EXPIRED:
                continue
            event = apply_tick(mem, params, price, now_ms)
            if event is None:
                continue
            if event == "close":
                if sym_key in close_inflight:
                    continue
                close_inflight.add(sym_key)
                # mem.state 已是 active；传入 mem 供后台任务读 entry/peak
                self._fire_bg(self._post_trailing_close_async(
                    strategy_id, sym_key, mem, price
                ))
            elif event == "activated":
                # armed→active：撤旧限价止盈单（如有）
                self._fire_bg(self._post_trailing_activate_async(
                    strategy_id, sym_key, mem
                ))
                logger.info(
                    "wick_spike trailing_tp activated strategy=%d %s "
                    "entry=%.6g peak=%.4f%% px=%.6g",
                    strategy_id, sym_key, mem.entry_price, mem.peak_pct, price,
                )
            elif event == "window_expired":
                # armed→expired：回退挂限价止盈单
                self._fire_bg(self._post_trailing_expire_async(
                    strategy_id, sym_key, mem
                ))
                logger.info(
                    "wick_spike trailing_tp window_expired strategy=%d %s "
                    "entry=%.6g — 回退限价止盈",
                    strategy_id, sym_key, mem.entry_price,
                )

    async def _post_trailing_activate_async(
        self,
        strategy_id: int,
        sym_key: str,
        mem: TrailingTpMemState,
    ) -> None:
        """armed→active：撤旧 tp_limit_order_id（如有），让移动追踪接管。

        异常只 log 不抛；mem.state 已由热路径切 active，DB state 由下次 refresh 同步。
        """
        try:
            async with async_session() as session:
                rows = list(
                    (
                        await session.execute(
                            select(Position).where(
                                Position.strategy_id == strategy_id,
                                Position.closed_at.is_(None),
                                Position.symbol == sym_key,
                            )
                        )
                    ).scalars().all()
                )
                oids = [
                    (p.tp_limit_order_id or "").strip()
                    for p in rows
                    if (p.tp_limit_order_id or "").strip()
                ]
                if not oids:
                    return
                auth = self._trailing_auth.get(strategy_id)
                if auth is None:
                    return
                # 复用 position_mgr 的撤单逻辑（_cancel_bot_tp_order_ids）
                await self._position_mgr._cancel_bot_tp_order_ids(
                    auth, sym_key, set(oids), strategy_id, keep_id=""
                )
                for p in rows:
                    p.tp_limit_order_id = None
                    p.trailing_tp_state = STATE_ACTIVE
                await session.commit()
                strategy_log_service.info(
                    strategy_id,
                    f"{sym_key} trailing_tp 激活 — 撤旧限价单 {len(oids)} 张，"
                    f"开始毫秒级移动追踪",
                )
        except Exception as e:
            logger.error(
                "wick_spike trailing_tp activate strategy=%d %s: %s",
                strategy_id, sym_key, e,
            )

    async def _post_trailing_expire_async(
        self,
        strategy_id: int,
        sym_key: str,
        mem: TrailingTpMemState,
    ) -> None:
        """armed→expired：窗口超时，回退到原限价止盈逻辑。

        更新 DB state='expired'，并调用 _ensure_tp_limit_orders 挂限价单。
        之后 scheduler 的 _manage_positions 会正常接管（trailing_taken_over=False）。
        """
        try:
            async with async_session() as session:
                db_strategy = await session.get(Strategy, strategy_id)
                if db_strategy is None or db_strategy.status != "running":
                    return
                open_positions = list(
                    (
                        await session.execute(
                            select(Position).where(
                                Position.strategy_id == strategy_id,
                                Position.closed_at.is_(None),
                                Position.symbol == sym_key,
                            )
                        )
                    ).scalars().all()
                )
                if not open_positions:
                    return
                for p in open_positions:
                    p.trailing_tp_state = STATE_EXPIRED
                await session.flush()

                auth = self._trailing_auth.get(strategy_id)
                if auth is None:
                    await session.commit()
                    return
                # 复用马丁引擎算 avg_entry / 止盈价
                from .martingale_engine import MartingaleEngine
                positions_data = [
                    {"quantity": p.quantity, "entry_price": p.entry_price}
                    for p in open_positions
                ]
                eng = MartingaleEngine(
                    base_quantity=0,
                    multiplier=db_strategy.martingale_mult,
                    max_layers=db_strategy.max_layers,
                    price_drop_pct=db_strategy.price_drop_pct,
                    price_drop_multiplier=float(db_strategy.price_drop_multiplier or 1.0),
                    take_profit_pct=db_strategy.take_profit_pct,
                )
                avg_entry, total_qty = eng.get_avg_entry_price(positions_data)
                pos_side = (open_positions[0].side or "").lower()
                await self._position_mgr._ensure_tp_limit_orders(
                    session, db_strategy, sym_key, auth,
                    open_positions, eng, avg_entry, total_qty, pos_side,
                )
                await session.commit()
                strategy_log_service.info(
                    strategy_id,
                    f"{sym_key} trailing_tp 窗口超时 — 回退限价止盈逻辑",
                )
        except Exception as e:
            logger.error(
                "wick_spike trailing_tp expire strategy=%d %s: %s",
                strategy_id, sym_key, e,
            )

    async def _post_trailing_close_async(
        self,
        strategy_id: int,
        sym_key: str,
        mem: TrailingTpMemState,
        trigger_price: float,
    ) -> None:
        """active 触发回撤平仓：市价平仓 + 清 inflight + 清 mem。

        复用 _close_positions；平仓后 Position.closed_at 写入，下次 refresh
        自然从 mems 字典消失。inflight 在 finally 清除（无论成功失败）。
        """
        try:
            async with async_session() as session:
                db_strategy = await session.get(Strategy, strategy_id)
                if db_strategy is None or db_strategy.status != "running":
                    return
                open_positions = list(
                    (
                        await session.execute(
                            select(Position).where(
                                Position.strategy_id == strategy_id,
                                Position.closed_at.is_(None),
                                Position.symbol == sym_key,
                            )
                        )
                    ).scalars().all()
                )
                if not open_positions:
                    return
                auth = self._trailing_auth.get(strategy_id)
                if auth is None:
                    return
                from .martingale_engine import MartingaleEngine
                positions_data = [
                    {"quantity": p.quantity, "entry_price": p.entry_price}
                    for p in open_positions
                ]
                eng = MartingaleEngine(
                    base_quantity=0,
                    multiplier=db_strategy.martingale_mult,
                    max_layers=db_strategy.max_layers,
                    price_drop_pct=db_strategy.price_drop_pct,
                    price_drop_multiplier=float(db_strategy.price_drop_multiplier or 1.0),
                    take_profit_pct=db_strategy.take_profit_pct,
                )
                avg_entry, total_qty = eng.get_avg_entry_price(positions_data)
                pos_side = (open_positions[0].side or "").lower()
                strategy_log_service.success(
                    strategy_id,
                    f"{sym_key} 移动止盈平仓 — 触发价 {trigger_price:.6g} "
                    f"峰值盈利 {mem.peak_pct:.2f}%",
                )
                await self._position_mgr._close_positions(
                    session, db_strategy, sym_key, auth,
                    open_positions, eng, avg_entry, pos_side,
                    "移动止盈", trigger_price,
                )
                await session.commit()
        except Exception as e:
            logger.error(
                "wick_spike trailing_tp close strategy=%d %s: %s",
                strategy_id, sym_key, e,
            )
        finally:
            inflight = self._trailing_close_inflight.get(strategy_id)
            if inflight is not None:
                inflight.discard(sym_key)
            # 从内存字典移除（避免下次 tick 重复触发；refresh 会重建）
            buckets = self._trailing_mems.get(strategy_id)
            if buckets is not None:
                buckets.pop(sym_key, None)

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

        只持「策略×币种×方向」腿锁覆盖门禁 + 市价成交（不抢 :30/:40 任务锁）；
        挂止盈/写库在锁外。同向腿 manage/止盈处理仍与开仓互斥。
        """
        sym_key = _norm_sym(symbol)
        side = (signal.value or getattr(strategy, "direction", None) or "").lower()

        def _skip(reason: str, detail: str = "") -> str:
            logger.info(
                "wick_spike skip strategy=%d %s reason=%s%s",
                strategy_id,
                sym_key,
                reason,
                f" {detail}" if detail else "",
            )
            return reason

        # 同策略同币同向短等；管其它币/另一条反向策略不影响
        try:
            async with hold_strategy_symbol(
                strategy_id, sym_key, side, timeout=_SYMBOL_LOCK_WAIT_SEC
            ):
                return await self._try_open_locked(
                    strategy_id=strategy_id,
                    strategy=strategy,
                    symbol=symbol,
                    sym_key=sym_key,
                    side=side,
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
                    signal_detect_perf=signal_detect_perf,
                    extreme_override=extreme_override,
                    skip=_skip,
                )
        except asyncio.TimeoutError:
            strategy_log_service.info(
                strategy_id,
                f"{symbol} 接针触发但同向腿占用中，稍后重试",
            )
            return _skip("busy", "leg_lock_timeout")

    async def _try_open_locked(
        self,
        *,
        strategy_id: int,
        strategy: Strategy,
        symbol: str,
        sym_key: str,
        side: str,
        signal: Signal,
        price: float,
        snap,
        params: WickSpikeParams,
        auth,
        public,
        total_margin: float,
        leverage: float,
        tick_ctx: TickContext,
        trade_ts_ms: int,
        signal_detect_perf: float,
        extreme_override: float | None,
        skip,
    ) -> str:
        """腿锁内：门禁 + 市价开仓。调用方必须已持有 strategy_leg_lock。"""
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
        if strategy is None or getattr(strategy, "status", None) != "running":
            return skip("retryable_fail", "strategy_not_running")

        # 优先新鲜账户流；流缺失时不信过期 tick_ctx，必须 REST（防假 has_pos 锁本根）
        stream_qty = (
            account_position_stream.leg_qty(acc_id, symbol, side)
            if acc_id > 0
            else None
        )
        if stream_qty is not None and stream_qty > 0:
            tick_ctx.exchange_legs[(sym_key, side)] = stream_qty
            strategy_log_service.info(
                strategy_id,
                f"{symbol} 接针触发但账户流显示已有同向仓，跳过",
            )
            return skip("has_pos", "account_stream")
        if stream_qty is not None and stream_qty <= 0:
            tick_ctx.exchange_legs.pop((sym_key, side), None)

        if sym_key in (tick_ctx.exclude_norm or frozenset()):
            strategy_log_service.info(
                strategy_id, f"{symbol} 接针触发但命中排除/黑名单快照，跳过"
            )
            return skip("blocked", "exclude_norm")

        # 流缺失或显示空仓：REST 复核同向腿（防手动仓 / 清腿过量 / 过期 tick_ctx）
        if stream_qty is None or stream_qty <= 0:
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
                    return skip("has_pos", "rest_positions")
                # REST 确认空：对齐缓存
                tick_ctx.exchange_legs.pop((sym_key, side), None)
                if acc_id > 0 and stream_qty is None:
                    account_position_stream.set_leg(acc_id, symbol, side, 0.0)
            except Exception as e:
                logger.warning(
                    "wick_spike %d %s pre-open position recheck failed: %s",
                    strategy_id,
                    symbol,
                    e,
                )
                # fail-closed：查不到持仓则不开，避免叠在手动同向腿上
                strategy_log_service.warning(
                    strategy_id,
                    f"{symbol} 接针开仓前持仓复检失败，跳过本轮 — {e}",
                )
                return skip("retryable_fail", "pos_recheck_failed")

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
            return skip("retryable_fail", "bad_balance")

        candidate = SignalCandidate(
            symbol=symbol,
            signal=signal,
            klines=[],
            current_price=price,
            rsi=float(vol_ratio),
            signal_label="毫秒接针",
            base_qty=base_qty,
        )

        # 市价下单（账户下单信号量 + 必要时设杠杆）
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
            return skip("retryable_fail", "order_none")

        # 成交后立刻更新内存腿，防止锁外窗口重复开
        fill_q = float(api_res.filled_qty or 0)
        tick_ctx.exchange_legs[(sym_key, side)] = (
            tick_ctx.exchange_legs.get((sym_key, side), 0) + fill_q
        )
        if acc_id > 0 and fill_q > 0:
            account_position_stream.apply_local_fill(
                acc_id, symbol, side, fill_q
            )

        # 锁外：挂止盈 + 写库丢后台 fire-and-forget，主循环立即继续扫描其他 symbol
        # （内存腿已更新 + apply_local_fill，防双开不受影响；DB 失败有 orphan reconcile 兜底）
        # 注意：_post_open 在币种锁释放前已 schedule；实际 IO 在锁外执行
        self._fire_bg(self._post_open_async(
            strategy_id, strategy, api_res, auth, signal,
            price, snap, extreme, n, progress, gap, vol_ratio, need,
            trade_age_ms, open_api_ms, signal_to_order_ms,
        ))
        return "opened"

    async def _post_open_async(
        self,
        strategy_id: int,
        strategy: Strategy,
        api_res,
        auth,
        signal,
        price: float,
        snap,
        extreme: float,
        n: float,
        progress: float,
        gap: float,
        vol_ratio: float,
        need: float,
        trade_age_ms: int,
        open_api_ms: float,
        signal_to_order_ms: float,
    ) -> None:
        """后台执行：挂止盈限价 + 写库 + orphan reconcile。不阻塞主循环。

        fire-and-forget：异常只 log 不抛。仓位已成交，DB 失败有对账兜底。
        """
        symbol = api_res.symbol
        try:
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
                            return
                        db_strategy.last_signal = signal.value
                        db_strategy.last_signal_at = now_beijing()
                        db_strategy.last_rsi = round(vol_ratio, 2)
                        await self._position_mgr.execute_open_db(
                            session, db_strategy, api_res
                        )
                        # 开仓成功立即推入 trailing mems，消除 15s 空窗
                        # （不等 _refresh_context，下一个 tick 即开始追踪）
                        await self._inject_trailing_mem_after_open(
                            session, strategy_id, symbol
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
                    return
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

            # 重试耗尽：仅用本笔成交单号补建（不领养手动仓）
            try:
                async with async_session() as session:
                    db_strategy = await session.get(Strategy, strategy_id)
                    if db_strategy is not None:
                        recovered = await self._position_mgr.recover_bot_open_after_db_fail(
                            session, db_strategy, api_res, auth
                        )
                        await session.commit()
                        if recovered:
                            logger.warning(
                                "wick_spike %d %s DB fail recovered via orphan reconcile",
                                strategy_id,
                                symbol,
                            )
                            return
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
        except Exception as e:
            logger.exception(
                "wick_spike _post_open_async strategy=%d %s: %s",
                strategy_id,
                symbol,
                e,
            )


wick_spike_runner = WickSpikeRunner()
