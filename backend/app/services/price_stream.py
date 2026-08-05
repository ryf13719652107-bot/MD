"""Binance-only millisecond last-price cache via ccxt.pro watch_trades.

仅供 wick_spike 使用；不影响 K 线信号路径。订阅集按需增删，空闲回收。
同时按 K 线周期累计本根成交量/高低，供接针放量判定（不依赖 K 线 WS 量能滞后）。

多策略：按 owner 登记 wanted，取并集订阅；每 symbol 独立 timeframe（冲突时取更细周期）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_IDLE_STOP_AFTER_SEC = 15 * 60
_RECONNECT_INITIAL_BACKOFF = 1.0
_RECONNECT_MAX_BACKOFF = 30.0


def _norm_sym(s: str) -> str:
    return (s or "").replace("/", "").replace(":USDT", "").replace("_", "").upper()


def _timeframe_ms(timeframe: str) -> int:
    mapping = {
        "1m": 60_000,
        "3m": 180_000,
        "5m": 300_000,
        "15m": 900_000,
        "30m": 1_800_000,
        "1h": 3_600_000,
    }
    return mapping.get(timeframe or "1m", 60_000)


class PriceStreamManager:
    """Per-symbol last trade price fed by Binance watch_trades."""

    def __init__(self):
        self._prices: dict[str, float] = {}
        self._ts_ms: dict[str, int] = {}
        self._seq: dict[str, int] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._last_access: dict[str, float] = {}
        # 多策略并集
        self._wanted_by_owner: dict[str, set[str]] = {}
        self._owner_tf_ms: dict[str, int] = {}
        self._wanted: set[str] = set()
        self._tf_ms_by_sym: dict[str, int] = {}
        self._client = None
        self._lock = asyncio.Lock()
        self._janitor_task: Optional[asyncio.Task] = None
        self._global_seq = 0
        # 本根（按 per-symbol tf 对齐）从成交累计
        self._bar_open_ms: dict[str, int] = {}
        self._bar_vol: dict[str, float] = {}
        self._bar_high: dict[str, float] = {}
        self._bar_low: dict[str, float] = {}
        # owner → Event：成交到达时 set，供接针 runner 事件驱动唤醒
        self._wake_events: dict[str, asyncio.Event] = {}
        # 可选轻量回调（禁止在回调里 await 下单）
        self._trade_hooks: list[Callable[[str, float, float, int], None]] = []
        # 最近 N 秒滚动成交量（ts_ms, amount），供瞬时放量判定
        self._rolling_trades: dict[str, deque] = {}
        # WS 断连重连后需 REST 补强的 symbol 集合
        self._resync_needed: set[str] = set()

    def subscribe_wake(self, owner: str) -> asyncio.Event:
        """为 owner 登记唤醒事件；成交写入后 set。"""
        ev = self._wake_events.get(owner)
        if ev is None:
            ev = asyncio.Event()
            self._wake_events[owner] = ev
        return ev

    def unsubscribe_wake(self, owner: str) -> None:
        self._wake_events.pop(owner, None)

    def get(self, symbol: str) -> tuple[float, int] | None:
        """Return (price, event_ts_ms) or None."""
        key = _norm_sym(symbol)
        self._last_access[key] = time.time()
        px = self._prices.get(key)
        if px is None or px <= 0:
            return None
        return px, int(self._ts_ms.get(key, 0))

    def seq(self, symbol: str) -> int:
        return int(self._seq.get(_norm_sym(symbol), 0))

    def bar_volume(self, symbol: str) -> float:
        return float(self._bar_vol.get(_norm_sym(symbol), 0.0) or 0.0)

    def bar_high(self, symbol: str) -> float:
        return float(self._bar_high.get(_norm_sym(symbol), 0.0) or 0.0)

    def bar_low(self, symbol: str) -> float:
        v = self._bar_low.get(_norm_sym(symbol))
        return float(v) if v is not None and v > 0 else 0.0

    def bar_open_ms(self, symbol: str) -> int:
        return int(self._bar_open_ms.get(_norm_sym(symbol), 0) or 0)

    def instant_vol_annualized(self, symbol: str, window_sec: float = 5.0) -> float:
        """最近 window_sec 秒成交量折算到分钟（与 vol_sma 可比）。

        解决本根累计量滞后：开盘前几秒累计量只有全根的百分之几，
        但瞬时脉冲折算到分钟后可立即反映放量。
        """
        key = _norm_sym(symbol)
        dq = self._rolling_trades.get(key)
        if not dq:
            return 0.0
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - int(window_sec * 1000)
        total = sum(amt for ts, amt in dq if ts >= cutoff)
        if total <= 0 or window_sec <= 0:
            return 0.0
        return total * (60.0 / window_sec)

    def consume_resync_needed(self) -> set[str]:
        """返回并清空 WS 断连重连后需 REST 补强的 symbol 集合。"""
        if not self._resync_needed:
            return set()
        out = set(self._resync_needed)
        self._resync_needed.clear()
        return out

    @property
    def global_seq(self) -> int:
        return self._global_seq

    def _recompute_wanted_unlocked(self) -> None:
        wanted: set[str] = set()
        for syms in self._wanted_by_owner.values():
            wanted |= syms
        self._wanted = wanted

        tf_map: dict[str, int] = {}
        for owner, syms in self._wanted_by_owner.items():
            tf = int(self._owner_tf_ms.get(owner, 60_000) or 60_000)
            for s in syms:
                prev = tf_map.get(s)
                if prev is None:
                    tf_map[s] = tf
                elif prev != tf:
                    chosen = min(prev, tf)
                    logger.warning(
                        "price_stream tf conflict %s: %sms vs %sms (owner=%s), using %sms",
                        s,
                        prev,
                        tf,
                        owner,
                        chosen,
                    )
                    tf_map[s] = chosen
        self._tf_ms_by_sym = tf_map

    async def set_wanted(
        self,
        client,
        symbols: set[str],
        *,
        timeframe: str = "1m",
        owner: str = "default",
    ) -> None:
        """按 owner 登记订阅集；多策略取并集，互不覆盖。"""
        self._client = client
        norms = {_norm_sym(s) for s in symbols if s}
        async with self._lock:
            self._wanted_by_owner[owner] = norms
            self._owner_tf_ms[owner] = _timeframe_ms(timeframe)
            self._recompute_wanted_unlocked()
            for sym in self._wanted:
                self._last_access[sym] = time.time()
                task = self._tasks.get(sym)
                if task is None or task.done():
                    self._tasks[sym] = asyncio.create_task(
                        self._run_symbol(sym),
                        name=f"price_ws:{sym}",
                    )
            if self._janitor_task is None or self._janitor_task.done():
                self._janitor_task = asyncio.create_task(
                    self._janitor_loop(), name="price_stream_janitor"
                )

    async def clear_wanted(self, owner: str) -> None:
        """策略停止时移除其订阅登记，并集自动收缩。"""
        async with self._lock:
            self._wanted_by_owner.pop(owner, None)
            self._owner_tf_ms.pop(owner, None)
            self._recompute_wanted_unlocked()
        self.unsubscribe_wake(owner)

    def _apply_trade(self, symbol_norm: str, px: float, amount: float, ts_ms: int) -> None:
        self._prices[symbol_norm] = px
        self._ts_ms[symbol_norm] = ts_ms
        self._seq[symbol_norm] = self._seq.get(symbol_norm, 0) + 1
        self._global_seq += 1
        self._last_access[symbol_norm] = time.time()

        tf = max(1, int(self._tf_ms_by_sym.get(symbol_norm, 60_000)))
        bar = (int(ts_ms) // tf) * tf
        if self._bar_open_ms.get(symbol_norm) != bar:
            self._bar_open_ms[symbol_norm] = bar
            self._bar_vol[symbol_norm] = 0.0
            self._bar_high[symbol_norm] = px
            self._bar_low[symbol_norm] = px
        if amount > 0:
            self._bar_vol[symbol_norm] = float(self._bar_vol.get(symbol_norm, 0.0)) + amount
        hi = self._bar_high.get(symbol_norm, px)
        lo = self._bar_low.get(symbol_norm, px)
        self._bar_high[symbol_norm] = max(hi, px)
        self._bar_low[symbol_norm] = min(lo, px) if lo > 0 else px

        # 最近 5 秒滚动成交量（供瞬时放量判定）
        dq = self._rolling_trades.setdefault(symbol_norm, deque(maxlen=2000))
        dq.append((ts_ms, amount))
        cutoff = ts_ms - 5000
        while dq and dq[0][0] < cutoff:
            dq.popleft()

        for ev in self._wake_events.values():
            ev.set()
        for hook in self._trade_hooks:
            try:
                hook(symbol_norm, px, amount, ts_ms)
            except Exception:
                logger.debug("price_stream trade hook failed", exc_info=True)

    async def _run_symbol(self, symbol_norm: str) -> None:
        backoff = _RECONNECT_INITIAL_BACKOFF
        just_reconnected = True  # 首次启动也需 REST 补强
        while symbol_norm in self._wanted:
            client = self._client
            if client is None:
                await asyncio.sleep(0.5)
                continue
            try:
                trades = await client.watch_trades(symbol_norm)
                rows = trades if isinstance(trades, list) else [trades]
                for t in rows:
                    if not isinstance(t, dict):
                        continue
                    try:
                        px = float(t.get("price") or 0)
                    except (TypeError, ValueError):
                        continue
                    if px <= 0:
                        continue
                    try:
                        amount = float(t.get("amount") or 0)
                    except (TypeError, ValueError):
                        amount = 0.0
                    ts = t.get("timestamp")
                    try:
                        ts_ms = int(ts) if ts is not None else int(time.time() * 1000)
                    except (TypeError, ValueError):
                        ts_ms = int(time.time() * 1000)
                    self._apply_trade(symbol_norm, px, amount, ts_ms)
                backoff = _RECONNECT_INITIAL_BACKOFF
                # 仅重连后首次成功才标记 REST 补强（断连期间成交数据丢失）
                if just_reconnected:
                    self._resync_needed.add(symbol_norm)
                    just_reconnected = False
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if symbol_norm not in self._wanted:
                    break
                logger.warning(
                    "price_stream %s ws error: %s; retry in %.1fs",
                    symbol_norm, e, backoff,
                )
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise
                backoff = min(backoff * 2, _RECONNECT_MAX_BACKOFF)
                just_reconnected = True  # 下次成功后需补强

    async def _janitor_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(60)
                now = time.time()
                async with self._lock:
                    for sym, task in list(self._tasks.items()):
                        if sym in self._wanted:
                            continue
                        last = self._last_access.get(sym, 0)
                        if now - last < _IDLE_STOP_AFTER_SEC:
                            continue
                        task.cancel()
                        self._tasks.pop(sym, None)
                        self._prices.pop(sym, None)
                        self._ts_ms.pop(sym, None)
                        self._seq.pop(sym, None)
                        self._bar_open_ms.pop(sym, None)
                        self._bar_vol.pop(sym, None)
                        self._bar_high.pop(sym, None)
                        self._bar_low.pop(sym, None)
                        self._tf_ms_by_sym.pop(sym, None)
                        self._rolling_trades.pop(sym, None)
                        logger.info("price_stream stopped idle %s", sym)
        except asyncio.CancelledError:
            raise

    async def shutdown(self) -> None:
        async with self._lock:
            self._wanted_by_owner.clear()
            self._owner_tf_ms.clear()
            self._wanted.clear()
            self._tf_ms_by_sym.clear()
            tasks = list(self._tasks.values())
            self._tasks.clear()
            if self._janitor_task and not self._janitor_task.done():
                tasks.append(self._janitor_task)
            self._janitor_task = None
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._prices.clear()
        self._ts_ms.clear()
        self._seq.clear()
        self._bar_open_ms.clear()
        self._bar_vol.clear()
        self._bar_high.clear()
        self._bar_low.clear()
        self._rolling_trades.clear()
        self._resync_needed.clear()


price_stream_manager = PriceStreamManager()
