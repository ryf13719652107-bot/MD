"""Per-symbol position processing: signal, open, manage, close, martingale."""
import asyncio
import logging
import math
from datetime import datetime
from types import SimpleNamespace
from typing import Optional, Any
from sqlalchemy import select, func, inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession
from ..models.strategy import Strategy
from ..models.position import Position
from ..models.trade import Trade
from ..models.strategy_blacklist import StrategySymbolBlacklist
from ..config import now_beijing
from .binance_service import BinanceService
from .strategy_engine import (
    calculate_rsi,
    generate_signal,
    Signal,
    calculate_wavetrend,
    generate_wt_signal,
    calculate_supertrend,
    generate_trend_wt_signal,
)
from .martingale_engine import MartingaleEngine
from .risk_manager import RiskManager
from .log_service import strategy_log_service
from .kline_stream import kline_stream_manager, _timeframe_ms
from .backup_service import backup_trade
from .order_times import exit_time_from_order, naive_beijing_from_ms_or_s
from .tick_context import TickContext, SignalCandidate, OpenApiResult, exchange_legs_from_positions

logger = logging.getLogger(__name__)


def strategy_signal_snapshot(strategy: Strategy) -> SimpleNamespace:
    """拷贝策略标量，供并行扫信号使用（禁止多协程共享 session 绑定的 ORM）。"""
    data = {
        attr.key: getattr(strategy, attr.key)
        for attr in sa_inspect(Strategy).mapper.column_attrs
    }
    return SimpleNamespace(**data)


def _norm_sym(s: str) -> str:
    """Canonical perp key (e.g. BTCUSDT). Uppercase so DB/exchange format differences still match."""
    return (s or "").upper().replace("/", "").replace(":USDT", "").replace("_", "")


def _collapse_phantom_l0_duplicates(
    positions: list,
    *,
    now=None,
    qty_tol: float = 0.02,
) -> list:
    """折叠「同腿多个 L0 且数量几乎相同」的记账重复行。

    选币池符号与 ccxt 符号竞态时会各建一条全量 L0；平仓若都写 Trade 会双倍盈亏。
    保留一条（优先有止盈单 ID），其余仅标 closed_at、**不写 Trade**。
    不同 layer 的马丁加仓行不受影响。
    """
    if len(positions) <= 1:
        return list(positions)
    ts = now or now_beijing()
    keep: list = []
    l0 = [p for p in positions if int(getattr(p, "layer", 0) or 0) == 0]
    higher = [p for p in positions if int(getattr(p, "layer", 0) or 0) > 0]
    if len(l0) <= 1:
        return list(positions)

    l0_sorted = sorted(
        l0,
        key=lambda p: (
            0 if (getattr(p, "tp_limit_order_id", None) or "").strip() else 1,
            int(getattr(p, "id", 0) or 0),
        ),
    )
    primary = l0_sorted[0]
    keep.append(primary)
    pq = float(getattr(primary, "quantity", 0) or 0)
    for p in l0_sorted[1:]:
        q = float(getattr(p, "quantity", 0) or 0)
        near_dup = pq > 0 and q > 0 and abs(q - pq) / pq <= qty_tol
        if near_dup and getattr(p, "closed_at", None) is None:
            p.closed_at = ts
            if hasattr(p, "symbol") and hasattr(primary, "symbol"):
                p.symbol = _norm_sym(primary.symbol) or p.symbol
            logger.warning(
                "collapse phantom L0 duplicate position id=%s qty=%.6f "
                "(keep id=%s) — no Trade written",
                getattr(p, "id", None),
                q,
                getattr(primary, "id", None),
            )
        else:
            keep.append(p)
    keep.extend(higher)
    return keep


def _open_signal_log_suffix(signal_label: str, rsi: float) -> str:
    """Format signal part of open logs; basic martingale has no indicator value."""
    if signal_label == "基础马丁":
        return "基础马丁"
    return f"{signal_label}={round(rsi, 1)}"


def _order_fill_avg_price(
    order: dict,
    fallback: float = 0.0,
    *,
    allow_order_price: bool = True,
) -> float:
    """从成交回报解析均价。

    优先 ccxt ``average`` 与币安 ``info.avgPrice``，其次 ``cost/filled``。
    顶层/info ``price`` 常是限价挂单价或市价参考价，**不得**在止盈出场路径启用
    （``allow_order_price=False``）；开仓等场景可作最后兜底。
    """
    if not isinstance(order, dict):
        return float(fallback or 0)

    def _pos(v) -> float:
        try:
            x = float(v or 0)
            return x if x > 0 else 0.0
        except (TypeError, ValueError):
            return 0.0

    avg = _pos(order.get("average"))
    if avg > 0:
        return avg
    info = order.get("info") if isinstance(order.get("info"), dict) else {}
    for key in ("avgPrice", "averagePrice"):
        avg = _pos(info.get(key))
        if avg > 0:
            return avg
    # cost/filled 推算 VWAP（部分回报 average 为空但仍有累计）
    filled = _pos(order.get("filled")) or _pos(info.get("executedQty"))
    cost = _pos(order.get("cost"))
    if cost <= 0 and filled > 0:
        # 币安 info.cumQuote ≈ 成交额（USDT）
        cost = _pos(info.get("cumQuote")) or _pos(info.get("cum_quote"))
    if filled > 0 and cost > 0:
        return cost / filled
    if allow_order_price:
        for raw in (order.get("price"), info.get("price")):
            avg = _pos(raw)
            if avg > 0:
                return avg
    fb = _pos(fallback)
    return fb


async def _fetch_order(client, order_id: str, symbol: str) -> dict:
    """查订单：币安保持原 exchange.fetch_order；GATE 走服务层（settle + 张→币）。"""
    if getattr(client, "exchange_id", None) == "gate":
        return await client.fetch_order(order_id, symbol)
    return await client.exchange.fetch_order(order_id, client._format_symbol(symbol))


async def _live_last_price(client, symbol: str, fallback: float = 0.0) -> float:
    """交易所最新价（ticker last），用于止盈穿越判定；勿用滞后 K 线 close。"""
    try:
        t = await client.fetch_ticker(symbol)
        for k in ("last", "close", "mark"):
            try:
                v = float(t.get(k) or 0)
            except (TypeError, ValueError):
                v = 0.0
            if v > 0:
                return v
        info = t.get("info") if isinstance(t.get("info"), dict) else {}
        for k in ("lastPrice", "markPrice"):
            try:
                v = float(info.get(k) or 0)
            except (TypeError, ValueError):
                v = 0.0
            if v > 0:
                return v
    except Exception:
        pass
    try:
        return float(fallback or 0)
    except (TypeError, ValueError):
        return 0.0


def _vwap_from_my_trades(trades: list) -> float:
    """从成交明细算 VWAP。"""
    num = 0.0
    den = 0.0
    for t in trades or []:
        if not isinstance(t, dict):
            continue
        try:
            px = float(t.get("price") or 0)
            amt = float(t.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        if px > 0 and amt > 0:
            num += px * amt
            den += amt
    return (num / den) if den > 0 else 0.0


async def _resolve_market_close_exit(
    client, symbol: str, order: dict, fallback: float = 0.0
) -> tuple[float, dict]:
    """市价平仓出场价：必须尽量拿到真实市价成交均价再写库。

    顺序：回报 average/avgPrice/cumQuote → 重查订单 → fetch_my_trades(orderId) VWAP
    → ticker last。禁止用滞后 K 线 close（HEI：记 0.2041 vs 币安 0.221）。
    """
    fill_order = order if isinstance(order, dict) else {}
    px = _order_fill_avg_price(fill_order, 0.0, allow_order_price=False)
    if px > 0:
        return px, fill_order

    oid = str(fill_order.get("id") or "").strip()
    if oid:
        for attempt in range(5):
            try:
                await asyncio.sleep(0.15 * (attempt + 1))
                oi = await _fetch_order(client, oid, symbol)
                px = _order_fill_avg_price(oi, 0.0, allow_order_price=False)
                if px > 0:
                    return px, oi
                if isinstance(oi, dict) and oi:
                    fill_order = oi
            except Exception:
                pass

        # closePosition 常无 average：按 orderId 拉成交明细算 VWAP
        fetch_trades = getattr(client, "fetch_my_trades", None)
        if callable(fetch_trades):
            for attempt in range(3):
                try:
                    await asyncio.sleep(0.2 * (attempt + 1))
                    trades = await fetch_trades(symbol, order_id=oid, limit=50)
                    px = _vwap_from_my_trades(trades if isinstance(trades, list) else [])
                    if px > 0:
                        merged = dict(fill_order)
                        merged["average"] = px
                        merged["filled"] = sum(
                            float(t.get("amount") or 0)
                            for t in (trades or [])
                            if isinstance(t, dict)
                        )
                        logger.info(
                            "market close %s: exit avg %.8f from my_trades orderId=%s",
                            symbol,
                            px,
                            oid,
                        )
                        return px, merged
                except Exception as e:
                    logger.debug(
                        "market close %s fetch_my_trades orderId=%s: %s",
                        symbol,
                        oid,
                        e,
                    )

    live = await _live_last_price(client, symbol, 0.0)
    if live > 0:
        logger.warning(
            "market close %s: no fill avg on order %s, using live ticker %.8f "
            "(not kline fallback)",
            symbol,
            oid or "?",
            live,
        )
        return live, fill_order
    try:
        fb = float(fallback or 0)
    except (TypeError, ValueError):
        fb = 0.0
    return fb, fill_order


def _tp_limit_reduce_only(client) -> bool:
    """止盈限价是否带 reduceOnly。

    币安双向：原链路只用 positionSide，勿带 reduceOnly（否则易 -1106）。
    GATE 双向：必须 reduceOnly，否则会开反向仓。
    """
    return getattr(client, "exchange_id", None) == "gate"


def _order_reduce_only_flag(order: dict) -> bool | None:
    """解析挂单 reduceOnly；无法判断时返回 None。"""
    if not isinstance(order, dict):
        return None
    info = order.get("info") if isinstance(order.get("info"), dict) else {}
    for key in ("reduceOnly", "reduce_only", "is_reduce_only"):
        if order.get(key) is not None:
            ro = order.get(key)
            return bool(ro) if isinstance(ro, bool) else str(ro).lower() in ("true", "1")
        if info.get(key) is not None:
            ro = info.get(key)
            return bool(ro) if isinstance(ro, bool) else str(ro).lower() in ("true", "1")
    return None


def _order_position_side(order: dict) -> str:
    info = order.get("info") if isinstance(order.get("info"), dict) else {}
    return str(
        order.get("positionSide")
        or order.get("posSide")
        or info.get("positionSide")
        or info.get("posSide")
        or ""
    ).upper()


def _is_matching_tp_close_limit(
    order: dict,
    *,
    close_side: str,
    ps_need: str,
    contracts: float,
    hedge_mode: bool,
    exchange_id: str | None,
    require_qty_match: bool = True,
) -> bool:
    """是否为平本腿的限价止盈挂单。

    Gate：必须 reduceOnly。
    币安：下单通常不带 reduceOnly，按平仓方向 + positionSide（双向）匹配。
    require_qty_match=False 时忽略数量（用于找加仓后数量已过时的旧单并去重）。
    """
    if (order.get("side") or "").lower() != close_side:
        return False
    typ = (order.get("type") or "").lower()
    if "limit" not in typ:
        return False
    eid = (exchange_id or "").lower()
    ro = _order_reduce_only_flag(order)
    if eid == "gate":
        if ro is not True:
            return False
    # 币安：若明确非 reduceOnly 且无 positionSide，可能是开仓限价，跳过
    if eid != "gate" and ro is False and not _order_position_side(order):
        return False
    if hedge_mode:
        ips = _order_position_side(order)
        if ips and ips != ps_need:
            return False
    if require_qty_match:
        amt = float(order.get("amount", 0) or 0) or float(order.get("remaining", 0) or 0)
        if contracts > 0 and amt > 0:
            rel = abs(amt - contracts) / max(contracts, 1e-12)
            if rel > 0.02 and abs(amt - contracts) > 1e-5:
                return False
    return bool(order.get("id"))


def _position_opened_at_from_exchange(ep: dict) -> Optional[datetime]:
    ts = ep.get("timestamp")
    dt = naive_beijing_from_ms_or_s(ts)
    if dt:
        return dt
    info = ep.get("info") or {}
    if isinstance(info, dict):
        for k in ("updateTime", "entryTime", "createdTime"):
            v = info.get(k)
            dt = naive_beijing_from_ms_or_s(v)
            if dt:
                return dt
    return None


def _single_symbol_stop_loss_trigger(
    margin_balance: float,
    symbol_floating_loss: float,
    threshold_pct: float,
) -> bool:
    """单币浮亏达到「保证金余额/权益」的 threshold_pct% 时触发。

    分母必须是 extract_margin_balance（币安保证金余额 / Gate total+未实现），
    不是可用余额，也不是不含浮盈亏的钱包余额。
    """
    if symbol_floating_loss <= 0:
        return False
    if margin_balance <= 0:
        return False
    pct = max(0.0, float(threshold_pct or 0))
    return symbol_floating_loss >= margin_balance * (pct / 100.0)


def _symbol_unrealized_pnl_from_exchange(
    raw_exchange_positions: list[dict[str, Any]],
    symbol: str,
    side: str,
) -> float | None:
    sym_key = _norm_sym(symbol)
    side_key = (side or "").lower()
    matched = False
    total = 0.0
    for ep in raw_exchange_positions or []:
        contracts = float(ep.get("contracts", 0) or 0)
        if contracts <= 0:
            continue
        if _norm_sym(ep.get("symbol") or "") != sym_key:
            continue
        if (ep.get("side") or "").lower() != side_key:
            continue
        info = ep.get("info") or {}
        if not isinstance(info, dict):
            info = {}
        matched = True
        total += float(
            ep.get("unrealizedPnl")
            or ep.get("unrealized_pnl")
            or info.get("unRealizedProfit")
            or info.get("unrealizedProfit")
            or 0
        )
    return total if matched else None


# _reconcile_orphan_from_exchange return values
RECONCILE_CREATED = "created"
RECONCILE_NO_ORPHAN = "no_orphan"  # nothing on exchange for this symbol, or no insert needed
RECONCILE_SKIPPED_OTHER_STRATEGY = "skipped_other_strategy"  # same acc: another strategy already owns symbol+side in DB
RECONCILE_DB_ERROR = "db_error"


def _bot_owned_positions(positions: list) -> list:
    """仅机器人开仓记录（有 exchange_order_id）。无单号视为手动/领养仓，不管理。"""
    return [
        p
        for p in positions
        if (getattr(p, "exchange_order_id", None) or "").strip()
    ]


def _klines_for_confirmed_signal_only(klines: list, timeframe: str) -> list:
    """仅使用已收盘 K 线算信号，对齐 TradingView 等「收盘确认」逻辑。

    ``timeframe`` 须与策略配置一致（与拉 K / kline_stream 使用同一周期字符串）。
    若 ``klines[-1]`` 的开盘时间 + 周期仍晚于当前时间，视为正在形成中的 K，排除后再算 WT/RSI。
    若最后一根已完整（时间已过该根收盘边界），则保留。
    """
    if len(klines) < 2:
        return klines
    tf_ms = _timeframe_ms(timeframe)
    import time

    now_ms = int(time.time() * 1000)
    try:
        last_open = int(klines[-1][0])
    except (TypeError, ValueError, IndexError):
        return klines[:-1]
    if now_ms >= last_open + tf_ms:
        return klines
    return klines[:-1]


def wick_martingale_mode_needs_wt(mode: str | None) -> bool:
    """接针加仓是否需要 WT 确认（在涨跌幅门槛之后）。缺省按 price_and_wt。"""
    return (mode or "price_and_wt") != "price_drop"


def martingale_wt_confirm_allows_add(
    klines_confirm: list,
    *,
    direction: str,
    wt_channel_length: int,
    wt_average_length: int,
    wt_os_level: float,
    wt_ob_level: float,
) -> tuple[bool, str]:
    """用 WT 确认马丁加仓。

    返回 (允许加仓, 日志文案)。
    WT 算不出时放行（与既有 wavetrend 加仓确认一致：仅在算出信号且为中性时拦截）。
    """
    wt = calculate_wavetrend(klines_confirm, wt_channel_length, wt_average_length)
    if wt is None:
        return True, "WT不可用，跳过确认"
    confirm = generate_wt_signal(wt, direction, wt_os_level, wt_ob_level)
    if confirm == Signal.NEUTRAL:
        return False, f"WT1={wt['wt1']:.2f} 信号已消失"
    return True, f"WT1={wt['wt1']:.2f} WT2={wt['wt2']:.2f}"


class PositionManager:
    """Handles per-symbol processing within a strategy tick."""

    def __init__(self, risk_mgr: Optional[RiskManager] = None):
        self.risk_mgr = risk_mgr or RiskManager()

    async def _fetch_open_orders_raw(
        self, auth_binance: BinanceService, symbol: str
    ) -> list[dict]:
        formatted = auth_binance._format_symbol(symbol)
        try:
            if getattr(auth_binance, "exchange_id", None) == "gate":
                orders = await auth_binance.fetch_open_orders(symbol)
            else:
                orders = await auth_binance.exchange.fetch_open_orders(formatted)
            return list(orders) if orders else []
        except Exception as e:
            logger.debug("Strategy %s: fetch_open_orders failed: %s", symbol, e)
            return []

    def _list_matching_tp_close_limits(
        self,
        orders: list[dict],
        *,
        position_side: str,
        contracts: float,
        auth_binance: BinanceService,
        require_qty_match: bool = True,
    ) -> list[dict]:
        close_side = "sell" if position_side == "long" else "buy"
        ps_need = "LONG" if position_side == "long" else "SHORT"
        eid = getattr(auth_binance, "exchange_id", None)
        hedge = bool(getattr(auth_binance, "hedge_mode", True))
        out: list[dict] = []
        for o in orders:
            if not isinstance(o, dict):
                continue
            if _is_matching_tp_close_limit(
                o,
                close_side=close_side,
                ps_need=ps_need,
                contracts=contracts,
                hedge_mode=hedge,
                exchange_id=eid,
                require_qty_match=require_qty_match,
            ):
                out.append(o)
        return out

    @staticmethod
    def _order_amount(order: dict) -> float:
        return float(order.get("amount", 0) or 0) or float(order.get("remaining", 0) or 0)

    @staticmethod
    def _pick_best_tp_match(matches: list[dict], contracts: float) -> dict | None:
        """优先数量最接近总仓的止盈单。"""
        if not matches:
            return None
        if contracts <= 0:
            return matches[0]
        return min(matches, key=lambda o: abs(PositionManager._order_amount(o) - contracts))

    @staticmethod
    def _tp_qty_ok(order: dict, contracts: float) -> bool:
        """挂单数量是否与总仓一致（2% 或绝对 1e-5）。"""
        if contracts <= 0:
            return True
        amt = PositionManager._order_amount(order)
        if amt <= 0:
            return False
        rel = abs(amt - contracts) / max(contracts, 1e-12)
        return rel <= 0.02 or abs(amt - contracts) <= 1e-5

    async def _cancel_tp_order_confirmed(
        self, auth_binance: BinanceService, order_id: str, symbol: str
    ) -> bool:
        """撤止盈单；已不存在/已终结也视为成功。"""
        oid = (order_id or "").strip()
        if not oid:
            return True
        try:
            await auth_binance.cancel_order(oid, symbol)
            return True
        except Exception as e:
            logger.warning(
                "cancel TP %s %s failed: %s — checking order status", oid, symbol, e
            )
        try:
            info = await asyncio.wait_for(
                _fetch_order(auth_binance, oid, symbol), timeout=3.0
            )
            st = (info.get("status") or "").lower()
            if st in ("canceled", "cancelled", "closed", "filled", "expired"):
                return True
        except Exception:
            pass
        return False

    async def _cancel_bot_tp_order_ids(
        self,
        auth_binance: BinanceService,
        symbol: str,
        order_ids: list[str] | set[str],
        strategy_id: int,
        *,
        keep_id: str = "",
    ) -> None:
        """只撤销机器人自己记下的止盈单号，绝不扫撤手动限价。"""
        keep = str(keep_id or "")
        seen: set[str] = set()
        for oid in order_ids:
            oid_s = str(oid or "").strip()
            if not oid_s or oid_s == keep or oid_s in seen:
                continue
            seen.add(oid_s)
            ok = await self._cancel_tp_order_confirmed(auth_binance, oid_s, symbol)
            if ok:
                strategy_log_service.info(
                    strategy_id, f"{symbol} 撤销机器人止盈限价单 id={oid_s}"
                )
            else:
                strategy_log_service.warning(
                    strategy_id, f"{symbol} 撤销机器人止盈单失败 id={oid_s}"
                )

    async def cancel_bot_tps_on_positions(
        self,
        auth_binance: BinanceService,
        symbol: str,
        positions: list,
        strategy_id: int,
    ) -> None:
        """平仓前撤掉这些仓位上的机器人止盈单，避免策略停掉后残留限价继续减仓。"""
        ids = {
            str(getattr(p, "tp_limit_order_id", None) or "").strip()
            for p in positions
            if str(getattr(p, "tp_limit_order_id", None) or "").strip()
        }
        if not ids:
            return
        await self._cancel_bot_tp_order_ids(
            auth_binance, symbol, ids, strategy_id, keep_id=""
        )
        for p in positions:
            if getattr(p, "tp_limit_order_id", None):
                p.tp_limit_order_id = None

    async def _cancel_duplicate_tp_limits(
        self,
        auth_binance: BinanceService,
        symbol: str,
        matches: list[dict],
        keep_id: str,
        strategy_id: int,
        *,
        only_bot_ids: set[str] | None = None,
    ) -> None:
        """兼容旧调用：默认只撤 only_bot_ids；未传则不撤任何「匹配到的」陌生单。"""
        if only_bot_ids is None:
            return
        await self._cancel_bot_tp_order_ids(
            auth_binance,
            symbol,
            only_bot_ids,
            strategy_id,
            keep_id=keep_id,
        )

    async def _bind_tp_limit_from_open_orders(
        self,
        auth_binance: BinanceService,
        symbol: str,
        position_side: str,
        pos: Position,
        contracts: float,
        *,
        strategy_id: int | None = None,
        cancel_duplicates: bool = False,
    ) -> None:
        """不再认领交易所上的陌生限价单（避免把手动止盈绑给机器人）。

        机器人止盈只使用本地 tp_limit_order_id 或自行新挂。
        """
        return

    async def _ensure_tp_limit_orders(
        self,
        session: AsyncSession,
        strategy: Strategy,
        symbol: str,
        auth_binance: BinanceService,
        open_positions: list,
        eng: MartingaleEngine,
        avg_entry: float,
        total_qty: float,
        pos_side: str,
    ) -> None:
        """限价止盈：只管理机器人自己的 tp_limit_order_id，按策略数量挂单。

        不认领/不撤销手动限价；无 exchange_order_id 的仓不挂止盈。
        """
        if not getattr(strategy, "take_profit_limit_order", False):
            return
        bot_positions = _bot_owned_positions(open_positions)
        if not bot_positions:
            return
        bot_qty = sum(float(p.quantity or 0) for p in bot_positions)
        if bot_qty <= 0:
            return
        # 调用方可能传入整腿 total_qty；止盈数量以机器人记账为准
        qty = bot_qty
        strategy_id = strategy.id
        existing_ids = {
            (p.tp_limit_order_id or "").strip()
            for p in bot_positions
            if (p.tp_limit_order_id or "").strip()
        }

        if existing_ids:
            keep = sorted(existing_ids)[0]
            keep_order = None
            try:
                keep_order = await asyncio.wait_for(
                    _fetch_order(auth_binance, keep, symbol), timeout=3.0
                )
            except (Exception, asyncio.TimeoutError):
                keep_order = None
            st = ((keep_order or {}).get("status") or "").lower()
            if keep_order and st in ("open", "new", "partially_filled", "partial"):
                if self._tp_qty_ok(keep_order, qty):
                    px = float(keep_order.get("price", 0) or 0)
                    for p in bot_positions:
                        p.tp_limit_order_id = keep
                        if px > 0:
                            p.take_profit_price = px
                    # 只撤其它机器人记下的重复 id，不动手动单
                    await self._cancel_bot_tp_order_ids(
                        auth_binance, symbol, existing_ids, strategy_id, keep_id=keep
                    )
                    await session.flush()
                    return
                # 数量不符：只撤机器人自己的旧单后重挂
                await self._cancel_bot_tp_order_ids(
                    auth_binance, symbol, existing_ids, strategy_id, keep_id=""
                )
                for p in bot_positions:
                    p.tp_limit_order_id = None
                await session.flush()
            elif keep_order and st in ("closed", "filled"):
                # 已成交：交给 TP 成交检测，这里不重挂
                return
            else:
                # 查不到/已取消：清本地 id 后重挂
                await self._cancel_bot_tp_order_ids(
                    auth_binance, symbol, existing_ids, strategy_id, keep_id=""
                )
                for p in bot_positions:
                    p.tp_limit_order_id = None
                await session.flush()

        tp_price = eng.get_take_profit_price(avg_entry, pos_side)
        if tp_price <= 0:
            return
        close_side = "sell" if pos_side == "long" else "buy"
        ps = "LONG" if pos_side == "long" else "SHORT"
        for attempt in range(2):
            try:
                tp_order = await auth_binance.create_limit_order(
                    symbol,
                    close_side,
                    qty,
                    tp_price,
                    reduce_only=_tp_limit_reduce_only(auth_binance),
                    position_side=ps,
                )
                oid = tp_order.get("id", "")
                if oid:
                    oid_s = str(oid)
                    for p in bot_positions:
                        p.tp_limit_order_id = oid_s
                        p.take_profit_price = tp_price
                    await session.flush()
                    strategy_log_service.info(
                        strategy_id,
                        f"{symbol} 补挂止盈限价单 @{tp_price:.6f} qty={qty:.4f} id={oid_s}",
                    )
                    return
                strategy_log_service.warning(
                    strategy_id, f"{symbol} 补挂止盈单异常 — 返回无id: {tp_order}"
                )
            except Exception as tp_err:
                logger.error(
                    "Strategy %d: TP re-place failed for %s (attempt %d): %s",
                    strategy_id,
                    symbol,
                    attempt + 1,
                    tp_err,
                )
                if attempt == 0:
                    await asyncio.sleep(0.5)
        strategy_log_service.warning(
            strategy_id, f"{symbol} 补挂止盈失败(已重试) — 仍可用市价止盈兜底"
        )

    async def _reconcile_orphan_from_exchange(
        self,
        session: AsyncSession,
        strategy: Strategy,
        symbol: str,
        auth_binance: BinanceService,
        current_price: float,
        *,
        raw_exchange_positions: list[dict[str, Any]] | None = None,
        adopt: bool = False,
        exchange_order_id: str = "",
        quantity: float | None = None,
        entry_price_override: float | None = None,
    ) -> str:
        """仅在机器人刚成交但本地无行时补建（须 adopt=True 且带 exchange_order_id）。

        默认不领养交易所手动仓。Returns one of RECONCILE_* constants.
        """
        strategy_id = strategy.id
        oid = str(exchange_order_id or "").strip()
        if not adopt or not oid:
            return RECONCILE_NO_ORPHAN

        sym_target = _norm_sym(symbol)
        if raw_exchange_positions is not None:
            eps = [
                ep
                for ep in raw_exchange_positions
                if _norm_sym(ep.get("symbol") or "") == sym_target
            ]
        else:
            try:
                eps = await auth_binance.fetch_positions([symbol])
            except Exception as e:
                logger.warning("Strategy %d: fetch_positions for reconcile %s failed: %s", strategy_id, symbol, e)
                return RECONCILE_NO_ORPHAN

        # 账户内已有同向开仓（含本策略）：禁止再 insert，避免
        # 选币池 BMTUSDT 与 ccxt BMT/USDT:USDT 各建一条全量仓。
        open_stmt = (
            select(Position)
            .where(
                Position.account_id == strategy.account_id,
                Position.closed_at.is_(None),
            )
        )
        open_rows = list((await session.execute(open_stmt)).scalars().all())
        other_keys = {
            (_norm_sym(p.symbol), p.side.lower())
            for p in open_rows
            if p.strategy_id != strategy_id
        }
        own_keys = {
            (_norm_sym(p.symbol), p.side.lower())
            for p in open_rows
            if p.strategy_id == strategy_id
        }

        created = False
        blocked_by_other = False
        for ep in eps:
            contracts = float(ep.get("contracts", 0) or 0)
            if contracts <= 0:
                continue
            ep_sym = _norm_sym(ep.get("symbol") or "")
            if ep_sym != sym_target:
                continue
            side = (ep.get("side") or "").lower()
            if side not in ("long", "short"):
                continue
            if side != (strategy.direction or "").lower():
                # 只认领与本策略方向一致的交易所持仓腿（多单策略不领养空单，反之亦然）
                continue
            if (ep_sym, side) in own_keys:
                logger.info(
                    "Strategy %d: skip reconcile %s %s — already have open DB row",
                    strategy_id,
                    ep_sym,
                    side,
                )
                continue
            if (ep_sym, side) in other_keys:
                # Another strategy on this account already has an open Position for this exact symbol+side.
                # Hedge mode: opposite side (long vs short) is a different key — 一多一空 can both have rows.
                blocked_by_other = True
                logger.warning(
                    "Strategy %d: skip reconcile %s %s — another strategy already holds this in DB",
                    strategy_id,
                    ep_sym,
                    side,
                )
                continue

            entry_price = float(entry_price_override or 0)
            if entry_price <= 0:
                entry_price = float(ep.get("entryPrice") or ep.get("entry_price") or 0)
            if entry_price <= 0:
                entry_price = float(ep.get("markPrice") or ep.get("mark_price") or current_price)
            mark_price = float(ep.get("markPrice") or ep.get("mark_price") or current_price)
            upnl = float(ep.get("unrealizedPnl") or ep.get("unrealized_pnl") or 0)
            qty = float(quantity or 0)
            if qty <= 0:
                qty = contracts

            eng = MartingaleEngine(
                base_quantity=qty,
                multiplier=strategy.martingale_mult,
                max_layers=strategy.max_layers,
                price_drop_multiplier=float(strategy.price_drop_multiplier or 1.0),
                take_profit_pct=strategy.take_profit_pct,
            )
            tp_price = eng.get_take_profit_price(entry_price, side)

            opened_at = _position_opened_at_from_exchange(ep) or now_beijing()
            # 统一规范符号，避免与 execute_open_db / 选币池格式分叉
            pos = Position(
                strategy_id=strategy_id,
                account_id=strategy.account_id,
                symbol=ep_sym,
                side=side,
                quantity=qty,
                entry_price=entry_price,
                mark_price=mark_price,
                unrealized_pnl=upnl,
                layer=0,
                take_profit_price=tp_price,
                exchange_order_id=oid,
                opened_at=opened_at,
            )
            session.add(pos)
            try:
                await session.flush()
            except Exception as e:
                logger.exception(
                    "Strategy %d: reconcile flush failed for %s %s: %s",
                    strategy_id,
                    symbol,
                    side,
                    e,
                )
                return RECONCILE_DB_ERROR
            created = True
            own_keys.add((ep_sym, side))
            logger.warning(
                "Strategy %d: reconciled bot open into DB — %s %s qty=%.6f entry=%.6f order=%s",
                strategy_id,
                ep_sym,
                side,
                qty,
                entry_price,
                oid,
            )
            strategy_log_service.warning(
                strategy_id,
                f"{ep_sym} 写库缺失已按成交单 {oid} 补建持仓(L0) qty={qty:.6f}",
            )

        if created:
            return RECONCILE_CREATED
        if blocked_by_other:
            return RECONCILE_SKIPPED_OTHER_STRATEGY
        return RECONCILE_NO_ORPHAN

    def _open_positions_stmt(self, strategy_id: int, sym_key: str):
        symbol_norm = func.replace(
            func.replace(func.upper(Position.symbol), "/", ""),
            ":USDT",
            "",
        )
        return (
            select(Position)
            .where(
                Position.strategy_id == strategy_id,
                Position.closed_at.is_(None),
                symbol_norm == sym_key,
            )
            .order_by(Position.layer.desc())
        )

    def _passes_new_entry_filters(self, symbol: str, strategy: Strategy, ctx: TickContext) -> bool:
        sym_key = _norm_sym(symbol)
        if not ctx.wallet_balance_valid:
            return False
        if ctx.allow_new_norms is not None and sym_key not in ctx.allow_new_norms:
            return False
        if sym_key in ctx.exclude_norm:
            return False
        if ctx.funding_filter_enabled and ctx.funding_rates is not None:
            from ..services.strategy_flags import (
                funding_rate_blocks_new_entry,
                funding_rate_threshold_pct,
            )

            rate = ctx.funding_rates.get(sym_key, 0.0)
            if funding_rate_blocks_new_entry(
                strategy.direction,
                rate,
                funding_rate_threshold_pct(strategy),
            ):
                return False
        return True

    def _compute_base_qty(self, strategy: Strategy, total_margin: float, current_price: float) -> float | None:
        base_qty = strategy.base_qty_value
        if strategy.base_qty_type == "margin_pct":
            if total_margin <= 0:
                return None
            base_qty = (total_margin * strategy.base_qty_value / 100) / current_price
        elif strategy.base_qty_type == "usdt":
            base_qty = strategy.base_qty_value / current_price
        return base_qty

    @staticmethod
    def _is_min_qty_order_error(exc: BaseException) -> bool:
        msg = str(exc).lower()
        return (
            "过小" in str(exc)
            or "无法换算为合约张数" in str(exc)
            or "min_notional" in msg
            or "min notional" in msg
            or ("notional" in msg and "too small" in msg)
        )

    async def _should_skip_min_qty_exceeds(
        self,
        auth_binance: BinanceService,
        strategy: Strategy,
        strategy_id: int,
        symbol: str,
        base_qty: float,
        current_price: float,
        *,
        action: str = "开仓",
    ) -> bool:
        """意图名义 < 交易所最小开仓名义时跳过（开关默认开）。"""
        from ..services.strategy_flags import skip_min_qty_exceeds_enabled

        if not skip_min_qty_exceeds_enabled(strategy):
            return False
        if base_qty is None or base_qty <= 0 or current_price is None or current_price <= 0:
            return False
        estimator = getattr(auth_binance, "estimate_min_open_notional", None)
        if not callable(estimator):
            return False
        try:
            min_n = await estimator(symbol, current_price)
        except Exception as e:
            logger.warning(
                "Strategy %d: %s min-notional estimate failed for %s: %s",
                strategy_id,
                action,
                symbol,
                e,
            )
            return False
        if min_n is None or min_n <= 0:
            return False
        intended = float(base_qty) * float(current_price)
        if min_n <= intended * 1.001:
            return False
        msg = (
            f"{symbol} 跳过{action} — 交易所最小约 {min_n:.2f}U > 意图 {intended:.2f}U"
        )
        logger.info("Strategy %d: %s", strategy_id, msg)
        strategy_log_service.info(strategy_id, msg)
        return True

    @staticmethod
    def _wt_like_limit(signal_source: str) -> int:
        return 200 if signal_source in ("wavetrend", "trend_wt", "wick_spike") else 100

    async def _load_klines(
        self,
        public_binance: BinanceService,
        symbol: str,
        timeframe: str,
        limit: int,
        strategy_id: int,
    ) -> list | None:
        klines = await kline_stream_manager.get(
            public_binance, symbol, timeframe, min_bars=limit
        )
        if not klines:
            try:
                klines = await public_binance.fetch_klines(symbol, timeframe, limit=limit)
            except Exception as e:
                logger.warning(
                    "Strategy %d: %s %s kline fetch failed: %s",
                    strategy_id, symbol, timeframe, e,
                )
                return None
        return klines

    async def _supertrend_bullish(
        self,
        strategy: Strategy,
        symbol: str,
        public_binance: BinanceService,
        timeframe: str,
    ) -> bool | None:
        """Return Supertrend bullish on confirmed bars; None if data insufficient."""
        atr_period = int(getattr(strategy, "st_atr_period", None) or 10)
        factor = float(getattr(strategy, "st_factor", None) or 3.0)
        limit = max(200, atr_period + 100)
        klines = await self._load_klines(
            public_binance, symbol, timeframe, limit, strategy.id
        )
        if not klines:
            return None
        confirmed = _klines_for_confirmed_signal_only(klines, timeframe)
        st = calculate_supertrend(confirmed, atr_period, factor)
        if st is None:
            return None
        return bool(st["bullish"])

    async def _trend_wt_confirm(
        self,
        strategy: Strategy,
        symbol: str,
        public_binance: BinanceService,
        klines_signal: list,
    ) -> tuple[Signal, dict] | None:
        """WaveTrend + dual-TF Supertrend filter. Returns (signal, wt) or None.

        None only when WaveTrend itself cannot be computed (same as plain WT).
        If Supertrend HTF data is missing, returns NEUTRAL so callers can still
        use strategy-TF klines for price / stop-loss management.
        """
        wt = calculate_wavetrend(
            klines_signal, strategy.wt_channel_length, strategy.wt_average_length
        )
        if wt is None:
            return None
        tf1 = getattr(strategy, "st_timeframe_1", None) or "15m"
        tf2 = getattr(strategy, "st_timeframe_2", None) or "30m"
        st1 = await self._supertrend_bullish(strategy, symbol, public_binance, tf1)
        st2 = await self._supertrend_bullish(strategy, symbol, public_binance, tf2)
        if st1 is None or st2 is None:
            # ST 数据不足：不开新仓/不加仓，但不阻断 manage 用策略周期 K 线算价
            wt = {
                **wt,
                "st1_bull": False,
                "st2_bull": False,
                "st_tf1": tf1,
                "st_tf2": tf2,
                "st_unavailable": True,
            }
            return Signal.NEUTRAL, wt
        signal = generate_trend_wt_signal(
            wt,
            strategy.direction,
            st1,
            st2,
            strategy.wt_os_level,
            strategy.wt_ob_level,
        )
        wt = {**wt, "st1_bull": st1, "st2_bull": st2, "st_tf1": tf1, "st_tf2": tf2}
        return signal, wt

    async def _fetch_klines_and_signal(
        self,
        strategy: Strategy | SimpleNamespace,
        symbol: str,
        public_binance: BinanceService,
        *,
        mutate_strategy: bool = True,
    ) -> tuple[list, float, str, Signal, float] | None:
        strategy_id = strategy.id
        limit = self._wt_like_limit(strategy.signal_source)
        klines = await self._load_klines(
            public_binance, symbol, strategy.timeframe, limit, strategy_id
        )
        if not klines:
            return None

        klines_signal = _klines_for_confirmed_signal_only(klines, strategy.timeframe)
        rsi = 0.0
        signal_label = "RSI"
        last_rsi_val: float | None = None
        if strategy.signal_source == "wick_spike":
            # 首仓由 WickSpikeRunner 负责；此处仅供持仓管理取价/K线，信号恒为中性。
            # 优先成交流最新价：forming K 线 close 在接针后常滞后（停在开盘附近），
            # 会误触发「现价已过止盈」市价兜底（HEI：K线0.204 vs 真实~0.221）。
            signal = Signal.NEUTRAL
            signal_label = "毫秒接针"
            try:
                current_price = float(klines[-1][4])
            except (TypeError, ValueError, IndexError):
                logger.warning("Strategy %d: %s invalid kline data, skipping", strategy_id, symbol)
                strategy_log_service.warning(strategy_id, f"{symbol} K线数据异常，跳过")
                return None
            try:
                from .price_stream import price_stream_manager

                got = price_stream_manager.get(symbol)
                if got and float(got[0]) > 0:
                    current_price = float(got[0])
            except Exception:
                pass
            # 不把 last_signal 写成 neutral，保留价流开仓写入的接针信息
            return klines, current_price, signal_label, signal, float(rsi or 0.0)
        if strategy.signal_source == "martingale_base":
            # 基础马丁：不看任何指标，每根 K 线开盘按策略方向直接开首单。
            signal = Signal.LONG if strategy.direction == "long" else Signal.SHORT
            last_rsi_val = 0.0
            rsi = 0.0
            signal_label = "基础马丁"
            strategy_log_service.info(
                strategy_id,
                f"{symbol} 基础马丁 → {signal.value}",
            )
        elif strategy.signal_source == "wavetrend":
            wt = calculate_wavetrend(
                klines_signal, strategy.wt_channel_length, strategy.wt_average_length
            )
            if wt is None:
                return None
            signal = generate_wt_signal(wt, strategy.direction, strategy.wt_os_level, strategy.wt_ob_level)
            last_rsi_val = round(wt["wt1"], 2)
            rsi = wt["wt1"]
            signal_label = "WT1"
            if signal != Signal.NEUTRAL:
                strategy_log_service.info(
                    strategy_id,
                    f"{symbol} WT1={wt['wt1']:.2f} WT2={wt['wt2']:.2f} 信号={signal.value}",
                )
        elif strategy.signal_source == "trend_wt":
            result = await self._trend_wt_confirm(
                strategy, symbol, public_binance, klines_signal
            )
            if result is None:
                return None
            signal, wt = result
            last_rsi_val = round(wt["wt1"], 2)
            rsi = wt["wt1"]
            signal_label = "趋势WT"
            st_tag = (
                f"ST({wt['st_tf1']}={'多' if wt['st1_bull'] else '空'},"
                f"{wt['st_tf2']}={'多' if wt['st2_bull'] else '空'})"
            )
            if signal != Signal.NEUTRAL:
                strategy_log_service.info(
                    strategy_id,
                    f"{symbol} 趋势WT WT1={wt['wt1']:.2f} WT2={wt['wt2']:.2f} {st_tag} 信号={signal.value}",
                )
            else:
                raw = generate_wt_signal(
                    wt, strategy.direction, strategy.wt_os_level, strategy.wt_ob_level
                )
                if raw != Signal.NEUTRAL:
                    reason = "ST数据不足" if wt.get("st_unavailable") else f"被超级趋势过滤 {st_tag}"
                    strategy_log_service.info(
                        strategy_id,
                        f"{symbol} 趋势WT 原始信号={raw.value} {reason}",
                    )
        else:
            rsi = calculate_rsi(klines_signal, strategy.rsi_period)
            if rsi is None:
                return None
            signal = generate_signal(rsi, strategy.direction, strategy.rsi_entry_threshold)
            last_rsi_val = round(rsi, 1)
            if signal != Signal.NEUTRAL:
                strategy_log_service.info(
                    strategy_id, f"{symbol} RSI={round(rsi, 1)} 信号={signal.value}"
                )

        if mutate_strategy and isinstance(strategy, Strategy):
            if last_rsi_val is not None:
                strategy.last_rsi = last_rsi_val
            strategy.last_signal = signal.value
            strategy.last_signal_at = now_beijing()

        try:
            current_price = float(klines[-1][4])
        except (TypeError, ValueError, IndexError):
            logger.warning("Strategy %d: %s invalid kline data, skipping", strategy_id, symbol)
            strategy_log_service.warning(strategy_id, f"{symbol} K线数据异常，跳过")
            return None

        return klines, current_price, signal_label, signal, float(rsi or 0.0)

    async def evaluate_signal(
        self,
        session: AsyncSession,
        strategy: Strategy,
        symbol: str,
        public_binance: BinanceService,
        total_margin: float,
        ctx: TickContext,
    ) -> SignalCandidate | None:
        candidate = await self.evaluate_signal_nodb(
            strategy, symbol, public_binance, total_margin, ctx
        )
        await session.flush()
        return candidate

    async def evaluate_signal_nodb(
        self,
        strategy: Strategy | SimpleNamespace,
        symbol: str,
        public_binance: BinanceService,
        total_margin: float,
        ctx: TickContext,
    ) -> SignalCandidate | None:
        """Session-free signal scan — safe to run concurrently via asyncio.gather.

        传入 strategy_signal_snapshot() 的副本；禁止写 ORM、禁止碰 AsyncSession。
        """
        if not self._passes_new_entry_filters(symbol, strategy, ctx):
            return None

        fetched = await self._fetch_klines_and_signal(
            strategy, symbol, public_binance, mutate_strategy=False
        )
        if not fetched:
            return None
        klines, current_price, signal_label, signal, rsi = fetched
        if signal == Signal.NEUTRAL:
            return None

        base_qty = self._compute_base_qty(strategy, total_margin, current_price)
        if base_qty is None:
            strategy_log_service.warning(
                strategy.id,
                f"{symbol} 无法开仓 — 余额为0(当前{total_margin:.1f})",
            )
            return None

        return SignalCandidate(
            symbol=symbol,
            signal=signal,
            klines=klines,
            current_price=current_price,
            rsi=rsi,
            signal_label=signal_label,
            base_qty=base_qty,
        )

    async def manage_symbol(
        self,
        session: AsyncSession,
        strategy: Strategy,
        symbol: str,
        auth_binance: Optional[BinanceService],
        public_binance: BinanceService,
        open_positions: list[Position],
        total_margin: float,
        leverage: float,
        ctx: TickContext,
    ) -> None:
        strategy_id = int(strategy.id)
        sym_key = _norm_sym(symbol)
        # 每次 manage 重绑 ORM，避免并行扫信号/rollback 后过期对象触发 MissingGreenlet
        fresh = await session.get(Strategy, strategy_id)
        if fresh is None or fresh.status != "running":
            return
        strategy = fresh
        result_pos = await session.execute(self._open_positions_stmt(strategy_id, sym_key))
        open_positions = list(result_pos.scalars().all())

        fetched = await self._fetch_klines_and_signal(strategy, symbol, public_binance)
        if not fetched:
            return
        klines, current_price, _signal_label, _signal, _rsi = fetched

        if auth_binance and not open_positions:
            try:
                outcome = await self._reconcile_orphan_from_exchange(
                    session,
                    strategy,
                    symbol,
                    auth_binance,
                    current_price,
                    raw_exchange_positions=ctx.raw_exchange_positions,
                )
                if outcome == RECONCILE_CREATED:
                    result = await session.execute(self._open_positions_stmt(strategy_id, sym_key))
                    open_positions = list(result.scalars().all())
            except Exception as e:
                logger.warning("Strategy %d: orphan reconcile for %s: %s", strategy_id, symbol, e)

        base_qty = self._compute_base_qty(strategy, total_margin, current_price)
        if base_qty is None:
            if open_positions:
                # Balance-dependent sizing is unavailable, but existing positions
                # must still receive TP/SL/close management. Martingale is blocked
                # below until a valid wallet balance is available again.
                layer_zero = min(open_positions, key=lambda p: p.layer)
                base_qty = float(layer_zero.quantity or 0)
            else:
                strategy_log_service.warning(
                    strategy_id, f"{symbol} 无法管理 — 余额为0(当前{total_margin:.1f})"
                )
                await session.flush()
                return

        if not auth_binance:
            await session.flush()
            return

        if open_positions:
            await self._manage_positions(
                session,
                strategy,
                symbol,
                auth_binance,
                public_binance,
                open_positions,
                base_qty,
                current_price,
                total_margin,
                leverage,
                klines,
                ctx,
            )
        else:
            await session.flush()

    async def _is_blacklisted_now(self, strategy_id: int, symbol: str) -> bool:
        """Read the blacklist from a fresh session immediately before a first-entry order."""
        from ..database import async_session

        async with async_session() as db:
            row = (
                await db.execute(
                    select(StrategySymbolBlacklist.id).where(
                        StrategySymbolBlacklist.strategy_id == strategy_id,
                        StrategySymbolBlacklist.symbol_norm == _norm_sym(symbol),
                    )
                )
            ).first()
        return row is not None

    async def _ensure_leverage_before_open(
        self,
        auth_binance: BinanceService,
        strategy_id: int,
        symbol: str,
        lev_int: int,
        *,
        quiet_cache_hit: bool = False,
    ) -> bool:
        """设杠杆；成功 True。quiet_cache_hit=True 时缓存命中不打策略日志、不走多余路径噪音。"""
        try:
            await auth_binance.ensure_markets_loaded()
            if quiet_cache_hit and auth_binance.is_leverage_cached(symbol, lev_int):
                return True
            applied, leverage_cache_hit = await auth_binance.set_symbol_leverage(symbol, lev_int)
            if leverage_cache_hit:
                if not quiet_cache_hit:
                    strategy_log_service.info(strategy_id, f"{symbol} 杠杆缓存命中 {applied}x")
            else:
                strategy_log_service.info(strategy_id, f"{symbol} 已设置交易所杠杆 {applied}x")
            return True
        except Exception as e:
            logger.error("Strategy %d: set leverage for %s failed: %s", strategy_id, symbol, e)
            strategy_log_service.error(strategy_id, f"{symbol} 设置杠杆失败，已取消开仓 — {e}")
            return False

    async def place_open_tp_limit(
        self,
        auth_binance: BinanceService,
        strategy: Strategy,
        result: OpenApiResult,
    ) -> OpenApiResult:
        """市价成交后挂止盈限价（不计入接针 open_api_ms）。"""
        if not strategy.take_profit_limit_order or result.tp_price <= 0:
            return result
        strategy_id = strategy.id
        symbol = result.symbol
        close_side = "sell" if result.position_side == "long" else "buy"
        tp_placed = False
        tp_limit_order_id: str | None = None
        for attempt in range(2):
            try:
                tp_order = await auth_binance.create_limit_order(
                    symbol,
                    close_side,
                    result.filled_qty,
                    result.tp_price,
                    reduce_only=_tp_limit_reduce_only(auth_binance),
                    position_side=result.ps,
                )
                oid = tp_order.get("id", "")
                if oid:
                    tp_limit_order_id = str(oid)
                    strategy_log_service.info(
                        strategy_id,
                        f"{symbol} 挂止盈限价单 @{result.tp_price:.6f} id={tp_limit_order_id}",
                    )
                    tp_placed = True
                    break
                strategy_log_service.warning(
                    strategy_id, f"{symbol} 挂止盈单异常 — 返回无id: {tp_order}"
                )
            except Exception as tp_err:
                logger.error(
                    "Strategy %d: TP limit order failed for %s (attempt %d): %s",
                    strategy_id, symbol, attempt + 1, tp_err,
                )
                if attempt == 0:
                    await asyncio.sleep(0.5)
        if not tp_placed:
            strategy_log_service.warning(
                strategy_id, f"{symbol} 止盈挂单失败(已重试) — 下次tick将用市价止盈兜底"
            )
        result.tp_limit_order_id = tp_limit_order_id
        return result

    async def execute_wick_open_market(
        self,
        candidate: SignalCandidate,
        strategy: Strategy,
        auth_binance: BinanceService,
        leverage: float,
    ) -> OpenApiResult | None:
        """接针热路径：仅市价开仓（杠杆缓存；下单前黑名单热复检；无挂止盈）。"""
        strategy_id = strategy.id
        symbol = candidate.symbol
        signal = candidate.signal
        base_qty = candidate.base_qty
        current_price = candidate.current_price

        side = "buy" if signal == Signal.LONG else "sell"
        ps = "LONG" if signal == Signal.LONG else "SHORT"
        position_side = "long" if side == "buy" else "short"
        lev_int = max(1, min(125, int(leverage or strategy.leverage or 10)))

        if not await self._ensure_leverage_before_open(
            auth_binance, strategy_id, symbol, lev_int, quiet_cache_hit=True
        ):
            return None

        # 黑名单已由选币池过滤 + _passes_new_entry_filters(exclude_norm) 挡住，下单前不再 DB 复检。

        if await self._should_skip_min_qty_exceeds(
            auth_binance, strategy, strategy_id, symbol, base_qty, current_price,
            action="开仓",
        ):
            return None

        try:
            order = await auth_binance.create_market_order(
                symbol, side, base_qty, position_side=ps,
            )
            # 先不回退信号价；缺均价时再查一次订单
            avg_price = _order_fill_avg_price(order, 0.0)
            if avg_price <= 0:
                oid = str(order.get("id") or "")
                if oid:
                    try:
                        order = await _fetch_order(auth_binance, oid, symbol)
                        avg_price = _order_fill_avg_price(order, 0.0)
                    except Exception as e:
                        logger.warning(
                            "Strategy %d: %s fetch_order for fill avg failed: %s",
                            strategy_id, symbol, e,
                        )
            if avg_price <= 0:
                avg_price = current_price
                logger.warning(
                    "Strategy %d: %s order filled but no average/price in response, using signal px",
                    strategy_id, symbol,
                )
            filled_qty = float(order.get("filled") or order.get("amount") or base_qty)
        except Exception as e:
            from ..services.strategy_flags import skip_min_qty_exceeds_enabled

            if skip_min_qty_exceeds_enabled(strategy) and self._is_min_qty_order_error(e):
                msg = f"{symbol} 跳过开仓 — 数量低于交易所最小要求"
                logger.info("Strategy %d: %s (%s)", strategy_id, msg, e)
                strategy_log_service.info(strategy_id, msg)
                return None
            logger.error("Strategy %d: failed to open %s: %s", strategy_id, symbol, e)
            strategy_log_service.error(strategy_id, f"{symbol} 开仓失败 — {e}")
            return None

        strategy_log_service.success(
            strategy_id,
            f"{symbol} 市价开{position_side}已成交 qty={filled_qty:.4f} "
            f"price={avg_price:.4f} {_open_signal_log_suffix(candidate.signal_label, candidate.rsi)}",
        )

        eng = MartingaleEngine(
            base_quantity=filled_qty,
            multiplier=strategy.martingale_mult,
            max_layers=strategy.max_layers,
            price_drop_multiplier=float(strategy.price_drop_multiplier or 1.0),
            take_profit_pct=strategy.take_profit_pct,
        )
        tp_price = eng.get_take_profit_price(avg_price, position_side)

        return OpenApiResult(
            symbol=symbol,
            signal=signal,
            base_qty=base_qty,
            current_price=current_price,
            rsi=candidate.rsi,
            signal_label=candidate.signal_label,
            side=side,
            position_side=position_side,
            ps=ps,
            order=order,
            avg_price=avg_price,
            filled_qty=filled_qty,
            tp_price=tp_price,
            tp_limit_order_id=None,
        )

    async def execute_open_api(
        self,
        candidate: SignalCandidate,
        strategy: Strategy,
        auth_binance: BinanceService,
        leverage: float,
    ) -> OpenApiResult | None:
        strategy_id = strategy.id
        symbol = candidate.symbol
        signal = candidate.signal
        base_qty = candidate.base_qty
        current_price = candidate.current_price

        side = "buy" if signal == Signal.LONG else "sell"
        ps = "LONG" if signal == Signal.LONG else "SHORT"
        position_side = "long" if side == "buy" else "short"

        lev_int = max(1, min(125, int(leverage or strategy.leverage or 10)))
        if not await self._ensure_leverage_before_open(
            auth_binance, strategy_id, symbol, lev_int, quiet_cache_hit=False
        ):
            return None

        try:
            if await self._is_blacklisted_now(strategy_id, symbol):
                logger.info(
                    "Strategy %d: %s was blacklisted before order submission; first entry cancelled",
                    strategy_id,
                    symbol,
                )
                strategy_log_service.info(
                    strategy_id,
                    f"{symbol} 下单前黑名单复检命中，已取消首次开仓",
                )
                return None
        except Exception as e:
            logger.error(
                "Strategy %d: blacklist recheck for %s failed; first entry cancelled: %s",
                strategy_id,
                symbol,
                e,
            )
            strategy_log_service.error(
                strategy_id,
                f"{symbol} 下单前黑名单复检失败，已安全取消首次开仓 — {e}",
            )
            return None

        if await self._should_skip_min_qty_exceeds(
            auth_binance, strategy, strategy_id, symbol, base_qty, current_price,
            action="开仓",
        ):
            return None

        try:
            order = await auth_binance.create_market_order(
                symbol, side, base_qty, position_side=ps,
            )
            avg_price = _order_fill_avg_price(order, current_price)
            if avg_price <= 0:
                avg_price = current_price
                logger.warning(
                    "Strategy %d: %s order filled but no average/price in response, using signal px",
                    strategy_id, symbol,
                )
            filled_qty = float(order.get("filled") or order.get("amount") or base_qty)
        except Exception as e:
            from ..services.strategy_flags import skip_min_qty_exceeds_enabled

            if skip_min_qty_exceeds_enabled(strategy) and self._is_min_qty_order_error(e):
                msg = f"{symbol} 跳过开仓 — 数量低于交易所最小要求"
                logger.info("Strategy %d: %s (%s)", strategy_id, msg, e)
                strategy_log_service.info(strategy_id, msg)
                return None
            logger.error("Strategy %d: failed to open %s: %s", strategy_id, symbol, e)
            strategy_log_service.error(strategy_id, f"{symbol} 开仓失败 — {e}")
            return None

        strategy_log_service.success(
            strategy_id,
            f"{symbol} 市价开{position_side}已成交 qty={filled_qty:.4f} "
            f"price={avg_price:.4f} {_open_signal_log_suffix(candidate.signal_label, candidate.rsi)}",
        )

        eng = MartingaleEngine(
            base_quantity=filled_qty,
            multiplier=strategy.martingale_mult,
            max_layers=strategy.max_layers,
            price_drop_multiplier=float(strategy.price_drop_multiplier or 1.0),
            take_profit_pct=strategy.take_profit_pct,
        )
        tp_price = eng.get_take_profit_price(avg_price, position_side)

        result = OpenApiResult(
            symbol=symbol,
            signal=signal,
            base_qty=base_qty,
            current_price=current_price,
            rsi=candidate.rsi,
            signal_label=candidate.signal_label,
            side=side,
            position_side=position_side,
            ps=ps,
            order=order,
            avg_price=avg_price,
            filled_qty=filled_qty,
            tp_price=tp_price,
            tp_limit_order_id=None,
        )
        return await self.place_open_tp_limit(auth_binance, strategy, result)

    async def execute_open_db(
        self,
        session: AsyncSession,
        strategy: Strategy,
        result: OpenApiResult,
    ) -> None:
        strategy_id = strategy.id
        symbol = _norm_sym(result.symbol)
        side = (result.position_side or "").lower()
        try:
            # 孤儿对账可能已用另一符号格式建过同向仓：合并更新，禁止再插一条全量仓
            existing = list(
                (
                    await session.execute(
                        self._open_positions_stmt(strategy_id, symbol)
                    )
                ).scalars().all()
            )
            same_side = [p for p in existing if (p.side or "").lower() == side]
            if same_side:
                # 首仓写库不应碰到马丁高层；若已有 layer>0 说明状态异常，禁止再插/覆盖
                if any(int(p.layer or 0) > 0 for p in same_side):
                    logger.error(
                        "Strategy %d: execute_open_db skipped — %s %s already has "
                        "martingale layers open",
                        strategy_id,
                        symbol,
                        side,
                    )
                    return
                # 折叠竞态产生的多条 L0，只保留一条并回填成交信息
                kept = _collapse_phantom_l0_duplicates(same_side)
                primary = kept[0]
                primary.symbol = symbol
                if result.filled_qty and float(result.filled_qty) > 0:
                    primary.quantity = float(result.filled_qty)
                if result.avg_price and float(result.avg_price) > 0:
                    primary.entry_price = float(result.avg_price)
                if result.current_price and float(result.current_price) > 0:
                    primary.mark_price = float(result.current_price)
                if result.tp_price and float(result.tp_price) > 0:
                    primary.take_profit_price = float(result.tp_price)
                oid = (result.order or {}).get("id", "")
                if oid:
                    primary.exchange_order_id = str(oid)
                if result.tp_limit_order_id:
                    primary.tp_limit_order_id = result.tp_limit_order_id
                await session.flush()
                logger.warning(
                    "Strategy %d: open DB merge into existing %s %s "
                    "(skip duplicate row; was race with orphan reconcile)",
                    strategy_id,
                    symbol,
                    side,
                )
                strategy_log_service.success(
                    strategy_id,
                    f"{symbol} 开{side}成功 qty={result.base_qty:.4f} "
                    f"price={result.avg_price:.4f} "
                    f"{_open_signal_log_suffix(result.signal_label, result.rsi)}",
                )
                return

            pos = Position(
                strategy_id=strategy_id,
                account_id=strategy.account_id,
                symbol=symbol,
                side=result.position_side,
                quantity=result.filled_qty,
                entry_price=result.avg_price,
                mark_price=result.current_price,
                layer=0,
                take_profit_price=result.tp_price,
                exchange_order_id=result.order.get("id", ""),
            )
            if result.tp_limit_order_id:
                pos.tp_limit_order_id = result.tp_limit_order_id
            session.add(pos)
            await session.flush()
        except Exception as e:
            logger.critical(
                "Strategy %d: %s order filled on exchange but DB record failed: %s",
                strategy_id, symbol, e,
            )
            strategy_log_service.error(
                strategy_id, f"{symbol} 开仓已成交但DB记录失败 — 请手动检查交易所仓位!"
            )
            raise

        logger.info(
            "Strategy %d: opened %s %s qty=%.4f price=%.4f %s",
            strategy_id,
            result.side,
            symbol,
            result.base_qty,
            result.avg_price,
            _open_signal_log_suffix(result.signal_label, result.rsi),
        )
        strategy_log_service.success(
            strategy_id,
            f"{symbol} 开{result.position_side}成功 qty={result.base_qty:.4f} "
            f"price={result.avg_price:.4f} {_open_signal_log_suffix(result.signal_label, result.rsi)}",
        )

    async def recover_bot_open_after_db_fail(
        self,
        session: AsyncSession,
        strategy: Strategy,
        result: OpenApiResult,
        auth_binance: BinanceService,
    ) -> bool:
        """市价已成交但 execute_open_db 失败时，仅用本笔成交单号补建（不领养手动仓）。"""
        oid = str((result.order or {}).get("id") or "").strip()
        if not oid:
            logger.error(
                "Strategy %d: open DB fail recover skipped — missing fill order id (%s)",
                strategy.id,
                result.symbol,
            )
            return False
        qty = float(result.filled_qty or 0) or float(getattr(result, "base_qty", 0) or 0) or None
        outcome = await self._reconcile_orphan_from_exchange(
            session,
            strategy,
            result.symbol,
            auth_binance,
            float(result.avg_price or result.current_price or 0),
            adopt=True,
            exchange_order_id=oid,
            quantity=qty,
            entry_price_override=float(result.avg_price or 0) or None,
        )
        if outcome == RECONCILE_CREATED:
            strategy_log_service.warning(
                strategy.id,
                f"{_norm_sym(result.symbol)} 写库失败后已用成交单号补建仓位记录",
            )
            return True
        return False

    async def process_symbol(
        self,
        session: AsyncSession,
        strategy: Strategy,
        symbol: str,
        auth_binance: Optional[BinanceService],
        public_binance: BinanceService,
        total_margin: float,
        leverage: float,
        *,
        allow_new_position: bool = True,
        ctx: TickContext | None = None,
    ):
        """Legacy single-symbol entry; scheduler uses evaluate/manage/execute_open directly."""
        strategy_id = strategy.id
        sym_key = _norm_sym(symbol)
        open_positions = list(
            (await session.execute(self._open_positions_stmt(strategy_id, sym_key))).scalars().all()
        )
        if ctx is None:
            ctx = await self._build_legacy_tick_context(
                strategy,
                public_binance,
                auth_binance,
                wallet_balance_valid=math.isfinite(total_margin) and total_margin > 0,
            )

        if open_positions:
            await self.manage_symbol(
                session, strategy, symbol, auth_binance, public_binance,
                open_positions, total_margin, leverage, ctx,
            )
            return

        if not allow_new_position:
            await session.flush()
            return

        candidate = await self.evaluate_signal(
            session, strategy, symbol, public_binance, total_margin, ctx,
        )
        if not candidate or not auth_binance:
            if not auth_binance and candidate:
                strategy_log_service.warning(strategy_id, f"{symbol} 无法开仓 — API未认证")
            return

        await self._open_from_candidate(
            session, strategy, candidate, auth_binance, public_binance,
            total_margin, leverage, ctx,
        )

    async def _build_legacy_tick_context(
        self,
        strategy: Strategy,
        public_binance: BinanceService,
        auth_binance: Optional[BinanceService],
        *,
        wallet_balance_valid: bool = False,
    ) -> TickContext:
        from ..services.strategy_flags import (
            exclude_delisting_enabled,
            exclude_mainstream_enabled,
            exclude_funding_enabled,
        )
        from ..services.binance_service import (
            get_strategy_pool_exclude_symbols,
            get_cached_last_funding_rates_pct,
        )

        mainstream_exclude = bool(
            strategy.use_coin_pool and exclude_mainstream_enabled(strategy)
        )
        exclude_norm: frozenset[str] = frozenset()
        if (
            getattr(strategy, "exclude_tradefi", False)
            or exclude_delisting_enabled(strategy)
            or mainstream_exclude
        ):
            excluded = await get_strategy_pool_exclude_symbols(
                public_binance,
                exclude_tradefi=bool(getattr(strategy, "exclude_tradefi", False)),
                exclude_delisting=exclude_delisting_enabled(strategy),
                exclude_mainstream=mainstream_exclude,
            )
            exclude_norm = frozenset(excluded) if excluded else frozenset()
        from ..database import async_session
        async with async_session() as db:
            bl_rows = (
                await db.execute(
                    select(StrategySymbolBlacklist.symbol_norm).where(
                        StrategySymbolBlacklist.strategy_id == strategy.id
                    )
                )
            ).scalars().all()
        if bl_rows:
            exclude_norm = frozenset(set(exclude_norm) | {(s or "").upper() for s in bl_rows if s})

        funding_rates = None
        funding_filter_enabled = False
        if strategy.use_coin_pool and exclude_funding_enabled(strategy):
            funding_filter_enabled = True
            funding_rates = await get_cached_last_funding_rates_pct(public_binance)

        raw_positions: list = []
        exchange_legs: dict[tuple[str, str], float] = {}
        if auth_binance:
            try:
                raw_positions = await auth_binance.fetch_positions()
                exchange_legs = exchange_legs_from_positions(raw_positions)
            except Exception as e:
                logger.warning("Strategy %d: fetch_positions for legacy ctx failed: %s", strategy.id, e)

        return TickContext(
            exclude_norm=exclude_norm,
            funding_rates=funding_rates,
            funding_filter_enabled=funding_filter_enabled,
            exchange_legs=exchange_legs,
            raw_exchange_positions=raw_positions,
            wallet_balance_valid=wallet_balance_valid,
        )

    async def _open_from_candidate(
        self,
        session: AsyncSession,
        strategy: Strategy,
        candidate: SignalCandidate,
        auth_binance: BinanceService,
        public_binance: BinanceService,
        total_margin: float,
        leverage: float,
        ctx: TickContext,
    ) -> None:
        strategy_id = strategy.id
        symbol = candidate.symbol
        sym_key = _norm_sym(symbol)
        side = candidate.signal.value

        if ctx.exchange_legs.get((sym_key, side), 0) > 0:
            try:
                outcome = await self._reconcile_orphan_from_exchange(
                    session,
                    strategy,
                    symbol,
                    auth_binance,
                    candidate.current_price,
                    raw_exchange_positions=ctx.raw_exchange_positions,
                )
                if outcome == RECONCILE_SKIPPED_OTHER_STRATEGY:
                    strategy_log_service.info(
                        strategy_id,
                        f"{symbol} 交易所有{side}仓，但同账户下另一策略已在本地占用「同币种+同方向」持仓，"
                        f"本策略不再重复记账/开仓（交易所该方向只有一条净仓）。"
                        f"一多一空为反方向时不会冲突；若仍看到本条，请检查两策略是否同向、或是否共抢同一池子币。",
                    )
                    return
                if outcome == RECONCILE_DB_ERROR:
                    strategy_log_service.error(
                        strategy_id,
                        f"{symbol} 写入持仓数据库失败 — 已阻止重复市价开仓，请查看 logs/bot.log 中的异常详情",
                    )
                    return
                # 默认不领养手动仓：有同向净仓则跳过新开，避免叠在手动腿上
                strategy_log_service.info(
                    strategy_id,
                    f"{symbol} 交易所已有{side}仓（可能为手动）— 不领养、不开新仓",
                )
                return
            except Exception as e:
                logger.warning(
                    "Strategy %d: reconcile before open for %s failed: %s",
                    strategy_id, symbol, e,
                )
                strategy_log_service.error(
                    strategy_id,
                    f"{symbol} 对账异常，已阻止重复市价开仓 — {e}",
                )
            return

        logger.info(
            "Strategy %d: %s signal=%s, attempting to open...",
            strategy_id, symbol, candidate.signal.value,
        )
        api_result = await self.execute_open_api(candidate, strategy, auth_binance, leverage)
        if not api_result:
            return
        try:
            await self.execute_open_db(session, strategy, api_result)
        except Exception:
            try:
                await session.rollback()
            except Exception:
                pass
            try:
                recovered = await self.recover_bot_open_after_db_fail(
                    session, strategy, api_result, auth_binance
                )
                if recovered:
                    return
            except Exception as e2:
                logger.error(
                    "Strategy %d: open DB fail recover for %s failed: %s",
                    strategy_id,
                    symbol,
                    e2,
                )

    async def _open_position(
        self,
        session,
        strategy,
        symbol,
        auth_binance,
        public_binance,
        signal,
        base_qty,
        current_price,
        total_margin,
        leverage,
        rsi,
        signal_label: str,
    ):
        candidate = SignalCandidate(
            symbol=symbol,
            signal=signal,
            klines=[],
            current_price=current_price,
            rsi=rsi,
            signal_label=signal_label,
            base_qty=base_qty,
        )
        api_result = await self.execute_open_api(candidate, strategy, auth_binance, leverage)
        if api_result:
            await self.execute_open_db(session, strategy, api_result)

    async def _manage_positions(
        self, session, strategy, symbol, auth_binance, public_binance, open_positions, base_qty, current_price, total_margin, leverage, klines=None, ctx: TickContext | None = None
    ):
        strategy_id = strategy.id
        # 只管理机器人开仓；无 exchange_order_id 视为手动/历史领养，不碰交易所
        bot_positions = _bot_owned_positions(open_positions)
        if not bot_positions:
            logger.info(
                "Strategy %d: skip manage %s — no bot-owned rows (missing exchange_order_id)",
                strategy_id,
                symbol,
            )
            return
        open_positions = bot_positions
        pos_side = open_positions[0].side

        positions_data = [{"quantity": p.quantity, "entry_price": p.entry_price} for p in open_positions]
        eng = MartingaleEngine(base_quantity=base_qty, multiplier=strategy.martingale_mult, max_layers=strategy.max_layers,
                               price_drop_pct=strategy.price_drop_pct, price_drop_multiplier=float(strategy.price_drop_multiplier or 1.0), take_profit_pct=strategy.take_profit_pct)
        avg_entry, total_qty = eng.get_avg_entry_price(positions_data)
        current_layer = max(p.layer for p in open_positions)

        # Update mark prices
        for p in open_positions:
            p.mark_price = current_price
            p.unrealized_pnl = (current_price - p.entry_price) * p.quantity if p.side == "long" else (p.entry_price - current_price) * p.quantity

        # --- Check SL / TP by price ---
        close_reason = None
        exit_price_override = current_price
        # 已挂限价止盈时：禁止用 K 线 close 软触发市价止盈（接针后 close 常滞后，
        # 会误判「已过止盈」→ 市价兜底，出场价还可能写成滞后 K 线价）。
        has_open_tp_limit = bool(strategy.take_profit_limit_order) and any(
            (p.tp_limit_order_id or "").strip() for p in open_positions
        )
        # 单币止损只用机器人记账浮亏，避免把同向手动仓盈亏算进来
        symbol_unrealized_pnl = sum(float(p.unrealized_pnl or 0) for p in open_positions)
        symbol_floating_loss = max(0.0, -symbol_unrealized_pnl)
        # total_margin 来自 tick 的 extract_margin_balance（权益，非钱包）
        margin_balance = float(total_margin)
        threshold_pct = float(getattr(strategy, "single_symbol_stop_loss_pct", 10) or 10)
        if (
            getattr(strategy, "single_symbol_stop_loss_enabled", False)
            and (ctx is None or ctx.wallet_balance_valid)
            and _single_symbol_stop_loss_trigger(
                margin_balance,
                symbol_floating_loss,
                threshold_pct,
            )
        ):
            logger.warning(
                "Strategy %d: single-symbol SL triggered %s %s loss=%.4f margin=%.4f threshold=%.4f pct=%.4f upnl_source=%s",
                strategy_id,
                symbol,
                pos_side,
                symbol_floating_loss,
                margin_balance,
                margin_balance * (threshold_pct / 100.0),
                threshold_pct,
                "bot_local",
            )
            strategy_log_service.warning(
                strategy_id,
                f"{symbol} 单币止损触发 — 浮亏 {symbol_floating_loss:.2f}U / "
                f"保证金余额 {margin_balance:.2f}U / "
                f"阈值 {margin_balance * (threshold_pct / 100.0):.2f}U",
            )
            close_reason = "single_symbol_stop_loss"
        elif strategy.stop_loss_enabled and self.risk_mgr.check_stop_loss(avg_entry, current_price, strategy.stop_loss_pct, pos_side):
            close_reason = "stop_loss"
        elif not has_open_tp_limit:
            # 将走市价止盈：触发判定必须用交易所最新价，禁止滞后 K 线 close
            live_for_tp = await _live_last_price(auth_binance, symbol, 0.0)
            if live_for_tp > 0 and eng.check_take_profit(
                avg_entry, live_for_tp, pos_side
            ):
                close_reason = "take_profit"
                exit_price_override = live_for_tp

        if close_reason:
            await self._close_positions(session, strategy, symbol, auth_binance, open_positions, eng, avg_entry, pos_side, close_reason, exit_price_override)
            return

        # --- Check TP limit order fill (before martingale, since position may already be closed) ---
        # 有挂单止盈且状态仍 open：只等限价成交，禁止「价已过止盈」市价兜底
        # （接针后价格常瞬间穿透又弹回，市价会在更差价位平掉，BICO/HEI 案例）
        if strategy.take_profit_limit_order:
            tp_filled = False
            for p in open_positions:
                if p.tp_limit_order_id:
                    try:
                        order_info = await asyncio.wait_for(
                            _fetch_order(auth_binance, p.tp_limit_order_id, symbol),
                            timeout=2.0,
                        )
                        status = (order_info.get("status") or "").lower()
                        avg_fill = _order_fill_avg_price(
                            order_info, 0.0, allow_order_price=False
                        )
                        if status in ("closed", "filled"):
                            if avg_fill <= 0:
                                logger.warning(
                                    "Strategy %d: %s TP filled but no avg/cumQuote — wait sync",
                                    strategy_id,
                                    symbol,
                                )
                                return
                            await self._close_positions(
                                session, strategy, symbol, auth_binance, open_positions, eng,
                                avg_entry, pos_side, "take_profit", current_price,
                                pre_exit_price=avg_fill, fill_order=order_info,
                            )
                            tp_filled = True
                            break
                    except (Exception, asyncio.TimeoutError):
                        pass
            if tp_filled:
                return

        # --- 补挂/补关联止盈限价单（重启丢单等）---
        await self._ensure_tp_limit_orders(
            session,
            strategy,
            symbol,
            auth_binance,
            open_positions,
            eng,
            avg_entry,
            total_qty,
            pos_side,
        )

        # --- Check martingale add ---
        last_entry = max(open_positions, key=lambda p: p.layer).entry_price
        result = eng.should_add_position(current_layer, last_entry, current_price, pos_side)
        if result.should_add:
            if ctx is not None and not ctx.wallet_balance_valid:
                strategy_log_service.warning(
                    strategy_id,
                    f"{symbol} 余额数据无效，已跳过本次马丁加仓",
                )
                await session.flush()
                return
            await self._martingale_add(session, strategy, symbol, auth_binance, open_positions, eng, result, avg_entry, total_qty, pos_side, current_price, klines, public_binance)
            return

        await session.flush()

    async def check_tp_fills(self, session, strategy, auth_binance, current_price: float):
        from ..models.position import Position
        from .strategy_concurrency import hold_strategy_symbol

        strategy_id = strategy.id
        stmt = select(Position).where(
            Position.strategy_id == strategy_id, Position.closed_at.is_(None)
        )
        result = await session.execute(stmt)
        open_positions = _bot_owned_positions(list(result.scalars().all()))

        processed_symbols: set[tuple[str, str]] = set()
        for p in open_positions:
            # 有止盈单 ID 即检测；不依赖 take_profit_price（补关联后可能为空）
            if not p.tp_limit_order_id:
                continue
            # 必须按规范符号去重：BMTUSDT 与 BMT/USDT:USDT 是同一腿
            sym_norm = _norm_sym(p.symbol)
            symbol_side_key = (sym_norm, (p.side or "").lower())
            if symbol_side_key in processed_symbols:
                continue
            # 腿锁：与接针同币同向开仓互斥；其它币/反向腿仍可进行
            try:
                async with hold_strategy_symbol(strategy_id, sym_norm, p.side):
                    order_info = await asyncio.wait_for(
                        _fetch_order(auth_binance, p.tp_limit_order_id, p.symbol),
                        timeout=2.0,
                    )
                    status = (order_info.get("status") or "").lower()
                    avg_fill = _order_fill_avg_price(
                        order_info, 0.0, allow_order_price=False
                    )
                    if status in ("closed", "filled"):
                        if avg_fill <= 0:
                            logger.warning(
                                "Strategy %d: TP order %s filled but no avg/cumQuote for %s — skip close, retry",
                                strategy_id,
                                p.tp_limit_order_id,
                                p.symbol,
                            )
                            continue
                        symbol_positions = [
                            op
                            for op in open_positions
                            if _norm_sym(op.symbol) == sym_norm
                            and (op.side or "").lower() == symbol_side_key[1]
                            and op.closed_at is None
                        ]
                        if not symbol_positions:
                            continue
                        # 折叠符号竞态留下的重复 L0，避免一条交易所腿写出多条 Trade
                        symbol_positions = _collapse_phantom_l0_duplicates(symbol_positions)
                        symbol_positions = [
                            op for op in symbol_positions if op.closed_at is None
                        ]
                        if not symbol_positions:
                            continue
                        positions_data = [{"quantity": op.quantity, "entry_price": op.entry_price} for op in symbol_positions]
                        eng = MartingaleEngine(base_quantity=symbol_positions[0].quantity, multiplier=strategy.martingale_mult,
                                               max_layers=strategy.max_layers, price_drop_multiplier=float(strategy.price_drop_multiplier or 1.0), take_profit_pct=strategy.take_profit_pct)
                        avg_entry, _ = eng.get_avg_entry_price(positions_data)
                        await self._close_positions(
                            session, strategy, sym_norm, auth_binance, symbol_positions,
                            eng, avg_entry, p.side, "take_profit", current_price,
                            pre_exit_price=avg_fill, fill_order=order_info,
                        )
                        logger.info("Strategy %d: TP fill detected mid-candle for %s @%.4f", strategy_id, sym_norm, avg_fill)
                        processed_symbols.add(symbol_side_key)
            except (Exception, asyncio.TimeoutError):
                logger.warning("Strategy %d: TP order check failed for %s %s, retrying next cycle", strategy_id, p.symbol, p.side)

    async def _close_positions(
        self,
        session,
        strategy,
        symbol,
        auth_binance,
        open_positions,
        eng,
        avg_entry,
        pos_side,
        close_reason,
        current_price,
        pre_exit_price: float = 0.0,
        fill_order: dict | None = None,
    ):
        strategy_id = strategy.id
        # 双保险：市价减仓只针对机器人开仓行
        open_positions = _bot_owned_positions(list(open_positions or []))
        if not open_positions:
            logger.info(
                "Strategy %d: _close_positions skip %s — no bot-owned rows",
                strategy_id,
                symbol,
            )
            return

        exit_price = 0.0

        if pre_exit_price > 0:
            exit_price = pre_exit_price
            if fill_order is None:
                for p in open_positions:
                    if p.tp_limit_order_id:
                        try:
                            fill_order = await asyncio.wait_for(
                                _fetch_order(auth_binance, p.tp_limit_order_id, symbol),
                                timeout=3.0,
                            )
                        except (Exception, asyncio.TimeoutError):
                            pass
                        break
            strategy_log_service.success(strategy_id, f"{symbol} 止盈平仓 — 限价单已成交 @{exit_price:.4f}")
            logger.info("Strategy %d: TP limit filled for %s @%.4f", strategy_id, symbol, exit_price)
        elif close_reason == "take_profit" and strategy.take_profit_limit_order:
            has_tp_order = any(p.tp_limit_order_id for p in open_positions)
            if not has_tp_order:
                strategy_log_service.warning(strategy_id, f"{symbol} 止盈条件触发但无限价单ID — 兜底市价平仓")
                logger.warning("Strategy %d: %s TP triggered but no tp_limit_order_id, falling back to market close", strategy_id, symbol)
            else:
                tp_order_id = None
                for p in open_positions:
                    if p.tp_limit_order_id:
                        tp_order_id = p.tp_limit_order_id
                        break
                if tp_order_id:
                    try:
                        order_info = await asyncio.wait_for(
                            _fetch_order(auth_binance, tp_order_id, symbol),
                            timeout=3.0,
                        )
                        order_status = (order_info.get("status") or "").lower()
                        avg_fill = _order_fill_avg_price(
                            order_info, 0.0, allow_order_price=False
                        )
                        if order_status in ("closed", "filled"):
                            if avg_fill > 0:
                                exit_price = avg_fill
                                fill_order = order_info
                                strategy_log_service.success(strategy_id, f"{symbol} 止盈限价单已成交 @{exit_price:.4f}")
                                logger.info("Strategy %d: TP limit already filled for %s @%.4f", strategy_id, symbol, exit_price)
                            else:
                                # 已成交但均价字段缺失：禁止市价兜底（仓已平），等 sync/下次解析
                                strategy_log_service.warning(
                                    strategy_id,
                                    f"{symbol} 止盈单已成交但无成交均价 — 暂不写库，等待同步",
                                )
                                logger.warning(
                                    "Strategy %d: %s TP filled status=%s but avg=0 — skip market fallback",
                                    strategy_id,
                                    symbol,
                                    order_status,
                                )
                                return
                        elif order_status in ("canceled", "cancelled", "expired"):
                            strategy_log_service.warning(strategy_id, f"{symbol} 止盈限价单已取消/过期 — 兜底市价平仓")
                            logger.warning("Strategy %d: %s TP order %s, falling back to market close", strategy_id, symbol, order_status)
                            for p in open_positions:
                                p.tp_limit_order_id = None
                        else:
                            # 限价仍挂着：只等成交，不做「价已过止盈」市价兜底
                            strategy_log_service.info(
                                strategy_id,
                                f"{symbol} 止盈限价单状态={order_status}，等待限价成交",
                            )
                            return
                    except (Exception, asyncio.TimeoutError) as e:
                        logger.warning("Strategy %d: TP order check failed for %s: %s — waiting for check_tp_fills", strategy_id, symbol, e)
                        return
                else:
                    strategy_log_service.warning(strategy_id, f"{symbol} 止盈条件触发但无限价单 — 兜底市价平仓")

            if exit_price <= 0:
                for p in open_positions:
                    if p.tp_limit_order_id:
                        try:
                            await auth_binance.cancel_order(p.tp_limit_order_id, symbol)
                        except Exception:
                            pass
                        p.tp_limit_order_id = None
                try:
                    close_qty = sum(
                        float(p.quantity or 0)
                        for p in open_positions
                        if p.closed_at is None
                    )
                    result = await auth_binance.close_position_qty(
                        symbol, pos_side, close_qty
                    )
                    if result and result.get("id"):
                        # fallback=0：禁止用 K 线价写库，必须解析市价成交均价
                        exit_price, fill_order = await _resolve_market_close_exit(
                            auth_binance, symbol, result, 0.0
                        )
                        if exit_price <= 0:
                            strategy_log_service.error(
                                strategy_id,
                                f"{symbol} 市价止盈已提交但未解析到成交均价 — 请核对交易所后手动处理",
                            )
                            return
                        close_reason = "take_profit"
                        strategy_log_service.success(
                            strategy_id,
                            f"{symbol} 兜底市价止盈 成交均价@{exit_price:.8g} qty={close_qty:g}",
                        )
                    else:
                        strategy_log_service.warning(strategy_id, f"{symbol} 兜底平仓失败 — 未找到交易所仓位")
                        return
                except Exception as e:
                    logger.error("Strategy %d: fallback market close failed: %s", strategy_id, e)
                    strategy_log_service.error(strategy_id, f"{symbol} 兜底平仓异常 — {e}")
                    return
        else:
            for p in open_positions:
                if p.tp_limit_order_id:
                    try:
                        order_info = await asyncio.wait_for(
                            _fetch_order(auth_binance, p.tp_limit_order_id, symbol),
                            timeout=2.0,
                        )
                        order_status = (order_info.get("status") or "").lower()
                        avg_fill = _order_fill_avg_price(
                            order_info, 0.0, allow_order_price=False
                        )
                        if order_status in ("closed", "filled"):
                            if avg_fill > 0:
                                exit_price = avg_fill
                                fill_order = order_info
                                strategy_log_service.success(strategy_id, f"{symbol} 止盈限价单已成交 @{exit_price:.4f}（止损检查时发现）")
                                logger.info("Strategy %d: TP limit already filled during SL check for %s @%.4f", strategy_id, symbol, exit_price)
                                close_reason = "take_profit"
                                break
                            # 已成交无均价：勿撤单/市价（仓可能已平）
                            logger.warning(
                                "Strategy %d: %s TP filled during SL check but avg=0 — skip",
                                strategy_id,
                                symbol,
                            )
                            return
                    except (Exception, asyncio.TimeoutError):
                        pass
                    try:
                        await auth_binance.cancel_order(p.tp_limit_order_id, symbol)
                    except Exception:
                        pass
                    p.tp_limit_order_id = None

            if exit_price <= 0:
                try:
                    close_qty = sum(
                        float(p.quantity or 0)
                        for p in open_positions
                        if p.closed_at is None
                    )
                    result = await auth_binance.close_position_qty(
                        symbol, pos_side, close_qty
                    )
                    if result and result.get("id"):
                        exit_price, fill_order = await _resolve_market_close_exit(
                            auth_binance, symbol, result, 0.0
                        )
                        if exit_price <= 0:
                            strategy_log_service.error(
                                strategy_id,
                                f"{symbol} 市价平仓已提交但未解析到成交均价 — 请核对交易所",
                            )
                            return
                    else:
                        strategy_log_service.warning(strategy_id, f"{symbol} 平仓失败 — 未找到交易所仓位")
                        return
                except Exception as e:
                    logger.error("Strategy %d: close position failed: %s", strategy_id, e)
                    strategy_log_service.error(strategy_id, f"{symbol} 平仓异常 — {e}")
                    return

        # Common: create Trade records and mark positions closed
        sym_norm = _norm_sym(symbol)
        logger.info("Strategy %d: closed %s due to %s", strategy_id, sym_norm, close_reason)
        detected_at = now_beijing()
        exit_time = exit_time_from_order(fill_order, fallback=detected_at)
        # 写 Trade 前再折叠一次，兜住 manage/TP/止损等所有平仓入口
        open_positions = _collapse_phantom_l0_duplicates(
            list(open_positions), now=exit_time
        )
        # 只扣本策略实际入账平仓量，保留同向手动仓残余
        bot_close_qty = sum(
            float(p.quantity or 0)
            for p in open_positions
            if getattr(p, "closed_at", None) is None
        )
        try:
            from .account_position_stream import account_position_stream

            acc_id = int(getattr(strategy, "account_id", 0) or 0)
            if acc_id > 0 and bot_close_qty > 0:
                account_position_stream.apply_local_close(
                    acc_id, symbol, pos_side, bot_close_qty
                )
        except Exception:
            logger.debug(
                "Strategy %d: clear account leg after close failed",
                strategy_id,
                exc_info=True,
            )
        trades_to_backup: list[Trade] = []
        for p in open_positions:
            if p.closed_at is not None:
                continue
            p.closed_at = exit_time
            p.symbol = sym_norm
            exit_pnl = (exit_price - p.entry_price) * p.quantity if p.side == "long" else (p.entry_price - exit_price) * p.quantity
            exit_pnl_pct = (exit_price - p.entry_price) / p.entry_price * 100 if p.side == "long" else (p.entry_price - exit_price) / p.entry_price * 100
            trade = Trade(
                strategy_id=strategy_id, account_id=strategy.account_id, symbol=sym_norm,
                side=p.side, quantity=p.quantity, entry_price=p.entry_price, exit_price=exit_price,
                realized_pnl=exit_pnl, pnl_pct=exit_pnl_pct,
                entry_time=p.opened_at or exit_time, exit_time=exit_time, layer=p.layer, close_reason=close_reason,
            )
            session.add(trade)
            trades_to_backup.append(trade)
        if close_reason == "single_symbol_stop_loss":
            sym_norm = _norm_sym(symbol)
            exists = (
                await session.execute(
                    select(StrategySymbolBlacklist.id).where(
                        StrategySymbolBlacklist.strategy_id == strategy_id,
                        StrategySymbolBlacklist.symbol_norm == sym_norm,
                    )
                )
            ).first()
            if not exists:
                session.add(
                    StrategySymbolBlacklist(
                        strategy_id=strategy_id,
                        symbol=symbol,
                        symbol_norm=sym_norm,
                        reason="single_symbol_stop_loss",
                    )
                )
            strategy_log_service.warning(
                strategy_id,
                f"{symbol} 触发单币止损(浮亏达钱包余额10%)，已加入黑名单，不再开新仓",
            )
        await session.flush()
        for trade in trades_to_backup:
            backup_trade(trade)

    async def _martingale_add(self, session, strategy, symbol, auth_binance, open_positions, eng, result, avg_entry, total_qty, pos_side, current_price, klines=None, public_binance=None):
        strategy_id = strategy.id
        side = "buy" if pos_side == "long" else "sell"
        ps = "LONG" if pos_side == "long" else "SHORT"

        # Signal re-check for martingale add (if enabled).
        # 基础马丁：永不做信号确认。
        # 接针：仅当 wick_martingale_mode=price_and_wt 时，在跌幅达标后再做 WT 确认。
        if (
            strategy.signal_source == "wick_spike"
            and wick_martingale_mode_needs_wt(
                getattr(strategy, "wick_martingale_mode", None)
            )
            and klines is not None
        ):
            klines_confirm = _klines_for_confirmed_signal_only(klines, strategy.timeframe)
            ok, detail = martingale_wt_confirm_allows_add(
                klines_confirm,
                direction=strategy.direction,
                wt_channel_length=strategy.wt_channel_length,
                wt_average_length=strategy.wt_average_length,
                wt_os_level=strategy.wt_os_level,
                wt_ob_level=strategy.wt_ob_level,
            )
            if not ok:
                strategy_log_service.info(
                    strategy_id, f"{symbol} 马丁加仓跳过 — {detail}"
                )
                return
            if "跳过确认" not in detail:
                strategy_log_service.info(
                    strategy_id, f"{symbol} 马丁加仓WT确认 — {detail}"
                )
        elif (
            strategy.martingale_rsi_enabled
            and strategy.signal_source not in ("martingale_base", "wick_spike")
            and klines is not None
            and public_binance is not None
        ):
            klines_confirm = _klines_for_confirmed_signal_only(klines, strategy.timeframe)
            if strategy.signal_source == "wavetrend":
                ok, detail = martingale_wt_confirm_allows_add(
                    klines_confirm,
                    direction=strategy.direction,
                    wt_channel_length=strategy.wt_channel_length,
                    wt_average_length=strategy.wt_average_length,
                    wt_os_level=strategy.wt_os_level,
                    wt_ob_level=strategy.wt_ob_level,
                )
                if not ok:
                    strategy_log_service.info(
                        strategy_id, f"{symbol} 马丁加仓跳过 — {detail}"
                    )
                    return
                if "跳过确认" not in detail:
                    strategy_log_service.info(
                        strategy_id, f"{symbol} 马丁加仓WT确认 — {detail}"
                    )
            elif strategy.signal_source == "trend_wt":
                # 默认只用 WT 确认加仓；开启 martingale_st_filter_enabled 才叠加超级趋势
                use_st = bool(getattr(strategy, "martingale_st_filter_enabled", False))
                if use_st:
                    tw_result = await self._trend_wt_confirm(
                        strategy, symbol, public_binance, klines_confirm
                    )
                    if tw_result is not None:
                        confirm, wt = tw_result
                        if confirm == Signal.NEUTRAL:
                            strategy_log_service.info(
                                strategy_id,
                                f"{symbol} 马丁加仓跳过 — 趋势WT WT1={wt['wt1']:.2f} 信号已消失/被ST过滤",
                            )
                            return
                        strategy_log_service.info(
                            strategy_id,
                            f"{symbol} 马丁加仓趋势WT确认 — WT1={wt['wt1']:.2f} "
                            f"ST({wt['st_tf1']}={'多' if wt['st1_bull'] else '空'},"
                            f"{wt['st_tf2']}={'多' if wt['st2_bull'] else '空'})",
                        )
                else:
                    wt = calculate_wavetrend(
                        klines_confirm, strategy.wt_channel_length, strategy.wt_average_length
                    )
                    if wt is not None:
                        confirm = generate_wt_signal(
                            wt, strategy.direction, strategy.wt_os_level, strategy.wt_ob_level
                        )
                        if confirm == Signal.NEUTRAL:
                            strategy_log_service.info(
                                strategy_id,
                                f"{symbol} 马丁加仓跳过 — WT1={wt['wt1']:.2f} 信号已消失",
                            )
                            return
                        strategy_log_service.info(
                            strategy_id,
                            f"{symbol} 马丁加仓WT确认 — WT1={wt['wt1']:.2f} WT2={wt['wt2']:.2f}",
                        )
            else:
                rsi_val = calculate_rsi(klines_confirm, strategy.rsi_period)
                if rsi_val is not None:
                    confirm = generate_signal(rsi_val, strategy.direction, strategy.rsi_entry_threshold)
                    if confirm == Signal.NEUTRAL:
                        strategy_log_service.info(strategy_id, f"{symbol} 马丁加仓跳过 — RSI={round(rsi_val,1)} 信号已消失")
                        return
                    strategy_log_service.info(strategy_id, f"{symbol} 马丁加仓RSI确认 — RSI={round(rsi_val,1)}")

        # Step 1: execute add order FIRST
        if await self._should_skip_min_qty_exceeds(
            auth_binance,
            strategy,
            strategy_id,
            symbol,
            float(result.next_quantity),
            current_price,
            action="马丁加仓",
        ):
            return

        try:
            order = await auth_binance.create_market_order(
                symbol, side, result.next_quantity, position_side=ps,
            )
            new_avg = _order_fill_avg_price(order, 0.0, allow_order_price=False)
            if new_avg <= 0:
                new_avg = current_price
                logger.warning("Strategy %d: %s martingale order filled but no average/price in response, using kline close", strategy_id, symbol)
            filled_qty = float(order.get("filled") or order.get("amount") or result.next_quantity)
        except Exception as e:
            from ..services.strategy_flags import skip_min_qty_exceeds_enabled

            if skip_min_qty_exceeds_enabled(strategy) and self._is_min_qty_order_error(e):
                msg = f"{symbol} 跳过马丁加仓 — 数量低于交易所最小要求"
                logger.info("Strategy %d: %s (%s)", strategy_id, msg, e)
                strategy_log_service.info(strategy_id, msg)
                return
            logger.error("Strategy %d: martingale add failed: %s", strategy_id, e)
            strategy_log_service.error(strategy_id, f"{symbol} 马丁加仓失败 — {e}")
            return

        # Step 2: record in DB
        try:
            new_total = total_qty + filled_qty
            new_avg_entry = (avg_entry * total_qty + new_avg * filled_qty) / new_total
            tp_price = eng.get_take_profit_price(new_avg_entry, pos_side)

            pos = Position(
                strategy_id=strategy_id, account_id=strategy.account_id,
                symbol=_norm_sym(symbol), side=pos_side, quantity=filled_qty,
                entry_price=new_avg, mark_price=current_price, layer=result.next_layer,
                take_profit_price=tp_price, exchange_order_id=order.get("id", ""),
            )
            session.add(pos)
            await session.flush()
        except Exception as e:
            logger.critical("Strategy %d: %s martingale order filled but DB record failed: %s", strategy_id, symbol, e)
            strategy_log_service.error(strategy_id, f"{symbol} 马丁加仓已成交但DB记录失败 — 请手动检查交易所仓位!")
            return

        # Step 3: 确认撤销旧止盈后才清 ID；撤不干净则禁止新挂，避免币安重复限价
        old_tp_cleared = True
        if strategy.take_profit_limit_order:
            for p in open_positions:
                oid = (p.tp_limit_order_id or "").strip()
                if not oid:
                    continue
                ok = await self._cancel_tp_order_confirmed(auth_binance, oid, symbol)
                if ok:
                    strategy_log_service.info(strategy_id, f"{symbol} 取消旧止盈单 {oid}")
                    p.tp_limit_order_id = None
                else:
                    old_tp_cleared = False
                    strategy_log_service.warning(
                        strategy_id,
                        f"{symbol} 旧止盈单 {oid} 未能确认撤销 — 跳过新挂以防重复",
                    )

        # Step 4: place new combined TP order (best-effort)
        if strategy.take_profit_limit_order:
            if not old_tp_cleared:
                strategy_log_service.warning(
                    strategy_id,
                    f"{symbol} 加仓后止盈未更新（旧单未撤净）；下次 manage 将尝试关联/去重",
                )
            else:
                # 旧机器人止盈已撤：按策略总数量新挂，不认领/不撤手动限价
                tp_placed = False
                close_side = "sell" if pos_side == "long" else "buy"
                for attempt in range(2):
                    try:
                        tp_order = await auth_binance.create_limit_order(
                            symbol,
                            close_side,
                            new_total,
                            tp_price,
                            reduce_only=_tp_limit_reduce_only(auth_binance),
                            position_side=ps,
                        )
                        tp_order_id = tp_order.get("id", "")
                        if tp_order_id:
                            pos.tp_limit_order_id = tp_order_id
                            for p in open_positions:
                                p.tp_limit_order_id = str(tp_order_id)
                                p.take_profit_price = tp_price
                            await session.flush()
                            strategy_log_service.info(
                                strategy_id,
                                f"{symbol} 更新止盈挂单 @{tp_price:.6f} qty={new_total:.4f}",
                            )
                            tp_placed = True
                            break
                        strategy_log_service.warning(
                            strategy_id, f"{symbol} 更新止盈单异常 — 返回无id: {tp_order}"
                        )
                    except Exception as tp_err:
                        logger.error(
                            "Strategy %d: TP limit update failed for %s (attempt %d): %s",
                            strategy_id,
                            symbol,
                            attempt + 1,
                            tp_err,
                        )
                        if attempt == 0:
                            await asyncio.sleep(0.5)
                if not tp_placed:
                    strategy_log_service.warning(
                        strategy_id,
                        f"{symbol} 止盈挂单更新失败(已重试) — 下次tick将用市价止盈兜底",
                    )

        logger.info("Strategy %d: martingale add layer %d for %s qty=%.4f price=%.4f drop=%.1f%%",
                    strategy_id, result.next_layer, symbol, result.next_quantity, new_avg, result.price_drop_from_last)
        strategy_log_service.info(strategy_id, f"{symbol} 马丁加仓 L{result.next_layer} qty={result.next_quantity:.4f} 跌幅={result.price_drop_from_last:.1f}%")
