"""按账户/交易所名路由到 BinanceService 或 GateService。"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from .binance_service import (
    BinanceService,
    clear_private_binance_service,
    get_binance_service,
    get_public_binance,
)
from .gate_service import (
    GateService,
    clear_private_gate_service,
    get_gate_service,
    get_public_gate,
)

logger = logging.getLogger(__name__)

SUPPORTED_EXCHANGES = frozenset({"binance", "gate"})


def normalize_exchange_id(exchange: str | None) -> str:
    ex = (exchange or "binance").strip().lower()
    if ex not in SUPPORTED_EXCHANGES:
        return "binance"
    return ex


def account_exchange_id(account: Any) -> str:
    return normalize_exchange_id(getattr(account, "exchange", None))


@runtime_checkable
class ExchangeClient(Protocol):
    exchange_id: str
    hedge_mode: bool

    def begin_tick(self) -> None: ...
    def pin(self) -> None: ...
    def unpin(self) -> None: ...

    async def ensure_markets_loaded(self) -> None: ...
    async def fetch_balance(self) -> dict: ...
    async def fetch_ticker(self, symbol: str) -> dict: ...
    async def fetch_tickers(self, symbols: list[str] | None = None) -> dict: ...
    async def fetch_klines(self, symbol: str, timeframe: str = "1m", limit: int = 100) -> list: ...
    async def fetch_positions(self, symbols: list[str] | None = None) -> list[dict]: ...
    async def set_symbol_leverage(self, symbol: str, leverage: int) -> tuple[int, bool]: ...
    async def cancel_order(self, order_id: str, symbol: str) -> dict: ...
    async def estimate_min_open_notional(
        self, symbol: str, price: float
    ) -> float | None: ...
    async def create_market_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        reduce_only: bool = False,
        position_side: str = "LONG",
        slippage_pct: float | None = None,
    ) -> dict: ...
    async def create_limit_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float,
        reduce_only: bool = False,
        position_side: str = "LONG",
    ) -> dict: ...
    async def close_position(self, symbol: str, side: str) -> dict: ...
    async def close_position_with_limit(self, symbol: str, side: str, price: float) -> dict: ...
    async def fetch_top_movers(self, source: str = "both", limit: int = 20) -> list[dict]: ...
    async def fetch_tradefi_perpetual_symbols_raw(self) -> set[str]: ...
    async def fetch_delisting_soon_symbols_raw(self) -> set[str]: ...
    async def fetch_last_funding_rates_pct_raw(self) -> dict[str, float]: ...
    async def watch_tickers(self, symbols: list[str] | None = None): ...
    async def watch_klines(self, symbol: str, timeframe: str = "1m"): ...
    async def watch_trades(self, symbol: str): ...
    async def close(self) -> None: ...


async def get_public_exchange(exchange: str | None = "binance") -> ExchangeClient:
    ex = normalize_exchange_id(exchange)
    if ex == "gate":
        return await get_public_gate()
    return await get_public_binance()


async def get_exchange_for_account(account: Any) -> ExchangeClient:
    from .encryption import decrypt

    ex = account_exchange_id(account)
    api_key = decrypt(account.api_key_encrypted)
    api_secret = decrypt(account.api_secret_encrypted)
    if ex == "gate":
        return await get_gate_service(
            api_key,
            api_secret,
            hedge_mode=bool(getattr(account, "hedge_mode", True)),
        )
    return await get_binance_service(
        api_key,
        api_secret,
        bool(getattr(account, "testnet", False)),
        bool(getattr(account, "hedge_mode", True)),
    )


def extract_margin_balance(client: Any, balance: dict) -> float:
    """币安 App「保证金余额」/ Gate 合约权益（钱包 + 未实现盈亏）。

    用于：收益曲线小时快照、保证金阈值止损、单币止损分母。
    """
    if getattr(client, "exchange_id", None) == "gate":
        from .gate_service import extract_gate_usdt_margin_balance

        return extract_gate_usdt_margin_balance(balance)
    from .binance_service import extract_usdt_margin_balance

    return extract_usdt_margin_balance(balance)


def extract_wallet_balance(client: Any, balance: dict) -> float:
    """兼容旧名：等同 extract_margin_balance（非 App「钱包余额」）。"""
    return extract_margin_balance(client, balance)


def extract_dashboard_balances(
    client: Any, balance: dict, *, unrealized_pnl: float = 0.0
) -> tuple[float, float]:
    """仪表盘用：(钱包余额, 保证金余额)。

    币安：totalWalletBalance / totalMarginBalance。
    Gate：info.total / (total+unrealised_pnl 或 cross_margin_balance)。
    """
    if getattr(client, "exchange_id", None) == "gate":
        from .gate_service import (
            extract_gate_usdt_margin_balance,
            extract_gate_usdt_unrealized_pnl,
            extract_gate_usdt_wallet_balance,
        )

        wallet = extract_gate_usdt_wallet_balance(balance)
        margin = extract_gate_usdt_margin_balance(balance)
        gate_upnl = extract_gate_usdt_unrealized_pnl(balance)
        # info 缺 upnl 时用持仓汇总浮盈亏兜底推导保证金
        if abs(gate_upnl) < 1e-12 and abs(float(unrealized_pnl or 0.0)) > 1e-12:
            margin = wallet + float(unrealized_pnl or 0.0)
        if wallet <= 0 and margin > 0:
            wallet = margin - float(unrealized_pnl or gate_upnl or 0.0)
            if wallet <= 0:
                wallet = margin
        if margin <= 0 and wallet > 0:
            margin = wallet + float(unrealized_pnl or gate_upnl or 0.0)
        return float(wallet), float(margin)

    from .binance_service import (
        extract_usdt_margin_balance,
        extract_usdt_pure_wallet_balance,
    )

    wallet = extract_usdt_pure_wallet_balance(balance)
    margin = extract_usdt_margin_balance(balance)
    if wallet <= 0 and margin > 0:
        wallet = margin - float(unrealized_pnl or 0.0)
        if wallet <= 0:
            wallet = margin
    if margin <= 0 and wallet > 0:
        margin = wallet + float(unrealized_pnl or 0.0)
    return float(wallet), float(margin)


async def clear_private_exchange_for_account(account: Any) -> None:
    from .encryption import decrypt

    ex = account_exchange_id(account)
    try:
        api_key = decrypt(account.api_key_encrypted)
        api_secret = decrypt(account.api_secret_encrypted)
    except Exception:
        return
    if ex == "gate":
        await clear_private_gate_service(
            api_key,
            api_secret,
            hedge_mode=bool(getattr(account, "hedge_mode", True)),
        )
    else:
        await clear_private_binance_service(
            api_key,
            api_secret,
            bool(getattr(account, "testnet", False)),
            bool(getattr(account, "hedge_mode", True)),
        )


# Re-export concrete types for typing convenience
__all__ = [
    "ExchangeClient",
    "BinanceService",
    "GateService",
    "normalize_exchange_id",
    "account_exchange_id",
    "get_public_exchange",
    "get_exchange_for_account",
    "clear_private_exchange_for_account",
]
