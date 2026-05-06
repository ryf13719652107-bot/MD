from dataclasses import dataclass
from typing import Optional


@dataclass
class MartingaleResult:
    should_add: bool
    next_quantity: float
    next_layer: int
    avg_entry_price: float
    price_drop_from_last: float


class MartingaleEngine:
    def __init__(
        self,
        base_quantity: float,
        multiplier: float = 1.5,
        max_layers: int = 8,
        price_drop_pct: float = 30.0,
        take_profit_pct: float = 2.0,
    ):
        self.base_quantity = base_quantity
        self.multiplier = multiplier
        self.max_layers = max_layers
        self.price_drop_pct = price_drop_pct
        self.take_profit_pct = take_profit_pct

    def calculate_position_size(self, layer: int) -> float:
        """Calculate position size for a given layer using martingale multiplier."""
        return self.base_quantity * (self.multiplier ** min(layer, self.max_layers - 1))

    def should_add_position(
        self,
        current_layer: int,
        last_entry_price: float,
        current_price: float,
        side: str,
    ) -> MartingaleResult:
        """Determine if a new position should be added based on price movement."""
        if current_layer >= self.max_layers:
            return MartingaleResult(False, 0, current_layer, last_entry_price, 0)

        if side == "long":
            price_drop_pct = ((last_entry_price - current_price) / last_entry_price) * 100
        else:
            price_drop_pct = ((current_price - last_entry_price) / last_entry_price) * 100

        should_add = price_drop_pct >= self.price_drop_pct

        next_layer = current_layer + 1 if should_add else current_layer
        next_quantity = self.calculate_position_size(next_layer) if should_add else 0

        return MartingaleResult(
            should_add=should_add,
            next_quantity=next_quantity,
            next_layer=next_layer,
            avg_entry_price=last_entry_price,
            price_drop_from_last=price_drop_pct,
        )

    def check_take_profit(
        self, avg_entry_price: float, current_price: float, side: str
    ) -> bool:
        """Check if take profit condition is met."""
        if side == "long":
            profit_pct = ((current_price - avg_entry_price) / avg_entry_price) * 100
        else:
            profit_pct = ((avg_entry_price - current_price) / avg_entry_price) * 100
        return profit_pct >= self.take_profit_pct

    def get_take_profit_price(self, avg_entry_price: float, side: str) -> float:
        """Calculate take profit price."""
        if side == "long":
            return avg_entry_price * (1 + self.take_profit_pct / 100)
        else:
            return avg_entry_price * (1 - self.take_profit_pct / 100)

    def get_avg_entry_price(self, positions: list[dict]) -> tuple[float, float]:
        """Calculate average entry price and total quantity from positions."""
        total_qty = sum(abs(p["quantity"]) if isinstance(p, dict) else abs(p.quantity) for p in positions)
        if total_qty == 0:
            return 0.0, 0.0
        weighted_price = sum(
            (abs(p["quantity"]) if isinstance(p, dict) else abs(p.quantity))
            * (p["entry_price"] if isinstance(p, dict) else p.entry_price)
            for p in positions
        )
        return weighted_price / total_qty, total_qty
