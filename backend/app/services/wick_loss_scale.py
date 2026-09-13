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


def wick_loss_scale_times_used(streak: int, max_times: int | None) -> int:
    """实际参与加倍的连亏次数：受 max_times 封顶；None 表示不限次数。"""
    try:
        n = int(streak)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return 0
    if max_times is None:
        return n
    try:
        cap_times = int(max_times)
    except (TypeError, ValueError):
        cap_times = 2
    if cap_times < 0:
        cap_times = 0
    return min(n, cap_times)


def wick_loss_scale_mult(
    streak: int,
    max_mult: float,
    base: float = 2.0,
    max_times: int | None = None,
) -> float:
    """streak=连续止损次数；0→1x，1→base，2→base²，封顶 max_mult。

    max_times 限制升档次数（默认策略字段为 2：最多 ×2 再 ×4）。
    函数默认 None=不限次数，只受 max_mult 封顶。
    """
    n = wick_loss_scale_times_used(streak, max_times)
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


def infer_flat_close_reason(side: str, entry: float, exit_px: float) -> str:
    """交易所已空、本地仍开时，用价差推断上一轮是止盈还是止损。"""
    try:
        entry_f = float(entry or 0)
        exit_f = float(exit_px or 0)
    except (TypeError, ValueError):
        return "sync"
    if entry_f <= 0 or exit_f <= 0:
        return "sync"
    side_l = (side or "").lower()
    if side_l == "long":
        return "take_profit" if exit_f >= entry_f else "stop_loss"
    if side_l in ("short", "sell"):
        return "take_profit" if exit_f <= entry_f else "stop_loss"
    return "sync"


def same_open_fill(existing_order_id: str | None, new_order_id: str | None) -> bool:
    """同一笔成交才允许 merge：双方都有单号且不同 → 上一轮未落库，禁止覆盖。"""
    old = str(existing_order_id or "").strip()
    new = str(new_order_id or "").strip()
    if not old or not new:
        return True
    return old == new


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
