"""Binance-only millisecond last-price cache via ccxt.pro watch_trades.

仅供 wick_spike 使用；不影响 K 线信号路径。订阅集按需增删，空闲回收。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

_IDLE_STOP_AFTER_SEC = 15 * 60
_RECONNECT_INITIAL_BACKOFF = 1.0
_RECONNECT_MAX_BACKOFF = 30.0


def _norm_sym(s: str) -> str:
    return (s or "").replace("/", "").replace(":USDT", "").replace("_", "").upper()


class PriceStreamManager:
    """Per-symbol last trade price fed by Binance watch_trades."""

    def __init__(self):
        self._prices: dict[str, float] = {}
        self._ts_ms: dict[str, int] = {}
        self._seq: dict[str, int] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._last_access: dict[str, float] = {}
        self._wanted: set[str] = set()
        self._client = None
        self._lock = asyncio.Lock()
        self._janitor_task: Optional[asyncio.Task] = None
        self._global_seq = 0

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

    @property
    def global_seq(self) -> int:
        return self._global_seq

    async def set_wanted(self, client, symbols: set[str]) -> None:
        """Ensure watchers for `symbols`; drop others when idle janitor runs."""
        self._client = client
        wanted = {_norm_sym(s) for s in symbols if s}
        async with self._lock:
            self._wanted = wanted
            for sym in wanted:
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

    async def _run_symbol(self, symbol_norm: str) -> None:
        backoff = _RECONNECT_INITIAL_BACKOFF
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
                    ts = t.get("timestamp")
                    try:
                        ts_ms = int(ts) if ts is not None else int(time.time() * 1000)
                    except (TypeError, ValueError):
                        ts_ms = int(time.time() * 1000)
                    self._prices[symbol_norm] = px
                    self._ts_ms[symbol_norm] = ts_ms
                    self._seq[symbol_norm] = self._seq.get(symbol_norm, 0) + 1
                    self._global_seq += 1
                    self._last_access[symbol_norm] = time.time()
                backoff = _RECONNECT_INITIAL_BACKOFF
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
                        logger.info("price_stream stopped idle %s", sym)
        except asyncio.CancelledError:
            raise

    async def shutdown(self) -> None:
        async with self._lock:
            self._wanted.clear()
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


price_stream_manager = PriceStreamManager()
