"""接针日志统计分析 API。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models.account import Account
from ..models.strategy import Strategy
from ..services.log_service import strategy_log_service
from ..services.wick_spike_log_stats import (
    analyze_paths,
    build_analysis,
    build_symbol_monitor,
    clear_wick_spike_lines,
    filter_report_by_strategies,
    resolve_bot_log_paths,
)
from ..services.wick_spike_outcome import enrich_open_outcomes

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/wick-stats", tags=["wick-stats"])

# 信号源筛选：接针 / WT 系 / 该账户全部策略（历史接针行仍可能出现）
_SIGNAL_GROUPS: dict[str, set[str] | None] = {
    "wick_spike": {"wick_spike"},
    "wavetrend": {"wavetrend", "trend_wt"},
    "all": None,
}


def _annotate_strategy_meta(data: dict, meta: dict[int, dict]) -> None:
    """给清单/开仓行补策略名与信号源。"""
    deep = data.get("vol_blocked_deep") or {}
    for row in deep.get("rows") or []:
        info = meta.get(int(row.get("strategy_id") or 0)) or {}
        row["strategy_name"] = info.get("name")
        row["signal_source"] = info.get("signal_source")
        row["signal_source_zh"] = info.get("signal_source_zh")
    oq = data.get("open_quality")
    if not oq:
        return
    for row in oq.get("rows") or []:
        info = meta.get(int(row.get("strategy_id") or 0)) or {}
        row["strategy_name"] = info.get("name")
        row["signal_source"] = info.get("signal_source")
        row["signal_source_zh"] = info.get("signal_source_zh")


def _signal_source_zh(src: str) -> str:
    m = {
        "wick_spike": "接针",
        "wavetrend": "WaveTrend",
        "trend_wt": "趋势WT",
        "rsi": "RSI",
        "martingale_base": "马丁底仓",
    }
    return m.get(src or "", src or "-")


async def _load_account_strategies(
    db: AsyncSession,
    account_id: int,
    signal_source: str,
) -> tuple[Account, list[Strategy], set[int], dict[int, dict]]:
    acc = await db.get(Account, account_id)
    if not acc:
        raise HTTPException(status_code=404, detail=f"账户 {account_id} 不存在")

    result = await db.execute(
        select(Strategy).where(Strategy.account_id == account_id).order_by(Strategy.id)
    )
    strategies = list(result.scalars().all())
    if signal_source not in _SIGNAL_GROUPS:
        raise HTTPException(
            status_code=400,
            detail="signal_source 须为 wick_spike / wavetrend / all",
        )
    group = _SIGNAL_GROUPS[signal_source]
    if group is not None:
        strategies = [s for s in strategies if (s.signal_source or "") in group]

    meta: dict[int, dict] = {}
    ids: set[int] = set()
    for s in strategies:
        ids.add(int(s.id))
        src = s.signal_source or ""
        meta[int(s.id)] = {
            "name": s.name or f"策略{s.id}",
            "signal_source": src,
            "signal_source_zh": _signal_source_zh(src),
        }
    return acc, strategies, ids, meta


@router.get("/analyze")
async def analyze_wick_spike_logs(
    account_id: int = Query(..., description="账户 ID（必选）"),
    signal_source: str = Query(
        "wick_spike",
        description="策略信号源筛选：wick_spike | wavetrend | all",
    ),
    progress_min: float = Query(1.5, ge=0, le=20, description="深针 progress 下限"),
    list_limit: int = Query(80, ge=1, le=500, description="清单最多条数"),
    include_rotated: bool = Query(True, description="是否包含 bot.log.* 轮转文件"),
    enrich_opens: bool = Query(True, description="对齐成交+最终针尖+盈亏"),
    max_enrich: int = Query(40, ge=1, le=100, description="最多对齐多少笔开仓"),
    db: AsyncSession = Depends(get_db),
):
    """扫描 bot.log，按账户/信号源过滤后返回接针统计。"""
    acc, _strategies, strategy_ids, meta = await _load_account_strategies(
        db, account_id, signal_source
    )

    paths = resolve_bot_log_paths(include_rotated=include_rotated)
    if not paths:
        return {
            "ok": False,
            "error": "未找到 logs/bot.log（请确认后端工作目录下有日志）",
            "account_id": account_id,
            "account_name": acc.name,
            "signal_source": signal_source,
            "strategy_ids": sorted(strategy_ids),
            "strategies": [
                {"id": sid, **meta[sid]} for sid in sorted(strategy_ids)
            ],
            "log_files": [],
            "near_miss_total": 0,
            "entry_total": 0,
            "text": "未找到日志文件",
        }

    if not strategy_ids:
        return {
            "ok": True,
            "error": None,
            "account_id": account_id,
            "account_name": acc.name,
            "signal_source": signal_source,
            "strategy_ids": [],
            "strategies": [],
            "log_files": [str(p) for p in paths],
            "near_miss_total": 0,
            "entry_total": 0,
            "opened_total": 0,
            "trigger_total": 0,
            "block_reasons": {},
            "vol_blocked_deep": {
                "progress_min": progress_min,
                "count": 0,
                "listed": 0,
                "rows": [],
                "counterfactual": {"need_5_pass": 0, "need_4_5_pass": 0, "total": 0},
            },
            "open_quality": None,
            "text": (
                f"账户「{acc.name}」下没有匹配信号源「{signal_source}」的策略。"
                "请切换信号源筛选，或确认该账户已创建接针策略。"
            ),
        }

    try:
        report = filter_report_by_strategies(analyze_paths(paths), strategy_ids)
        data = build_analysis(report, progress_min=progress_min, list_limit=list_limit)
        data["ok"] = True
        data["error"] = None
        data["account_id"] = account_id
        data["account_name"] = acc.name
        data["signal_source"] = signal_source
        data["strategy_ids"] = sorted(strategy_ids)
        data["strategies"] = [
            {"id": sid, **meta[sid]} for sid in sorted(strategy_ids)
        ]
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

        _annotate_strategy_meta(data, meta)
        # 文本头部标明账户/信号源
        header = (
            f"账户: {acc.name} (id={account_id}) | 信号源筛选: {signal_source} | "
            f"策略数: {len(strategy_ids)}\n"
        )
        data["text"] = header + data.get("text", "")
        return data
    except Exception as e:
        logger.exception("wick-stats analyze failed: %s", e)
        return {
            "ok": False,
            "error": str(e),
            "account_id": account_id,
            "account_name": acc.name,
            "signal_source": signal_source,
            "log_files": [str(p) for p in paths],
            "text": f"分析失败: {e}",
        }


@router.get("/symbol-monitor")
async def wick_symbol_monitor(
    account_id: int = Query(..., description="账户 ID（必选）"),
    symbol: str = Query(..., min_length=2, description="币种，如 BLESS / BLESSUSDT"),
    signal_source: str = Query("wick_spike", description="wick_spike | wavetrend | all"),
    list_limit: int = Query(120, ge=1, le=500, description="时间线最多条数"),
    include_rotated: bool = Query(True, description="是否包含 bot.log.*"),
    db: AsyncSession = Depends(get_db),
):
    """按币种查询接针监控（near-miss / trigger / opened），解释未满足条件的原因。"""
    acc, _strategies, strategy_ids, meta = await _load_account_strategies(
        db, account_id, signal_source
    )
    paths = resolve_bot_log_paths(include_rotated=include_rotated)
    if not paths:
        raise HTTPException(status_code=404, detail="未找到 logs/bot.log")
    if not strategy_ids:
        raise HTTPException(
            status_code=400,
            detail=f"账户「{acc.name}」下没有匹配信号源「{signal_source}」的策略",
        )

    report = filter_report_by_strategies(analyze_paths(paths), strategy_ids)
    mon = build_symbol_monitor(report, symbol, list_limit=list_limit)
    for row in mon.get("rows") or []:
        info = meta.get(int(row.get("strategy_id") or 0)) or {}
        row["strategy_name"] = info.get("name")
        row["signal_source"] = info.get("signal_source")
        row["signal_source_zh"] = info.get("signal_source_zh")

    return {
        "ok": True,
        "account_id": account_id,
        "account_name": acc.name,
        "signal_source": signal_source,
        "strategy_ids": sorted(strategy_ids),
        "log_files": [str(p) for p in paths],
        **mon,
    }


@router.delete("/logs")
async def clear_wick_stats_logs(
    account_id: int = Query(..., description="账户 ID（必选）"),
    signal_source: str = Query(
        "all",
        description="清除范围对应的策略信号源：wick_spike | wavetrend | all",
    ),
    include_rotated: bool = Query(True, description="是否处理 bot.log.* 轮转文件"),
    clear_memory_logs: bool = Query(True, description="同时清空策略内存日志"),
    db: AsyncSession = Depends(get_db),
):
    """
    删除该账户相关策略在 bot.log 中的接针行，便于统计功能重新采集。

    不删除 trades 成交记录；若需清空成交请用交易历史页。
    """
    acc, _strategies, strategy_ids, _meta = await _load_account_strategies(
        db, account_id, signal_source
    )
    if not strategy_ids:
        raise HTTPException(
            status_code=400,
            detail=f"账户「{acc.name}」下没有匹配信号源「{signal_source}」的策略可清理",
        )

    paths = resolve_bot_log_paths(include_rotated=include_rotated)
    if not paths:
        raise HTTPException(status_code=404, detail="未找到 logs/bot.log")

    result = clear_wick_spike_lines(paths, strategy_ids=strategy_ids)
    cleared_mem = 0
    if clear_memory_logs:
        for sid in strategy_ids:
            strategy_log_service.clear(sid)
            cleared_mem += 1

    logger.warning(
        "wick-stats logs cleared account=%s(%s) strategies=%s files=%s lines=%s",
        account_id,
        acc.name,
        sorted(strategy_ids),
        result["files_touched"],
        result["lines_removed"],
    )
    return {
        "ok": True,
        "account_id": account_id,
        "account_name": acc.name,
        "signal_source": signal_source,
        "strategy_ids": sorted(strategy_ids),
        "log_files": [str(p) for p in paths],
        "files_touched": result["files_touched"],
        "lines_removed": result["lines_removed"],
        "memory_logs_cleared": cleared_mem,
        "message": (
            f"已清除账户「{acc.name}」相关接针日志 "
            f"{result['lines_removed']} 行（{result['files_touched']} 个文件）"
        ),
    }
