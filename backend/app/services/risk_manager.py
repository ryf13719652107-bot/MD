from dataclasses import dataclass
from typing import Optional


@dataclass
class RiskCheckResult:
    passed: bool
    reason: str = ""


class RiskManager:
    def __init__(
        self,
        max_total_positions: int = 20,
        max_position_per_symbol: int = 8,
        max_exposure_ratio: float = 0.5,
    ):
        self.max_total_positions = max_total_positions
        self.max_position_per_symbol = max_position_per_symbol
        self.max_exposure_ratio = max_exposure_ratio

    def can_open_position(
        self,
        open_positions: list,
        symbol: str,
        total_balance: float,
        new_position_value: float,
    ) -> RiskCheckResult:
        """Check if a new position can be opened based on risk limits."""
        # Count open positions
        active_positions = [p for p in open_positions if p.closed_at is None]
        if len(active_positions) >= self.max_total_positions:
            return RiskCheckResult(False, f"Max total positions ({self.max_total_positions}) reached")

        # Count positions per symbol
        symbol_positions = [
            p for p in active_positions if (p.symbol or "").replace("/", "") == symbol.replace("/", "")
        ]
        if len(symbol_positions) >= self.max_position_per_symbol:
            return RiskCheckResult(False, f"Max positions per symbol ({self.max_position_per_symbol}) reached")

        # Check exposure ratio
        total_exposure = sum(
            abs(float(p.quantity or 0)) * float(p.mark_price or p.entry_price or 0)
            for p in active_positions
        )
        if total_balance > 0 and (total_exposure + new_position_value) / total_balance > self.max_exposure_ratio:
            return RiskCheckResult(False, f"Max exposure ratio ({self.max_exposure_ratio * 100}%) exceeded")

        return RiskCheckResult(True)

    def check_stop_loss(
        self, entry_price: float, current_price: float, stop_loss_pct: float, side: str
    ) -> bool:
        """Check if stop loss should trigger."""
        if side == "long":
            pnl_pct = ((current_price - entry_price) / entry_price) * 100
        else:
            pnl_pct = ((entry_price - current_price) / entry_price) * 100
        return pnl_pct <= -abs(stop_loss_pct)

    def check_margin_threshold(self, total_margin: float, margin_threshold: float) -> bool:
        """Check if margin is below critical threshold."""
        return total_margin < margin_threshold
