"""解析 bot.log 中的接针行，统计进场贴尖程度与量能挡住的深针 near-miss。"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Optional


_TS = r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"

_NEAR_MISS_RE = re.compile(
    _TS
    + r".*wick_spike near-miss strategy=(?P<sid>\d+)\s+(?P<sym>\S+)\s+"
    + r"dir=(?P<dir>\w+)\s+px=(?P<px>[^\s]+)\s+open=(?P<open>[^\s]+)\s+"
    + r"ext=(?P<ext>[^\s]+)\s+thr=(?P<thr>[^\s]+)\s+pierce=(?P<pierce>\w+)\s+"
    + r"atrN=(?P<atrN>[^\s]+)\s+progress=(?P<progress>[^\s]+)"
    + r"(?:\s+amp%=(?P<amp>[^\s]+))?"
    + r"\s+vol×=(?P<vol>[^\s]+)\s+need×=(?P<need>[^\s]+)\s+vol_hot=(?P<vol_hot>\w+)"
)

# 新格式（含 tip_gap / open / ext / progress）
_TRIGGER_RE = re.compile(
    _TS
    + r".*wick_spike trigger strategy=(?P<sid>\d+)\s+(?P<sym>\S+)\s+(?P<side>\w+)\s+"
    + r"px=(?P<px>[^\s]+)"
    + r"(?:\s+open=(?P<open>[^\s]+)\s+ext=(?P<ext>[^\s]+)\s+atrN=(?P<atrN>[^\s]+)"
    + r"\s+progress=(?P<progress>[^\s]+)\s+tip_gap%=(?P<tip_gap>[^\s]+))?"
    + r"\s+vol×=(?P<vol>[^\s]+)"
    + r"(?:\s+need×=(?P<need>[^\s]+))?"
    + r"(?:\s+trade_age_ms=(?P<age>-?\d+))?"
    + r"(?:\s+detect_to_lock_ms=(?P<detect_ms>[^\s]+))?"
)

_OPENED_RE = re.compile(
    _TS
    + r".*wick_spike opened strategy=(?P<sid>\d+)\s+(?P<sym>\S+)\s+"
    + r"open_api_db_ms=(?P<open_ms>[^\s]+)\s+trade_age_ms=(?P<age>-?\d+)"
    + r"(?:\s+px=(?P<px>[^\s]+)\s+open=(?P<open>[^\s]+)\s+ext=(?P<ext>[^\s]+)"
    + r"\s+progress=(?P<progress>[^\s]+)\s+tip_gap%=(?P<tip_gap>[^\s]+))?"
    + r"\s+vol×=(?P<vol>[^\s]+)"
    + r"(?:\s+need×=(?P<need>[^\s]+))?"
)


def _f(v: Optional[str], default: float = float("nan")) -> float:
    if v is None or v == "":
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _b(v: Optional[str]) -> bool:
    return str(v).lower() in ("true", "1", "yes")


@dataclass
class NearMissRow:
    ts: str
    strategy_id: int
    symbol: str
    direction: str
    px: float
    open: float
    ext: float
    thr: float
    pierce: bool
    atr_n: float
    progress: float
    vol_x: float
    need_x: float
    vol_hot: bool

    @property
    def tip_gap_pct(self) -> float:
        if self.open <= 0:
            return float("nan")
        return abs(self.px - self.ext) / self.open * 100.0

    @property
    def vol_shortfall(self) -> float:
        return self.need_x - self.vol_x


@dataclass
class EntryRow:
    kind: str  # trigger | opened
    ts: str
    strategy_id: int
    symbol: str
    side: str = ""
    px: float = float("nan")
    open: float = float("nan")
    ext: float = float("nan")
    progress: float = float("nan")
    tip_gap_pct: float = float("nan")
    vol_x: float = float("nan")
    need_x: float = float("nan")
    trade_age_ms: int = -1
    open_api_db_ms: float = float("nan")
    detect_to_lock_ms: float = float("nan")


@dataclass
class WickLogReport:
    near_misses: list[NearMissRow] = field(default_factory=list)
    entries: list[EntryRow] = field(default_factory=list)

    def vol_blocked_deep(
        self, *, progress_min: float = 1.5, pierce_only: bool = True
    ) -> list[NearMissRow]:
        out = []
        for r in self.near_misses:
            if r.vol_hot:
                continue
            if r.progress < progress_min:
                continue
            if pierce_only and not r.pierce:
                continue
            out.append(r)
        return out


def iter_log_lines(paths: Iterable[Path]) -> Iterator[str]:
    for path in paths:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "wick_spike" in line:
                    yield line.rstrip("\n")


def parse_line(line: str) -> Optional[object]:
    m = _NEAR_MISS_RE.search(line)
    if m:
        return NearMissRow(
            ts=m.group("ts"),
            strategy_id=int(m.group("sid")),
            symbol=m.group("sym"),
            direction=m.group("dir"),
            px=_f(m.group("px")),
            open=_f(m.group("open")),
            ext=_f(m.group("ext")),
            thr=_f(m.group("thr")),
            pierce=_b(m.group("pierce")),
            atr_n=_f(m.group("atrN")),
            progress=_f(m.group("progress")),
            vol_x=_f(m.group("vol")),
            need_x=_f(m.group("need")),
            vol_hot=_b(m.group("vol_hot")),
        )
    m = _OPENED_RE.search(line)
    if m:
        return EntryRow(
            kind="opened",
            ts=m.group("ts"),
            strategy_id=int(m.group("sid")),
            symbol=m.group("sym"),
            px=_f(m.group("px")),
            open=_f(m.group("open")),
            ext=_f(m.group("ext")),
            progress=_f(m.group("progress")),
            tip_gap_pct=_f(m.group("tip_gap")),
            vol_x=_f(m.group("vol")),
            need_x=_f(m.group("need")),
            trade_age_ms=int(m.group("age") or -1),
            open_api_db_ms=_f(m.group("open_ms")),
        )
    m = _TRIGGER_RE.search(line)
    if m:
        return EntryRow(
            kind="trigger",
            ts=m.group("ts"),
            strategy_id=int(m.group("sid")),
            symbol=m.group("sym"),
            side=m.group("side") or "",
            px=_f(m.group("px")),
            open=_f(m.group("open")),
            ext=_f(m.group("ext")),
            progress=_f(m.group("progress")),
            tip_gap_pct=_f(m.group("tip_gap")),
            vol_x=_f(m.group("vol")),
            need_x=_f(m.group("need")),
            trade_age_ms=int(m.group("age") or -1),
            detect_to_lock_ms=_f(m.group("detect_ms")),
        )
    return None


def analyze_paths(paths: Iterable[Path]) -> WickLogReport:
    report = WickLogReport()
    for line in iter_log_lines(paths):
        row = parse_line(line)
        if isinstance(row, NearMissRow):
            report.near_misses.append(row)
        elif isinstance(row, EntryRow):
            report.entries.append(row)
    return report


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def summarize_gaps(values: list[float]) -> dict[str, float]:
    vals = sorted(v for v in values if v == v)  # drop nan
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "min": vals[0],
        "p25": _percentile(vals, 0.25),
        "p50": _percentile(vals, 0.50),
        "p75": _percentile(vals, 0.75),
        "p90": _percentile(vals, 0.90),
        "max": vals[-1],
        "mean": sum(vals) / len(vals),
    }


def _summary_zh(s: dict[str, float], *, unit: str = "") -> str:
    if not s.get("n"):
        return "暂无数据"
    u = unit
    if u == "ms":
        return (
            f"样本{int(s['n'])} 均值{s['mean']:.0f}ms "
            f"中位{s['p50']:.0f}ms P90={s['p90']:.0f}ms 最大{s['max']:.0f}ms"
        )
    if u == "%":
        return (
            f"样本{int(s['n'])} 均值{s['mean']:.3f}% "
            f"中位{s['p50']:.3f}% P90={s['p90']:.3f}% 最大{s['max']:.3f}%"
        )
    return (
        f"样本{int(s['n'])} 均值{s['mean']:.2f} "
        f"中位{s['p50']:.2f} P90={s['p90']:.2f} 最大{s['max']:.2f}"
    )


def _dir_zh(d: str) -> str:
    x = (d or "").lower()
    if x in ("long", "buy"):
        return "做多"
    if x in ("short", "sell"):
        return "做空"
    return d or "-"


def format_report(
    report: WickLogReport,
    *,
    progress_min: float = 1.5,
    list_limit: int = 50,
) -> str:
    data = build_analysis(report, progress_min=progress_min, list_limit=list_limit)
    return str(data["text"])


def build_analysis(
    report: WickLogReport,
    *,
    progress_min: float = 1.5,
    list_limit: int = 80,
) -> dict:
    """结构化分析结果（API / 前端用）+ text 纯文本报告。"""
    deep = report.vol_blocked_deep(progress_min=progress_min, pierce_only=True)
    dedup: dict[tuple, NearMissRow] = {}
    for r in deep:
        key = (r.strategy_id, r.symbol, r.ts[:16])
        prev = dedup.get(key)
        if prev is None or r.progress > prev.progress or (
            r.progress == prev.progress and r.vol_shortfall < prev.vol_shortfall
        ):
            dedup[key] = r
    deep_u = sorted(dedup.values(), key=lambda x: (x.ts, x.symbol))

    reason: Counter[str] = Counter()
    for r in report.near_misses:
        if r.pierce and not r.vol_hot:
            reason["已刺破但量能不够"] += 1
        elif (not r.pierce) and r.vol_hot:
            reason["量能够但未刺破"] += 1
        elif (not r.pierce) and (not r.vol_hot):
            reason["未刺破且量能不够"] += 1
        else:
            reason["其它近阈值"] += 1

    opened = [e for e in report.entries if e.kind == "opened"]
    triggers = [e for e in report.entries if e.kind == "trigger"]
    tip_opened = summarize_gaps([e.tip_gap_pct for e in opened])
    tip_trigger = summarize_gaps([e.tip_gap_pct for e in triggers])

    # 速度：成交年龄（trigger+opened）、抢锁（trigger）、下单写库（opened）
    trade_ages = [
        float(e.trade_age_ms)
        for e in report.entries
        if e.trade_age_ms >= 0
    ]
    detect_locks = [
        e.detect_to_lock_ms
        for e in triggers
        if e.detect_to_lock_ms == e.detect_to_lock_ms and e.detect_to_lock_ms >= 0
    ]
    open_dbs = [
        e.open_api_db_ms
        for e in opened
        if e.open_api_db_ms == e.open_api_db_ms and e.open_api_db_ms >= 0
    ]
    trade_age_stat = summarize_gaps(trade_ages)
    detect_lock_stat = summarize_gaps(detect_locks)
    open_db_stat = summarize_gaps(open_dbs)

    shortfalls = [r.vol_shortfall for r in deep_u]
    vols = [r.vol_x for r in deep_u]
    progs = [r.progress for r in deep_u]
    would_pass_5 = sum(1 for r in deep_u if r.vol_x >= 5.0)
    would_pass_45 = sum(1 for r in deep_u if r.vol_x >= 4.5)

    rows = [
        {
            "ts": r.ts,
            "strategy_id": r.strategy_id,
            "symbol": r.symbol,
            "direction": r.direction,
            "direction_zh": _dir_zh(r.direction),
            "progress": round(r.progress, 4),
            "vol_x": round(r.vol_x, 4),
            "need_x": round(r.need_x, 4),
            "vol_shortfall": round(r.vol_shortfall, 4),
            "tip_gap_pct": round(r.tip_gap_pct, 4) if r.tip_gap_pct == r.tip_gap_pct else None,
            "px": r.px,
            "open": r.open,
            "ext": r.ext,
        }
        for r in deep_u[:list_limit]
    ]

    text_lines = [
        "=== 接针日志分析 ===",
        f"近失记录: {len(report.near_misses)} 条",
        f"触发记录: {len(triggers)} 条 / 开仓成功: {len(opened)} 条",
        "",
        "--- 进场距针尖（占开盘价%，越小越贴）---",
    ]
    if tip_opened.get("n", 0):
        text_lines.append(f"开仓成功: {_summary_zh(tip_opened, unit='%')}")
    else:
        text_lines.append("开仓成功: 暂无贴尖字段（部署含新日志后才有）")
    if tip_trigger.get("n", 0):
        text_lines.append(f"信号触发: {_summary_zh(tip_trigger, unit='%')}")
    else:
        text_lines.append("信号触发: 暂无贴尖字段（部署含新日志后才有）")

    text_lines.append("")
    text_lines.append("--- 接针速度（毫秒）---")
    text_lines.append(f"成交推送年龄: {_summary_zh(trade_age_stat, unit='ms')}")
    text_lines.append(f"检出→抢锁: {_summary_zh(detect_lock_stat, unit='ms')}")
    text_lines.append(f"下单+写库: {_summary_zh(open_db_stat, unit='ms')}")

    text_lines.append("")
    text_lines.append("--- 近失卡点分布 ---")
    for k, v in reason.most_common():
        text_lines.append(f"  {k}: {v}")
    text_lines.append("")
    text_lines.append(
        f"--- 深针被量能挡住（已刺破、刺破进度≥{progress_min}、量能未达标）---"
    )
    text_lines.append(f"去重后条数: {len(deep_u)}（同策略+币种+分钟只计一次）")
    if deep_u:
        sv = summarize_gaps(vols)
        ss = summarize_gaps(shortfalls)
        sp = summarize_gaps(progs)
        text_lines.append(
            f"实际量能倍数: {_summary_zh(sv)} | "
            f"距门槛缺口: {_summary_zh(ss)} | "
            f"刺破进度: {_summary_zh(sp)}"
        )
        text_lines.append(
            f"若量能门槛改为 5 倍可过 {would_pass_5}/{len(deep_u)}；"
            f"改为 4.5 倍可过 {would_pass_45}/{len(deep_u)}"
        )
        text_lines.append("")
        text_lines.append(
            f"{'时间':19} {'策略':>4} {'币种':12} {'方向':4} "
            f"{'进度':>5} {'量能':>5} {'门槛':>5} {'缺口':>5} {'贴尖%':>6}"
        )
        for r in deep_u[:list_limit]:
            text_lines.append(
                f"{r.ts:19} {r.strategy_id:4d} {r.symbol:12} {_dir_zh(r.direction):4} "
                f"{r.progress:5.2f} {r.vol_x:5.2f} {r.need_x:5.3g} "
                f"{r.vol_shortfall:5.2f} {r.tip_gap_pct:6.3f}"
            )
        if len(deep_u) > list_limit:
            text_lines.append(f"... 另有 {len(deep_u) - list_limit} 条未列出")
    else:
        text_lines.append("（无）")

    return {
        "near_miss_total": len(report.near_misses),
        "entry_total": len(report.entries),
        "opened_total": len(opened),
        "trigger_total": len(triggers),
        "block_reasons": dict(reason),
        "tip_gap_opened": tip_opened,
        "tip_gap_trigger": tip_trigger,
        "trade_age_ms": trade_age_stat,
        "detect_to_lock_ms": detect_lock_stat,
        "open_api_db_ms": open_db_stat,
        "speed_labels": {
            "trade_age_ms": "成交推送年龄",
            "detect_to_lock_ms": "检出→抢锁",
            "open_api_db_ms": "下单+写库",
        },
        "vol_blocked_deep": {
            "progress_min": progress_min,
            "count": len(deep_u),
            "listed": len(rows),
            "vol_summary": summarize_gaps(vols),
            "shortfall_summary": summarize_gaps(shortfalls),
            "progress_summary": summarize_gaps(progs),
            "counterfactual": {
                "need_5_pass": would_pass_5,
                "need_4_5_pass": would_pass_45,
                "total": len(deep_u),
            },
            "rows": rows,
        },
        "text": "\n".join(text_lines),
    }


def resolve_bot_log_paths(*, include_rotated: bool = True) -> list[Path]:
    """定位 backend/logs/bot.log（兼容 cwd=backend 或项目根）。"""
    roots = [
        Path.cwd(),
        Path.cwd() / "backend",
        Path(__file__).resolve().parents[2],  # backend/
    ]
    seen: set[Path] = set()
    out: list[Path] = []
    for root in roots:
        log_dir = (root / "logs").resolve()
        main = log_dir / "bot.log"
        if not main.is_file():
            continue
        for p in [main, *sorted(log_dir.glob("bot.log.*"))] if include_rotated else [main]:
            if p.is_file():
                rp = p.resolve()
                if rp not in seen:
                    seen.add(rp)
                    out.append(rp)
        if out:
            break
    return out
