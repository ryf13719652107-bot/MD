"""4h 震荡选币：ADX + 区间振幅 + 流动性评分（供选币池 source=range）。"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, Optional

from .binance_service import (
    BinanceService,
    get_cached_tradefi_symbols,
    is_tradefi_or_commodity_symbol,
)

logger = logging.getLogger(__name__)

# 4h 震荡筛选阈值
ADX_PERIOD = 14
ADX_MAX = 22.0
RANGE_MIN_PCT = 5.0
RANGE_MAX_PCT = 50.0
MAX_ABS_CHANGE_24H = 50.0
KLINE_TIMEFRAME = "4h"
KLINE_LIMIT = 60
# 扫描阶段固定门槛：仅拉成交额达标的候选（省 API）；策略成交量在读榜时再滤
MIN_CANDIDATE_VOLUME_24H = 30_000_000  # 3000 万 USDT
# 最多扫描多少个币的 4h K 线（按成交额排序，排除后取前 N）
MAX_SCAN_COUNT = 500
SCAN_CONCURRENCY = 8

# 震荡榜不扫描、不入选（用户不做主流大盘）
EXCLUDED_MAJOR_SYMBOLS: frozenset[str] = frozenset({
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "DOTUSDT",
    "LTCUSDT",
    "BCHUSDT",
    "NEARUSDT",
    "APTUSDT",
    "ARBUSDT",
    "OPUSDT",
    "FILUSDT",
    "ATOMUSDT",
    "UNIUSDT",
    "ETCUSDT",
    "TRXUSDT",
    "SUIUSDT",
})


def calculate_range_pct(klines: list, lookback: int | None = None) -> Optional[float]:
    """(HH-LL)/last_close * 100 over lookback bars."""
    bars = klines[-lookback:] if lookback else klines
    if len(bars) < 10:
        return None
    hi = max(float(k[2]) for k in bars)
    lo = min(float(k[3]) for k in bars)
    close = float(bars[-1][4])
    if close <= 0:
        return None
    return (hi - lo) / close * 100.0


def calculate_adx(klines: list, period: int = ADX_PERIOD) -> Optional[float]:
    """Wilder ADX(period) from OHLCV rows."""
    n = len(klines)
    if n < period + 2:
        return None

    highs = [float(x[2]) for x in klines]
    lows = [float(x[3]) for x in klines]
    closes = [float(x[4]) for x in klines]

    tr = [0.0] * n
    pdm = [0.0] * n
    mdm = [0.0] * n
    for i in range(1, n):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        pdm[i] = up if up > down and up > 0 else 0.0
        mdm[i] = down if down > up and down > 0 else 0.0

    def wilder_smooth(src: list[float], p: int) -> list[float]:
        out = [0.0] * n
        out[p] = sum(src[1 : p + 1])
        for i in range(p + 1, n):
            out[i] = out[i - 1] - (out[i - 1] / p) + src[i]
        return out

    atr = wilder_smooth(tr, period)
    apdm = wilder_smooth(pdm, period)
    amdm = wilder_smooth(mdm, period)

    dx_vals: list[float] = []
    for i in range(period, n):
        if atr[i] <= 0:
            continue
        pdi = 100.0 * apdm[i] / atr[i]
        mdi = 100.0 * amdm[i] / atr[i]
        denom = pdi + mdi
        if denom <= 0:
            continue
        dx_vals.append(100.0 * abs(pdi - mdi) / denom)

    if len(dx_vals) < period:
        return None

    adx = sum(dx_vals[:period]) / period
    for dx in dx_vals[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx


def oscillation_score(
    adx: float,
    range_pct: float,
    volume_24h: float,
    price_change_pct: float,
) -> float:
    """越高越适合震荡策略。"""
    vol_term = math.log10(max(volume_24h, 1.0)) * 1.5
    range_term = min(range_pct, 20.0) * 0.5
    change_penalty = abs(price_change_pct) * 0.3
    return (ADX_MAX - adx) * 2.0 + range_term + vol_term - change_penalty


def _range_filter_reject_reason(
    adx: float,
    range_pct: float,
    price_change_pct: float,
) -> str | None:
    """未通过时返回原因标签，供统计日志。"""
    if adx >= ADX_MAX:
        return "adx_high"
    if range_pct < RANGE_MIN_PCT:
        return "range_low"
    if range_pct > RANGE_MAX_PCT:
        return "range_high"
    if abs(price_change_pct) > MAX_ABS_CHANGE_24H:
        return "change_24h"
    return None


def _passes_range_filters(
    adx: float,
    range_pct: float,
    price_change_pct: float,
) -> bool:
    return _range_filter_reject_reason(adx, range_pct, price_change_pct) is None


def build_scan_candidates(
    ticker_items: list[dict[str, Any]],
    *,
    excluded_symbols: frozenset[str] = EXCLUDED_MAJOR_SYMBOLS,
    tradefi_norm: frozenset[str] | None = None,
    min_volume_24h: float = MIN_CANDIDATE_VOLUME_24H,
    max_scan: int = MAX_SCAN_COUNT,
) -> list[dict[str, Any]]:
    """
    候选 = 成交额达标 + 排除主流/TradFi/黄金白银原油等，按成交额降序取前 max_scan。
    """
    tradefi = tradefi_norm or frozenset()

    def _skip(sym: str) -> bool:
        if sym in excluded_symbols:
            return True
        return is_tradefi_or_commodity_symbol(sym, tradefi)

    eligible = [
        x
        for x in ticker_items
        if x.get("volume_24h", 0) >= min_volume_24h
        and not _skip(x.get("symbol", ""))
    ]
    eligible.sort(key=lambda x: -x["volume_24h"])
    return eligible[:max_scan]


async def fetch_range_oscillation_pool(
    binance: BinanceService,
    limit: int = 20,
    max_scan: int | None = None,
) -> list[dict[str, Any]]:
    """
    扫描 4h 震荡特征，返回与 fetch_top_movers 相同结构。source 固定为 'range'。
    候选仅扫 MIN_CANDIDATE_VOLUME_24H 以上成交额；更严门槛由策略 coin_pool_min_volume_24h 在读榜时过滤。
    """
    scan_cap = max_scan if max_scan is not None else MAX_SCAN_COUNT

    tickers = await binance.exchange.fetch_tickers()
    ticker_items: list[dict[str, Any]] = []
    for sym, t in tickers.items():
        if ":USDT" not in sym:
            continue
        pct = t.get("percentage")
        if pct is None:
            continue
        try:
            pct_f = float(pct)
        except (TypeError, ValueError):
            continue
        vol = float(t.get("quoteVolume") or 0)
        if vol <= 0:
            continue
        ticker_items.append({
            "symbol": sym.replace("/", "").replace(":USDT", ""),
            "price_change_pct": pct_f,
            "volume_24h": vol,
        })

    tradefi_norm = await get_cached_tradefi_symbols(binance)
    candidates = build_scan_candidates(
        ticker_items,
        tradefi_norm=tradefi_norm,
        max_scan=scan_cap,
    )

    sem = asyncio.Semaphore(SCAN_CONCURRENCY)
    reject_stats: dict[str, int] = {
        "klines_fail": 0,
        "klines_short": 0,
        "metric_none": 0,
        "adx_high": 0,
        "range_low": 0,
        "range_high": 0,
        "change_24h": 0,
        "passed": 0,
    }

    async def _scan_one(item: dict[str, Any]) -> Optional[dict[str, Any]]:
        sym = item["symbol"]
        async with sem:
            try:
                klines = await binance.fetch_klines(sym, KLINE_TIMEFRAME, KLINE_LIMIT)
            except Exception as e:
                logger.debug("range pool: %s klines failed: %s", sym, e)
                reject_stats["klines_fail"] += 1
                return None
        if not klines or len(klines) < ADX_PERIOD + 5:
            reject_stats["klines_short"] += 1
            return None
        adx = calculate_adx(klines, ADX_PERIOD)
        range_pct = calculate_range_pct(klines)
        if adx is None or range_pct is None:
            reject_stats["metric_none"] += 1
            return None
        reason = _range_filter_reject_reason(
            adx, range_pct, item["price_change_pct"]
        )
        if reason:
            reject_stats[reason] += 1
            return None
        reject_stats["passed"] += 1
        score = oscillation_score(
            adx, range_pct, item["volume_24h"], item["price_change_pct"]
        )
        return {
            **item,
            "adx": round(adx, 2),
            "range_pct": round(range_pct, 2),
            "oscillation_score": round(score, 2),
        }

    scan_results = await asyncio.gather(*[_scan_one(c) for c in candidates])
    scored = [r for r in scan_results if r is not None]

    scored.sort(key=lambda x: -x["oscillation_score"])
    result: list[dict[str, Any]] = []
    for i, row in enumerate(scored[:limit]):
        result.append({
            "symbol": row["symbol"],
            "rank": i + 1,
            "price_change_pct": row["price_change_pct"],
            "volume_24h": row["volume_24h"],
            "source": "range",
            "adx": row.get("adx"),
            "range_pct": row.get("range_pct"),
            "oscillation_score": row.get("oscillation_score"),
        })

    logger.info(
        "Range pool: candidates=%d passed=%d return=%d (limit=%d) reject=%s "
        "thresholds adx<%.0f range=%.0f-%.0f |24h%%|<=%.0f",
        len(candidates),
        len(scored),
        len(result),
        limit,
        reject_stats,
        ADX_MAX,
        RANGE_MIN_PCT,
        RANGE_MAX_PCT,
        MAX_ABS_CHANGE_24H,
    )
    return result
