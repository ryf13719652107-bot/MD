"""Per-strategy-tick shared context: filters and exchange snapshot computed once."""
from dataclasses import dataclass, field
from typing import Any

from .strategy_engine import Signal


def exchange_legs_from_positions(raw_positions: list) -> dict[tuple[str, str], float]:
    leg_map: dict[tuple[str, str], float] = {}
    for ep in raw_positions:
        contracts = float(ep.get("contracts", 0) or 0)
        if contracts <= 0:
            continue
        sym = (ep.get("symbol") or "").replace("/", "").replace(":USDT", "").upper()
        side = (ep.get("side") or "").lower()
        leg_map[(sym, side)] = leg_map.get((sym, side), 0) + contracts
    return leg_map


@dataclass
class TickContext:
    exclude_norm: frozenset[str] = field(default_factory=frozenset)
    funding_rates: dict[str, float] | None = None
    funding_filter_enabled: bool = False
    exchange_legs: dict[tuple[str, str], float] = field(default_factory=dict)
    raw_exchange_positions: list[dict[str, Any]] = field(default_factory=list)
    allow_new_norms: frozenset[str] | None = None
    wallet_balance_valid: bool = False


@dataclass
class SignalCandidate:
    symbol: str
    signal: Signal
    klines: list
    current_price: float
    rsi: float
    signal_label: str
    base_qty: float


@dataclass
class OpenApiResult:
    symbol: str
    signal: Signal
    base_qty: float
    current_price: float
    rsi: float
    signal_label: str
    side: str
    position_side: str
    ps: str
    order: dict
    avg_price: float
    filled_qty: float
    tp_price: float = 0.0
    tp_limit_order_id: str | None = None
