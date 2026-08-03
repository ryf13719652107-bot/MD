"""接针日志统计分析 API。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query

from ..services.wick_spike_log_stats import (
    analyze_paths,
    build_analysis,
    resolve_bot_log_paths,
)
from ..services.wick_spike_outcome import enrich_open_outcomes

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/wick-stats", tags=["wick-stats"])


@router.get("/analyze")
async def analyze_wick_spike_logs(
    progress_min: float = Query(1.5, ge=0, le=20, description="深针 progress 下限"),
    list_limit: int = Query(80, ge=1, le=500, description="清单最多条数"),
    include_rotated: bool = Query(True, description="是否包含 bot.log.* 轮转文件"),
    enrich_opens: bool = Query(True, description="对齐成交+最终针尖+盈亏"),
    max_enrich: int = Query(40, ge=1, le=100, description="最多对齐多少笔开仓"),
):
    """扫描 bot.log，返回接针贴尖统计、量能挡住清单，以及开仓相对最终针尖/盈亏。"""
    paths = resolve_bot_log_paths(include_rotated=include_rotated)
    if not paths:
        return {
            "ok": False,
            "error": "未找到 logs/bot.log（请确认后端工作目录下有日志）",
            "log_files": [],
            "near_miss_total": 0,
            "entry_total": 0,
            "text": "未找到日志文件",
        }

    try:
        report = analyze_paths(paths)
        data = build_analysis(report, progress_min=progress_min, list_limit=list_limit)
        data["ok"] = True
        data["error"] = None
        data["log_files"] = [str(p) for p in paths]

        if enrich_opens:
            try:
                outcome = await enrich_open_outcomes(report, max_rows=max_enrich)
                data["open_quality"] = outcome
                data["text"] = data.get("text", "") + "\n\n" + outcome.get("text", "")
            except Exception as e:
                logger.exception("open quality enrich failed: %s", e)
                data["open_quality"] = {
                    "n": 0,
                    "error": str(e),
                    "text": f"开仓质量对齐失败: {e}",
                    "rows": [],
                    "pnl_by_tip_bucket": [],
                }
                data["text"] = data.get("text", "") + f"\n\n开仓质量对齐失败: {e}"
        else:
            data["open_quality"] = None

        return data
    except Exception as e:
        logger.exception("wick-stats analyze failed: %s", e)
        return {
            "ok": False,
            "error": str(e),
            "log_files": [str(p) for p in paths],
            "text": f"分析失败: {e}",
        }
