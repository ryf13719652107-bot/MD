#!/usr/bin/env python3
"""从 bot.log 统计接针：进场距极值 + 深针被量能挡住的 near-miss。

用法（在 backend 目录或项目根）:
  python -m app.services.wick_spike_log_stats
  python scripts/analyze_wick_spike_logs.py --log logs/bot.log --progress-min 1.5

东京服务器:
  cd /www/wwwroot/MD/backend
  python3 scripts/analyze_wick_spike_logs.py --log logs/bot.log*
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 允许直接 python scripts/... 时找到 app 包
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.services.wick_spike_log_stats import analyze_paths, format_report


def _expand_logs(patterns: list[str]) -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()
    for pat in patterns:
        p = Path(pat)
        matches = sorted(Path().glob(pat)) if any(c in pat for c in "*?[]") else []
        if not matches and p.exists():
            matches = [p]
        if not matches:
            # 相对 backend/logs
            alt = _BACKEND_ROOT / pat
            if any(c in pat for c in "*?[]"):
                matches = sorted(_BACKEND_ROOT.glob(pat))
            elif alt.exists():
                matches = [alt]
            else:
                matches = sorted((_BACKEND_ROOT / "logs").glob(Path(pat).name))
        for m in matches:
            rp = m.resolve()
            if rp not in seen and rp.is_file():
                seen.add(rp)
                out.append(rp)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="分析接针 bot.log")
    ap.add_argument(
        "--log",
        nargs="+",
        default=["logs/bot.log", "logs/bot.log.*"],
        help="日志文件或 glob，默认 logs/bot.log*",
    )
    ap.add_argument("--progress-min", type=float, default=1.5, help="深针 progress 下限")
    ap.add_argument("--list-limit", type=int, default=80, help="清单最多列出条数")
    args = ap.parse_args()

    paths = _expand_logs(args.log)
    if not paths:
        print("未找到日志文件，请用 --log 指定路径", file=sys.stderr)
        return 1

    print("读取:")
    for p in paths:
        print(f"  {p}")
    report = analyze_paths(paths)
    print()
    print(
        format_report(
            report,
            progress_min=args.progress_min,
            list_limit=args.list_limit,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
