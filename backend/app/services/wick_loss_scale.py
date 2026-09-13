"""接针同币种连亏加倍：止损 1/2/3 次 → ×2/×4/×8，受上限封顶。"""

from __future__ import annotations

from sqlalchemy import func, or_, select

from ..models.trade import Trade


def _trade_symbol_norm_expr():
    """与 position_manager._norm_sym 一致：去斜杠 / :USDT / 下划线后大写。"""
    return func.replace(
        func.replace(
            func.replace(func.upper(Trade.symbol), "/", ""),
            ":USDT",
            "",
        ),
        "_",
        "",
    )


def wick_loss_scale_mult(
    streak: int, max_mult: float, base: float = 2.0
) -> float:
    """streak=连续止损次数；0→1x，1→base，2→base²，封顶 max_mult。默认 base=2 → ×2/×4/×8。"""
    try:
        n = int(streak)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return 1.0
    try:
        step = float(base)
    except (TypeError, ValueError):
        step = 2.0
    if step < 1.0:
        step = 1.0
    try:
        cap = float(max_mult)
    except (TypeError, ValueError):
        cap = 8.0
    if cap < 1.0:
        cap = 1.0
    raw = step ** n
    return min(raw, cap)


def consecutive_stop_loss_streak(reasons: list[str | None]) -> int:
    """reasons 已按时间倒序（最近在前）；只数开头连续的 stop_loss。"""
    n = 0
    for raw in reasons:
        if (raw or "").strip() == "stop_loss":
            n += 1
        else:
            break
    return n


def _norm_trade_symbol(symbol: str) -> str:
    return (symbol or "").upper().replace("/", "").replace(":USDT", "").replace("_", "")


async def count_consecutive_stop_losses(
    session, strategy_id: int, symbol: str
) -> int:
    """按该策略+币种最近 layer=0 成交，统计连续 stop_loss 次数。"""
    sid = int(strategy_id or 0)
    sym = _norm_trade_symbol(symbol)
    if sid <= 0 or not sym:
        return 0
    rows = (
        await session.execute(
            select(Trade.close_reason)
            .where(
                Trade.strategy_id == sid,
                or_(Trade.layer == 0, Trade.layer.is_(None)),
                _trade_symbol_norm_expr() == sym,
            )
            .order_by(Trade.exit_time.desc(), Trade.id.desc())
            .limit(20)
        )
    ).scalars().all()
    return consecutive_stop_loss_streak(list(rows))


async def last_close_reason(session, strategy_id: int, symbol: str) -> str:
    """该策略+币种最近一笔 layer0 的 close_reason（符号格式不敏感）。"""
    sid = int(strategy_id or 0)
    sym = _norm_trade_symbol(symbol)
    if sid <= 0 or not sym:
        return ""
    raw = (
        await session.execute(
            select(Trade.close_reason)
            .where(
                Trade.strategy_id == sid,
                or_(Trade.layer == 0, Trade.layer.is_(None)),
                _trade_symbol_norm_expr() == sym,
            )
            .order_by(Trade.exit_time.desc(), Trade.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return str(raw or "").strip()
