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
    # open_api_ms=仅下单；旧日志 open_api_db_ms=含写库，解析兼容
    + r"(?:open_api_ms|open_api_db_ms)=(?P<open_ms>[^\s]+)"
    + r"(?:\s+signal_to_order_ms=(?P<sig_ord>[^\s]+))?"
    + r"\s+trade_age_ms=(?P<age>-?\d+)"
    + r"(?:\s+px=(?P<px>[^\s]+)\s+open=(?P<open>[^\s]+)\s+ext=(?P<ext>[^\s]+)"
    + r"(?:\s+atrN=(?P<atrN>[^\s]+))?"
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
    amp_pct: float = float("nan")  # 极值相对开盘涨跌幅 %

    @property
    def tip_gap_pct(self) -> float:
        if self.open <= 0:
            return float("nan")
        return abs(self.px - self.ext) / self.open * 100.0

    @property
    def vol_shortfall(self) -> float:
        return self.need_x - self.vol_x

    @property
    def move_pct(self) -> float:
        if self.amp_pct == self.amp_pct:
            return self.amp_pct
        if self.open <= 0:
            return float("nan")
        d = (self.direction or "").lower()
        if d == "short":
            return max(0.0, (self.ext - self.open) / self.open * 100.0)
        if d == "long":
            return max(0.0, (self.open - self.ext) / self.open * 100.0)
        return abs(self.ext - self.open) / self.open * 100.0


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
    atr_n: float = float("nan")  # 刺破阈值 N=ATR×倍数（价格单位）
    trade_age_ms: int = -1
    open_api_ms: float = float("nan")  # 仅下单（旧字段名 open_api_db_ms 亦写入此）
    signal_to_order_ms: float = float("nan")  # 捕捉信号 → 下单返回
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
            amp_pct=_f(m.groupdict().get("amp")),
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
            atr_n=_f(m.groupdict().get("atrN")),
            trade_age_ms=int(m.group("age") or -1),
            open_api_ms=_f(m.group("open_ms")),
            signal_to_order_ms=_f(m.group("sig_ord")),
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
            atr_n=_f(m.group("atrN")),
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


def filter_report_by_strategies(
    report: WickLogReport, strategy_ids: set[int]
) -> WickLogReport:
    """只保留指定策略的接针日志行。"""
    if not strategy_ids:
        return WickLogReport()
    return WickLogReport(
        near_misses=[r for r in report.near_misses if r.strategy_id in strategy_ids],
        entries=[e for e in report.entries if e.strategy_id in strategy_ids],
    )


_STRATEGY_ID_RE = re.compile(r"strategy=(\d+)\b")


def clear_wick_spike_lines(
    paths: Iterable[Path],
    *,
    strategy_ids: set[int] | None = None,
) -> dict:
    """
    从 bot.log（及轮转文件）删除接针相关行。

    strategy_ids 为空/None：删除全部含 wick_spike 的行；
    否则只删 strategy=ID 落在集合内的接针行。
    """
    files_touched = 0
    lines_removed = 0
    for path in paths:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        out_lines: list[str] = []
        removed = 0
        for line in text.splitlines(keepends=True):
            raw = line.rstrip("\r\n")
            if "wick_spike" not in raw:
                out_lines.append(line)
                continue
            if strategy_ids is None:
                removed += 1
                continue
            m = _STRATEGY_ID_RE.search(raw)
            if m and int(m.group(1)) in strategy_ids:
                removed += 1
                continue
            out_lines.append(line)
        if removed == 0:
            continue
        # 必须原地截断写入：tmp+replace 会换 inode，运行中的
        # RotatingFileHandler 仍握着旧 FD，新日志会写丢、统计读到空文件。
        try:
            with path.open("w", encoding="utf-8", newline="") as f:
                f.write("".join(out_lines))
        except OSError:
            continue
        files_touched += 1
        lines_removed += removed
    return {"files_touched": files_touched, "lines_removed": lines_removed}


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
    open_apis = [
        e.open_api_ms
        for e in opened
        if e.open_api_ms == e.open_api_ms and e.open_api_ms >= 0
    ]
    signal_to_orders = [
        e.signal_to_order_ms
        for e in opened
        if e.signal_to_order_ms == e.signal_to_order_ms and e.signal_to_order_ms >= 0
    ]
    trade_age_stat = summarize_gaps(trade_ages)
    detect_lock_stat = summarize_gaps(detect_locks)
    open_api_stat = summarize_gaps(open_apis)
    signal_to_order_stat = summarize_gaps(signal_to_orders)

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
    text_lines.append(f"信号→下单完成: {_summary_zh(signal_to_order_stat, unit='ms')}")
    text_lines.append(f"下单: {_summary_zh(open_api_stat, unit='ms')}")

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
        "signal_to_order_ms": signal_to_order_stat,
        "open_api_ms": open_api_stat,
        # 兼容旧前端字段名
        "open_api_db_ms": open_api_stat,
        "speed_labels": {
            "trade_age_ms": "成交推送年龄",
            "detect_to_lock_ms": "检出→抢锁",
            "signal_to_order_ms": "信号→下单完成",
            "open_api_ms": "下单",
            "open_api_db_ms": "下单",
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


def _norm_sym_key(s: str) -> str:
    """统一为 XXXUSDT，便于 BLESS / BLESSUSDT 互匹配。"""
    k = (s or "").upper().replace("/", "").replace(":USDT", "").replace("_", "")
    if k and not k.endswith("USDT"):
        k = f"{k}USDT"
    return k


def _near_miss_reason_zh(r: NearMissRow) -> str:
    """把 near-miss 字段翻译成可读卡点。"""
    if r.pierce and r.vol_x < r.need_x:
        return (
            f"已刺破但量能不足：vol×={r.vol_x:.2f} < need×={r.need_x:g}"
            f"（还差 {max(0.0, r.need_x - r.vol_x):.2f}×）"
        )
    if (not r.pierce) and r.vol_x >= r.need_x:
        return (
            f"量能够但深度不够：progress={r.progress:.2f}（需≥1 才刺破）"
            f"；极值={r.ext:.6g} 刺破线={r.thr:.6g}"
        )
    if (not r.pierce) and r.vol_x < r.need_x:
        return (
            f"深度与量能都不够：progress={r.progress:.2f}（需≥1）；"
            f"vol×={r.vol_x:.2f} < need×={r.need_x:g}"
        )
    if r.pierce and r.vol_x >= r.need_x:
        mv = r.move_pct
        if mv == mv:
            return (
                f"已刺破且量能达标但未开仓：amp%={mv:.2f}"
                f"（可能未达最小涨跌幅/节流/占锁/同根已开）"
            )
        return "接近触发（已刺破且量能达标，可能被节流/占锁/同根已开）"
    return "接近但未开仓（详见 progress / vol×）"


def build_symbol_monitor(
    report: WickLogReport,
    symbol: str,
    *,
    list_limit: int = 120,
) -> dict:
    """按币种汇总接针监控时间线，解释为何未满足条件。"""
    key = _norm_sym_key(symbol)
    if not key:
        return {
            "symbol": "",
            "symbol_norm": "",
            "near_miss_n": 0,
            "trigger_n": 0,
            "opened_n": 0,
            "reason_counts": {},
            "rows": [],
            "note": "请输入币种，如 BLESS 或 BLESSUSDT",
        }

    events: list[dict] = []
    reason_counts: Counter[str] = Counter()

    for r in report.near_misses:
        if _norm_sym_key(r.symbol) != key:
            continue
        why = _near_miss_reason_zh(r)
        reason_counts[why.split("：")[0]] += 1
        events.append(
            {
                "ts": r.ts,
                "kind": "near_miss",
                "kind_zh": "近失",
                "strategy_id": r.strategy_id,
                "symbol": r.symbol,
                "direction": r.direction,
                "direction_zh": _dir_zh(r.direction),
                "px": r.px,
                "open": r.open,
                "ext": r.ext,
                "thr": r.thr,
                "pierce": r.pierce,
                "progress": r.progress,
                "atr_n": r.atr_n,
                "vol_x": r.vol_x,
                "need_x": r.need_x,
                "tip_gap_pct": r.tip_gap_pct if r.tip_gap_pct == r.tip_gap_pct else None,
                "reason": why,
            }
        )

    for e in report.entries:
        if _norm_sym_key(e.symbol) != key:
            continue
        if e.kind == "trigger":
            why = "条件已满足 → 触发下单"
            kind_zh = "触发"
        else:
            why = "已开仓成交"
            kind_zh = "开仓"
        reason_counts[kind_zh] += 1
        events.append(
            {
                "ts": e.ts,
                "kind": e.kind,
                "kind_zh": kind_zh,
                "strategy_id": e.strategy_id,
                "symbol": e.symbol,
                "direction": e.side,
                "direction_zh": _dir_zh(e.side),
                "px": e.px if e.px == e.px else None,
                "open": e.open if e.open == e.open else None,
                "ext": e.ext if e.ext == e.ext else None,
                "thr": None,
                "pierce": True if e.kind in ("trigger", "opened") else None,
                "progress": e.progress if e.progress == e.progress else None,
                "atr_n": e.atr_n if e.atr_n == e.atr_n else None,
                "vol_x": e.vol_x if e.vol_x == e.vol_x else None,
                "need_x": e.need_x if e.need_x == e.need_x else None,
                "tip_gap_pct": e.tip_gap_pct if e.tip_gap_pct == e.tip_gap_pct else None,
                "reason": why,
            }
        )

    events.sort(key=lambda x: x["ts"], reverse=True)
    listed = events[: max(1, list_limit)]
    near_n = sum(1 for x in events if x["kind"] == "near_miss")
    trig_n = sum(1 for x in events if x["kind"] == "trigger")
    open_n = sum(1 for x in events if x["kind"] == "opened")

    if not events:
        note = (
            f"日志中没有 {_norm_sym_key(symbol)} 的接针监控行。"
            "常见原因：未进选币池/未订阅、策略方向与针向不符、"
            "progress 过低（<0.5）未写 near-miss、或日志已清除/轮转。"
        )
    else:
        note = (
            f"共 {len(events)} 条（近失 {near_n} / 触发 {trig_n} / 开仓 {open_n}），"
            f"展示最近 {len(listed)} 条。无记录的分钟 = 当时未接近条件或未监控。"
        )

    return {
        "symbol": symbol.strip().upper(),
        "symbol_norm": key if key.endswith("USDT") else f"{key}USDT" if key else "",
        "near_miss_n": near_n,
        "trigger_n": trig_n,
        "opened_n": open_n,
        "reason_counts": dict(reason_counts),
        "listed": len(listed),
        "total": len(events),
        "rows": listed,
        "note": note,
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
