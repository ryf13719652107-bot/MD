"""WebSocket K-line stream manager.

为策略信号计算提供实时 OHLCV 数据：
- 每个 (symbol, timeframe) 启动一个后台任务，调用 ccxt.pro `watch_ohlcv`
  持续接收推送，并把最近 N 根写入内存缓冲。
- 首次订阅时通过 REST `fetch_ohlcv` 灌入历史，避免冷启动 RSI/WT 收敛不足。
- `get()` 返回最近 N 根快照；若 WS 未就绪或缓冲不够，自动 REST 兜底。
- 长时间未读取的订阅自动停止，减轻交易所连接资源。

策略主循环原本每 tick `fetch_ohlcv` → 改成读这里的内存缓冲，可大幅减少 REST。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_MAX_BARS = 500
_IDLE_STOP_AFTER_SEC = 15 * 60  # 15 分钟没人读 → 关闭订阅
_RECONNECT_INITIAL_BACKOFF = 1.0
_RECONNECT_MAX_BACKOFF = 30.0


def _norm_sym(s: str) -> str:
    return (s or "").replace("/", "").replace(":USDT", "").upper()


def _timeframe_ms(timeframe: str) -> int:
    """K 线周期 → 毫秒（与 scheduler TIMEFRAME_SECONDS 对齐，未知周期按 1m）。"""
    mapping = {
        "1m": 60_000,
        "3m": 180_000,
        "5m": 300_000,
        "15m": 900_000,
        "30m": 1_800_000,
        "1h": 3_600_000,
        "2h": 7_200_000,
        "4h": 14_400_000,
        "1d": 86_400_000,
    }
    return mapping.get(timeframe, 60_000)


def _normalize_candles(raw) -> list[list]:
    """统一 WS / REST 返回为 [[ts,o,h,l,c,v], ...]。

    ccxt `watch_ohlcv` 一般为蜡烛列表；个别版本或中间态可能是单根扁平数组。
    """
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) == 0:
        return []
    if isinstance(raw[0], (list, tuple)):
        out: list[list] = []
        for x in raw:
            if isinstance(x, (list, tuple)) and len(x) >= 6:
                out.append([x[0], x[1], x[2], x[3], x[4], x[5]])
        return out
    if len(raw) >= 6 and isinstance(raw[0], (int, float)):
        return [[raw[0], raw[1], raw[2], raw[3], raw[4], raw[5]]]
    return []


def _buffer_stale_for_timeframe(buf: list, timeframe: str) -> bool:
    """最后一根 K 的开盘时间若长期不推进，说明 WS 可能未更新，应用 REST 纠偏。"""
    if not buf:
        return True
    try:
        last_open = int(buf[-1][0])
    except (TypeError, ValueError, IndexError):
        return True
    now_ms = int(time.time() * 1000)
    tf_ms = _timeframe_ms(timeframe)
    # 新周期开始后应很快收到更新；超时仍停在旧开盘时间则视为停滞
    return (now_ms - last_open) > (tf_ms + 15_000)


class KlineStreamManager:
    """Per-(symbol, timeframe) OHLCV cache fed by ccxt.pro websockets."""

    def __init__(self, max_bars: int = _DEFAULT_MAX_BARS):
        self._max_bars = max_bars
        self._buffers: dict[tuple[str, str], list[list]] = {}
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._ready: dict[tuple[str, str], asyncio.Event] = {}
        self._last_access: dict[tuple[str, str], float] = {}
        self._lock = asyncio.Lock()
        self._janitor_task: Optional[asyncio.Task] = None

    @staticmethod
    def _key(symbol: str, timeframe: str) -> tuple[str, str]:
        return (_norm_sym(symbol), timeframe)

    def _merge(self, key: tuple[str, str], rows) -> None:
        rows = _normalize_candles(rows)
        if not rows:
            return
        buf = self._buffers.get(key) or []
        if not buf:
            self._buffers[key] = list(rows)[-self._max_bars :]
            return
        idx = {int(r[0]): i for i, r in enumerate(buf)}
        for r in rows:
            try:
                ts = int(r[0])
            except (TypeError, ValueError, IndexError):
                continue
            if ts in idx:
                buf[idx[ts]] = list(r)
            else:
                buf.append(list(r))
                idx[ts] = len(buf) - 1
        buf.sort(key=lambda x: int(x[0]))
        if len(buf) > self._max_bars:
            buf = buf[-self._max_bars :]
        self._buffers[key] = buf

    async def _seed_via_rest(self, public_binance, symbol: str, timeframe: str, limit: int) -> None:
        key = self._key(symbol, timeframe)
        try:
            data = await public_binance.fetch_klines(symbol, timeframe, limit=limit)
            if data:
                self._merge(key, data)
        except Exception as e:
            logger.warning("kline_stream seed REST failed for %s %s: %s", symbol, timeframe, e)

    async def _run_subscription(self, public_binance, symbol: str, timeframe: str) -> None:
        key = self._key(symbol, timeframe)
        backoff = _RECONNECT_INITIAL_BACKOFF
        while True:
            try:
                ohlcv = await public_binance.watch_klines(symbol, timeframe)
                self._merge(key, ohlcv)
                ev = self._ready.get(key)
                if ev is not None and not ev.is_set():
                    ev.set()
                backoff = _RECONNECT_INITIAL_BACKOFF
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    "kline_stream %s %s ws error: %s; retry in %.1fs",
                    symbol, timeframe, e, backoff,
                )
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise
                backoff = min(backoff * 2, _RECONNECT_MAX_BACKOFF)

    async def _ensure_started(self, public_binance, symbol: str, timeframe: str, min_bars: int) -> None:
        key = self._key(symbol, timeframe)
        seed_limit = max(min_bars, self._max_bars)
        need_start = False
        async with self._lock:
            self._last_access[key] = time.time()
            task = self._tasks.get(key)
            if task is None or task.done():
                need_start = True
                self._ready[key] = asyncio.Event()
        if need_start:
            await self._seed_via_rest(public_binance, symbol, timeframe, seed_limit)
            async with self._lock:
                task2 = self._tasks.get(key)
                if task2 is None or task2.done():
                    self._tasks[key] = asyncio.create_task(
                        self._run_subscription(public_binance, symbol, timeframe),
                        name=f"kline_ws:{key[0]}:{key[1]}",
                    )
                if self._janitor_task is None or self._janitor_task.done():
                    self._janitor_task = asyncio.create_task(
                        self._janitor_loop(), name="kline_stream_janitor"
                    )

    async def get(
        self,
        public_binance,
        symbol: str,
        timeframe: str,
        min_bars: int,
    ) -> list[list]:
        """Return up to `min_bars` most recent OHLCV rows.

        - 若订阅未启动：启动并 REST 灌入种子。
        - 若缓冲够新且条数足：直接返回（减少 REST）。
        - 条数不足或 K 线时间停滞：REST 拉取合并（防止 WS 挂了后永远停在种子数据上不开仓）。
        """
        key = self._key(symbol, timeframe)
        await self._ensure_started(public_binance, symbol, timeframe, min_bars)
        self._last_access[key] = time.time()
        buf = self._buffers.get(key) or []
        need_rest = len(buf) < min_bars or _buffer_stale_for_timeframe(buf, timeframe)
        if not need_rest:
            return list(buf[-self._max_bars :])
        try:
            data = await public_binance.fetch_klines(
                symbol, timeframe, limit=max(min_bars, self._max_bars)
            )
            if data:
                self._merge(key, data)
        except Exception as e:
            logger.warning(
                "kline_stream REST fallback failed for %s %s: %s", symbol, timeframe, e
            )
        return list((self._buffers.get(key) or [])[-self._max_bars :])

    async def _stop_subscription(self, key: tuple[str, str]) -> None:
        task = self._tasks.pop(key, None)
        self._ready.pop(key, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug("kline_stream stop %s: %s", key, e)

    async def _janitor_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(60)
                now = time.time()
                idle_keys: list[tuple[str, str]] = []
                async with self._lock:
                    for key, ts in list(self._last_access.items()):
                        if now - ts > _IDLE_STOP_AFTER_SEC:
                            idle_keys.append(key)
                            self._last_access.pop(key, None)
                            self._buffers.pop(key, None)
                for key in idle_keys:
                    logger.info(
                        "kline_stream stop idle subscription %s %s", key[0], key[1]
                    )
                    await self._stop_subscription(key)
                if not self._tasks:
                    return
        except asyncio.CancelledError:
            raise

    async def shutdown(self) -> None:
        async with self._lock:
            keys = list(self._tasks.keys())
        for key in keys:
            await self._stop_subscription(key)
        if self._janitor_task and not self._janitor_task.done():
            self._janitor_task.cancel()
            try:
                await self._janitor_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._janitor_task = None
        self._buffers.clear()
        self._last_access.clear()


kline_stream_manager = KlineStreamManager()
