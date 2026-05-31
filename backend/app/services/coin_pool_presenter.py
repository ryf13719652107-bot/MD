"""选币池 API 响应组装（附加资金费率等展示字段）。"""

from ..schemas.coin_pool import CoinPoolResponse
from .binance_service import (
    BinanceService,
    get_cached_last_funding_rates_pct,
    _normalize_symbol_for_tradefi,
)


async def coin_pool_responses_with_funding(
    binance: BinanceService,
    coins: list,
) -> list[CoinPoolResponse]:
    rates = await get_cached_last_funding_rates_pct(binance)
    out: list[CoinPoolResponse] = []
    for c in coins:
        resp = CoinPoolResponse.model_validate(c)
        key = _normalize_symbol_for_tradefi(c.symbol)
        out.append(resp.model_copy(update={"funding_rate_pct": rates.get(key)}))
    return out
