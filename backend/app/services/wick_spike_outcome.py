"""开仓记录对齐 trades + K 线最终高低，评估「相对最终针尖」与盈亏。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select

from ..config import BEIJING_TZ
from ..database import async_session
from ..models.strategy import Strategy
from ..models.trade import Trade
from .binance_service import get_public_binance
from .position_manager import _norm_sym
from .wick_spike_engine import tip_gap_pct
from .wick_spike_log_stats import EntryRow, WickLogReport, summarize_gaps

logger = logging.getLogger(__name__)


def _parse_ts(ts: str) -> Optional[datetime]:
    try:
        # 日志为北京时间无时区
        return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING_TZ)
    except ValueError:
        return None


def _bar_open_dt(ts: datetime, timeframe: str = "1m") -> datetime:
    """按策略周期把时间对齐到 K 线开盘时刻。"""
    tf = (timeframe or "1m").lower()
    step = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}.get(tf, 1)
    total_min = ts.hour * 60 + ts.minute
    floored = (total_min // step) * step
    return ts.replace(hour=floored // 60, minute=floored % 60, second=0, microsecond=0)


@dataclass
class OpenOutcome:
    ts: str
    strategy_id: int
    symbol: str
    side: str
    side_zh: str
    entry_px: float
    bar_open: float
    # 触发瞬间极值（日志）
    trigger_ext: float | None
    tip_gap_at_trigger_pct: float | None
    # 本根收盘后最终极值
    final_ext: float | None
    final_tip_gap_pct: float | None
    wick_range_pct: float | None
    capture_ratio: float | None  # 1=贴最终针尖，0=停在开盘
    # 信号条件（触发瞬间）
    vol_x: float | None = None
    need_x: float | None = None
    progress: float | None = None
    atr_n: float | None = None  # N=ATR×倍数
    # 速度（ms）
    trade_age_ms: int | None = None
    detect_to_lock_ms: float | None = None
    open_api_ms: float | None = None
    signal_to_order_ms: float | None = None  # 捕捉信号 → 下单返回
    # 对齐成交
    trade_id: int | None = None
    trade_entry: float | None = None
    trade_exit: float | None = None
    realized_pnl: float | None = None
    pnl_pct: float | None = None
    close_reason: str | None = None
    layer: int | None = None
    matched: bool = False
    kline_ok: bool = False
    note: str = ""


def _finite_f(v: float) -> float | None:
    if v != v:
        return None
    return float(v)


def _side_zh(side: str) -> str:
    s = (side or "").lower()
    if s == "long":
        return "做多"
    if s == "short":
        return "做空"
    return side or "-"


def _final_tip_metrics(
    side: str,
    bar_open: float,
    entry_px: float,
    high: float,
    low: float,
) -> tuple[float, float, float, float]:
    """返回 final_ext, final_tip_gap_pct, wick_range_pct, capture_ratio。"""
    side = (side or "").lower()
    if bar_open <= 0 or entry_px <= 0:
        return float("nan"), float("nan"), float("nan"), float("nan")
    if side == "short":
        final_ext = high
        gap = (final_ext - entry_px) / bar_open * 100.0
        wick = (final_ext - bar_open) / bar_open * 100.0
    else:
        final_ext = low
        gap = (entry_px - final_ext) / bar_open * 100.0
        wick = (bar_open - final_ext) / bar_open * 100.0
    # 进场优于「最终极值」时 gap 为负，统计上视为已贴尖
    gap = max(0.0, gap)
    wick = max(0.0, wick)
    if wick > 1e-9:
        # 已走出的针深里，进场吃到多少（1=在针尖，0=在开盘）
        captured = max(0.0, min(1.0, 1.0 - gap / wick))
    else:
        captured = float("nan")
    return final_ext, gap, wick, captured


def pair_opened_with_side(
    report: WickLogReport,
) -> list[tuple[EntryRow, str, EntryRow | None]]:
    """opened 行配方向：优先同策略同币、时间接近的 trigger；并带回 trigger 行。"""
    triggers = [e for e in report.entries if e.kind == "trigger"]
    opened = [e for e in report.entries if e.kind == "opened"]
    out: list[tuple[EntryRow, str, EntryRow | None]] = []
    for op in opened:
        op_dt = _parse_ts(op.ts)
        best_side = ""
        best_dt = None
        best_tr: EntryRow | None = None
        for tr in triggers:
            if tr.strategy_id != op.strategy_id:
                continue
            if _norm_sym(tr.symbol) != _norm_sym(op.symbol):
                continue
            tr_dt = _parse_ts(tr.ts)
            if op_dt is None or tr_dt is None:
                continue
            delta = abs((op_dt - tr_dt).total_seconds())
            if delta > 30:
                continue
            if best_dt is None or delta < best_dt:
                best_dt = delta
                best_side = (tr.side or "").lower()
                best_tr = tr
        out.append((op, best_side, best_tr))
    return out


def _finite_ms(v: float) -> float | None:
    if v != v or v < 0:
        return None
    return float(v)


def match_trade(
    trades: list[Trade],
    *,
    strategy_id: int,
    symbol: str,
    side: str,
    event_dt: datetime,
    window_sec: float = 180.0,
) -> Optional[Trade]:
    sym = _norm_sym(symbol)
    side = (side or "").lower()
    event_naive = event_dt.replace(tzinfo=None) if event_dt.tzinfo else event_dt
    best: Trade | None = None
    best_key: tuple[int, float] | None = None  # (layer_penalty, delta)
    for t in trades:
        if t.strategy_id != strategy_id:
            continue
        if _norm_sym(t.symbol) != sym:
            continue
        if side and (t.side or "").lower() != side:
            continue
        if t.entry_time is None:
            continue
        delta = abs((t.entry_time - event_naive).total_seconds())
        if delta > window_sec:
            continue
        layer = int(getattr(t, "layer", 0) or 0)
        # 优先第 0 层（接针开仓），再取时间最近
        key = (0 if layer == 0 else 1, delta)
        if best_key is None or key < best_key:
            best = t
            best_key = key
    return best


def _pnl_buckets(rows: list[OpenOutcome]) -> list[dict]:
    """按最终距针尖%分桶，看盈亏是否更好（解决「和盈亏脱节」）。"""
    bins = [
        ("贴尖 ≤0.3%", 0.0, 0.3),
        ("较贴 0.3%~1%", 0.3, 1.0),
        ("偏离 1%~2%", 1.0, 2.0),
        ("偏离 >2%", 2.0, 1e9),
    ]
    out = []
    for label, lo, hi in bins:
        xs = [
            r
            for r in rows
            if r.matched
            and r.pnl_pct is not None
            and r.final_tip_gap_pct is not None
            and r.final_tip_gap_pct == r.final_tip_gap_pct
            and lo <= r.final_tip_gap_pct < hi
        ]
        if not xs:
            out.append({"bucket": label, "n": 0})
            continue
        pnls = [float(r.pnl_pct) for r in xs]
        wins = sum(1 for p in pnls if p > 0)
        out.append(
            {
                "bucket": label,
                "n": len(xs),
                "win_rate": wins / len(xs),
                "avg_pnl_pct": sum(pnls) / len(xs),
                "median_pnl_pct": sorted(pnls)[len(pnls) // 2],
                "avg_final_tip_gap_pct": sum(float(r.final_tip_gap_pct) for r in xs) / len(xs),
            }
        )
    return out


async def _fetch_bar_ohlc(
    exchange,
    symbol: str,
    bar_dt: datetime,
    timeframe: str = "1m",
) -> Optional[tuple[float, float, float, float]]:
    """取该分钟 K 的 open/high/low/close。"""
    aware = bar_dt if bar_dt.tzinfo else bar_dt.replace(tzinfo=BEIJING_TZ)
    since_ms = int(aware.astimezone(timezone.utc).timestamp() * 1000)
    try:
        rows = await exchange.fetch_klines(symbol, timeframe, limit=3, since=since_ms)
    except Exception as e:
        logger.debug("fetch bar ohlc %s %s: %s", symbol, bar_dt, e)
        return None
    if not rows:
        return None
    best = min(rows, key=lambda r: abs(int(r[0]) - since_ms))
    if abs(int(best[0]) - since_ms) > 90_000:
        return None
    return float(best[1]), float(best[2]), float(best[3]), float(best[4])


async def enrich_open_outcomes(
    report: WickLogReport,
    *,
    max_rows: int = 40,
    timeframe: str = "1m",
) -> dict:
    """对齐 trades + 最终针尖，返回结构化结果。"""
    paired = pair_opened_with_side(report)
    # 只要有开仓价/开盘价的
    candidates: list[tuple[EntryRow, str, EntryRow | None, datetime]] = []
    for op, side, tr in paired:
        dt = _parse_ts(op.ts)
        if dt is None:
            continue
        if not (op.px == op.px and op.px > 0):
            # 旧日志可能无 px，后面用 trade.entry
            pass
        candidates.append((op, side, tr, dt))
    # 新在前
    candidates.sort(key=lambda x: x[3], reverse=True)
    candidates = candidates[:max_rows]

    if not candidates:
        return {
            "n": 0,
            "matched_trades": 0,
            "kline_ok": 0,
            "final_tip_gap": {"n": 0},
            "capture_ratio": {"n": 0},
            "pnl_pct": {"n": 0},
            "pnl_by_tip_bucket": [],
            "rows": [],
            "text": "无开仓日志可对齐（需有 wick_spike opened 记录）",
        }

    strategy_ids = {op.strategy_id for op, _, _, _ in candidates}
    t_min = min(dt.replace(tzinfo=None) for _, _, _, dt in candidates) - timedelta(minutes=5)
    t_max = max(dt.replace(tzinfo=None) for _, _, _, dt in candidates) + timedelta(minutes=5)

    async with async_session() as session:
        result = await session.execute(
            select(Trade).where(
                Trade.strategy_id.in_(strategy_ids),
                Trade.entry_time >= t_min,
                Trade.entry_time <= t_max,
            )
        )
        trades = list(result.scalars().all())
        tf_result = await session.execute(
            select(Strategy.id, Strategy.timeframe).where(Strategy.id.in_(strategy_ids))
        )
        tf_map = {int(row[0]): (row[1] or "1m") for row in tf_result.all()}

    public = await get_public_binance()

    # 串行拉 K 线，避免打爆 REST；少量并发
    sem = asyncio.Semaphore(3)

    async def one(
        op: EntryRow, side: str, tr: EntryRow | None, dt: datetime
    ) -> OpenOutcome:
        trade = match_trade(
            trades,
            strategy_id=op.strategy_id,
            symbol=op.symbol,
            side=side,
            event_dt=dt,
        )
        if trade and not side:
            side = (trade.side or "").lower()
        if trade is not None:
            entry_px = float(trade.entry_price)
        elif op.px == op.px and op.px > 0:
            entry_px = float(op.px)
        else:
            entry_px = float("nan")
        log_open = float(op.open) if op.open == op.open and op.open > 0 else float("nan")
        bar_open = log_open

        note_parts = []
        if not trade:
            note_parts.append("未匹配到成交")
        if not side:
            note_parts.append("缺方向")

        final_ext = tip_final = wick = capture = float("nan")
        kline_ok = False
        tf = tf_map.get(op.strategy_id, timeframe)
        bar_dt = _bar_open_dt(dt, tf)

        async with sem:
            ohlc = None
            if side and (
                (log_open == log_open and log_open > 0)
                or (entry_px == entry_px and entry_px > 0)
                or trade is not None
            ):
                try:
                    ohlc = await _fetch_bar_ohlc(public, op.symbol, bar_dt, tf)
                except Exception as e:
                    note_parts.append(f"K线失败:{e}")
                    ohlc = None

        trig_ext = op.ext if op.ext == op.ext else None
        # 触发贴尖：信号触发价 vs 触发时极值（看决策质量）
        # 最终贴尖：成交均价 vs 收盘后 K 线极值（看执行质量）
        trig_gap = None
        signal_px = float(op.px) if op.px == op.px and op.px > 0 else float("nan")
        if ohlc:
            o, h, l, _c = ohlc
            # 最终贴尖一律用交易所 K 线 O/H/L，禁止混用日志 open
            bar_open = o
            if entry_px == entry_px and entry_px > 0 and bar_open > 0 and side:
                final_ext, tip_final, wick, capture = _final_tip_metrics(
                    side, bar_open, entry_px, h, l
                )
                kline_ok = True
            else:
                note_parts.append("无法算最终针尖")
            if (
                signal_px == signal_px
                and signal_px > 0
                and trig_ext is not None
                and bar_open > 0
            ):
                trig_gap = tip_gap_pct(bar_open, float(trig_ext), signal_px)
            elif op.tip_gap_pct == op.tip_gap_pct:
                trig_gap = float(op.tip_gap_pct)
        else:
            note_parts.append("无K线")
            if op.tip_gap_pct == op.tip_gap_pct:
                trig_gap = float(op.tip_gap_pct)

        detect_ms = None
        if tr is not None:
            detect_ms = _finite_ms(tr.detect_to_lock_ms)
        trade_age = op.trade_age_ms if op.trade_age_ms >= 0 else None
        open_api = _finite_ms(op.open_api_ms)
        sig_ord = _finite_ms(op.signal_to_order_ms)

        vol_x = _finite_f(op.vol_x)
        need_x = _finite_f(op.need_x)
        progress = _finite_f(op.progress)
        atr_n = _finite_f(op.atr_n)
        if atr_n is None and tr is not None:
            atr_n = _finite_f(tr.atr_n)
        if vol_x is None and tr is not None:
            vol_x = _finite_f(tr.vol_x)
        if need_x is None and tr is not None:
            need_x = _finite_f(tr.need_x)
        if progress is None and tr is not None:
            progress = _finite_f(tr.progress)

        return OpenOutcome(
            ts=op.ts,
            strategy_id=op.strategy_id,
            symbol=op.symbol,
            side=side,
            side_zh=_side_zh(side),
            entry_px=entry_px if entry_px == entry_px else float("nan"),
            bar_open=bar_open if bar_open == bar_open else float("nan"),
            trigger_ext=trig_ext,
            tip_gap_at_trigger_pct=trig_gap,
            final_ext=final_ext if final_ext == final_ext else None,
            final_tip_gap_pct=tip_final if tip_final == tip_final else None,
            wick_range_pct=wick if wick == wick else None,
            capture_ratio=capture if capture == capture else None,
            vol_x=vol_x,
            need_x=need_x,
            progress=progress,
            atr_n=atr_n,
            trade_age_ms=trade_age,
            detect_to_lock_ms=detect_ms,
            open_api_ms=open_api,
            signal_to_order_ms=sig_ord,
            trade_id=trade.id if trade else None,
            trade_entry=float(trade.entry_price) if trade else None,
            trade_exit=float(trade.exit_price) if trade else None,
            realized_pnl=float(trade.realized_pnl) if trade else None,
            pnl_pct=float(trade.pnl_pct) if trade else None,
            close_reason=trade.close_reason if trade else None,
            layer=(int(trade.layer) if trade.layer is not None else 0) if trade else None,
            matched=trade is not None,
            kline_ok=kline_ok,
            note="；".join(note_parts),
        )

    outcomes = list(
        await asyncio.gather(*[one(op, side, tr, dt) for op, side, tr, dt in candidates])
    )

    final_gaps = [
        float(r.final_tip_gap_pct)
        for r in outcomes
        if r.final_tip_gap_pct is not None
    ]
    captures = [
        float(r.capture_ratio) for r in outcomes if r.capture_ratio is not None
    ]
    pnls = [float(r.pnl_pct) for r in outcomes if r.matched and r.pnl_pct is not None]
    buckets = _pnl_buckets(outcomes)

    sig_ords = [
        float(r.signal_to_order_ms)
        for r in outcomes
        if r.signal_to_order_ms is not None
    ]
    open_apis = [
        float(r.open_api_ms) for r in outcomes if r.open_api_ms is not None
    ]

    text_lines = [
        "=== 开仓质量：相对最终针尖 + 盈亏 ===",
        f"分析开仓 {len(outcomes)} 笔；匹配成交 {sum(1 for r in outcomes if r.matched)}；"
        f"拿到收盘K线 {sum(1 for r in outcomes if r.kline_ok)}",
        "",
        "--- 相对本根最终针尖（收盘后高低）---",
        f"距最终针尖%: { _fmt_summary(summarize_gaps(final_gaps), '%') }",
        f"针尖捕获率(1=贴尖): { _fmt_summary(summarize_gaps(captures), '') }",
        "",
        "--- 信号→下单速度 ---",
        f"信号→下单完成: { _fmt_summary(summarize_gaps(sig_ords), 'ms') }",
        f"下单(含账户锁): { _fmt_summary(summarize_gaps(open_apis), 'ms') }",
        "",
        "--- 与盈亏对齐 ---",
        f"已平仓盈亏%: { _fmt_summary(summarize_gaps(pnls), '%') }",
        "按「距最终针尖」分桶（看贴尖是否对应更好盈亏）:",
    ]
    for b in buckets:
        if b["n"] == 0:
            text_lines.append(f"  {b['bucket']}: 无样本")
        else:
            text_lines.append(
                f"  {b['bucket']}: n={b['n']} 胜率{b['win_rate']*100:.0f}% "
                f"均盈亏{b['avg_pnl_pct']:.3f}% 中位{b['median_pnl_pct']:.3f}%"
            )

    rows_out = []
    for r in outcomes:
        rows_out.append(
            {
                "ts": r.ts,
                "strategy_id": r.strategy_id,
                "symbol": r.symbol,
                "side": r.side,
                "side_zh": r.side_zh,
                "entry_px": _round(r.entry_px),
                "bar_open": _round(r.bar_open),
                "tip_gap_at_trigger_pct": _round(r.tip_gap_at_trigger_pct),
                "final_tip_gap_pct": _round(r.final_tip_gap_pct),
                "wick_range_pct": _round(r.wick_range_pct),
                "capture_ratio": _round(r.capture_ratio),
                "vol_x": _round(r.vol_x, 3),
                "need_x": _round(r.need_x, 3),
                "progress": _round(r.progress, 3),
                "atr_n": _round(r.atr_n, 6),
                "trade_age_ms": r.trade_age_ms,
                "detect_to_lock_ms": _round(r.detect_to_lock_ms, 1),
                "open_api_ms": _round(r.open_api_ms, 1),
                "signal_to_order_ms": _round(r.signal_to_order_ms, 1),
                "trade_id": r.trade_id,
                "realized_pnl": _round(r.realized_pnl),
                "pnl_pct": _round(r.pnl_pct),
                "close_reason": r.close_reason,
                "layer": r.layer,
                "matched": r.matched,
                "kline_ok": r.kline_ok,
                "note": r.note,
            }
        )

    return {
        "n": len(outcomes),
        "matched_trades": sum(1 for r in outcomes if r.matched),
        "kline_ok": sum(1 for r in outcomes if r.kline_ok),
        "final_tip_gap": summarize_gaps(final_gaps),
        "capture_ratio": summarize_gaps(captures),
        "pnl_pct": summarize_gaps(pnls),
        "signal_to_order_ms": summarize_gaps(sig_ords),
        "open_api_ms": summarize_gaps(open_apis),
        "pnl_by_tip_bucket": buckets,
        "rows": rows_out,
        "text": "\n".join(text_lines),
    }


def _round(v: float | None, nd: int = 4) -> float | None:
    if v is None:
        return None
    if v != v:
        return None
    return round(float(v), nd)


def _fmt_summary(s: dict, unit: str) -> str:
    if not s.get("n"):
        return "暂无数据"
    if unit == "%":
        return (
            f"样本{int(s['n'])} 均值{s['mean']:.3f}% "
            f"中位{s['p50']:.3f}% P90={s['p90']:.3f}%"
        )
    if unit == "ms":
        return (
            f"样本{int(s['n'])} 均值{s['mean']:.0f}ms "
            f"中位{s['p50']:.0f}ms P90={s['p90']:.0f}ms"
        )
    return (
        f"样本{int(s['n'])} 均值{s['mean']:.3f} "
        f"中位{s['p50']:.3f} P90={s['p90']:.3f}"
    )
