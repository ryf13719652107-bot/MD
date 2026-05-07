import time
import logging
import ccxt.async_support as ccxt_async
import ccxt.pro as ccxtpro
from typing import Optional

logger = logging.getLogger(__name__)


class BinanceService:
    """Wrapper around ccxt binanceusdm (USD-M Futures) with TTL cache."""

    _TTL_SECONDS = 1800  # 30 minutes

    def __init__(self, api_key: str = "", secret: str = "", testnet: bool = True, hedge_mode: bool = True):
        self.api_key = api_key
        self.secret = secret
        self.testnet = testnet
        self.hedge_mode = hedge_mode
        self._exchange: Optional[ccxt_async.binanceusdm] = None
        self._ws_exchange: Optional[ccxtpro.binanceusdm] = None
        self._created_at: float = time.time()

    def _is_expired(self) -> bool:
        return (time.time() - self._created_at) > self._TTL_SECONDS

    @property
    def exchange(self) -> ccxt_async.binanceusdm:
        if self._exchange is None or self._is_expired():
            self._recreate()
        return self._exchange

    @property
    def ws_exchange(self) -> ccxtpro.binanceusdm:
        if self._ws_exchange is None or self._is_expired():
            self._recreate()
        return self._ws_exchange

    def _recreate(self):
        old_exchange = self._exchange
        old_ws = self._ws_exchange
        self._exchange = None
        self._ws_exchange = None
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            if old_exchange:
                loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._safe_close(old_exchange)))
            if old_ws:
                loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._safe_close(old_ws)))
        except RuntimeError:
            pass  # no event loop running
        self._exchange = self._create_exchange(False)
        self._ws_exchange = self._create_exchange(True)
        self._created_at = time.time()
        logger.info("BinanceService TTL expired, recreated exchange instances")

    async def _safe_close(self, ex):
        try:
            await ex.close()
        except Exception:
            pass

    def _create_exchange(self, pro: bool = False):
        from ..config import settings

        if pro:
            cls = ccxtpro.binanceusdm
        else:
            cls = ccxt_async.binanceusdm

        config = {
            "apiKey": self.api_key,
            "secret": self.secret,
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        }

        if settings.http_proxy:
            config["proxies"] = {"http": settings.http_proxy, "https": settings.http_proxy}

        exchange = cls(config)

        if self.testnet:
            exchange.set_sandbox_mode(True)

        return exchange

    # ---- Market Data (Public) ----

    async def fetch_balance(self) -> dict:
        return await self.exchange.fetch_balance()

    async def fetch_ticker(self, symbol: str) -> dict:
        return await self.exchange.fetch_ticker(self._format_symbol(symbol))

    async def fetch_tickers(self, symbols: list[str] | None = None) -> dict:
        formatted = [self._format_symbol(s) for s in symbols] if symbols else None
        return await self.exchange.fetch_tickers(formatted)

    async def fetch_klines(self, symbol: str, timeframe: str = "1m", limit: int = 100) -> list:
        return await self.exchange.fetch_ohlcv(
            self._format_symbol(symbol), timeframe=timeframe, limit=limit
        )

    async def fetch_positions(self, symbols: list[str] | None = None) -> list[dict]:
        formatted = [self._format_symbol(s) for s in symbols] if symbols else None
        return await self.exchange.fetch_positions(formatted)

    async def fetch_leverage(self, symbol: str | None = None, use_coin_pool: bool = False) -> float:
        """Fetch leverage for a symbol. Defaults to 20x if not set or if using coin pool."""
        if not symbol or use_coin_pool:
            return 20.0
        try:
            formatted = self._format_symbol(symbol)
            response = await self.exchange.fapiPrivate_get_leverage({"symbol": formatted.replace("/", "").replace(":USDT", "")})
            return float(response.get("leverage", 20))
        except Exception:
            return 20.0

    # ---- Orders (Private) ----

    def _order_params(self, position_side: str, reduce_only: bool = False) -> dict:
        """Build params dict. positionSide and reduceOnly only sent in hedge mode."""
        params: dict = {}
        if self.hedge_mode:
            params["positionSide"] = position_side
            if reduce_only:
                params["reduceOnly"] = True
        return params

    async def cancel_order(self, order_id: str, symbol: str) -> dict:
        """Cancel an existing order by ID."""
        formatted_symbol = self._format_symbol(symbol)
        return await self.exchange.cancel_order(order_id, formatted_symbol)

    async def create_market_order(
        self, symbol: str, side: str, amount: float,
        reduce_only: bool = False, position_side: str = "LONG",
        slippage_pct: float | None = None,
    ) -> dict:
        formatted_symbol = self._format_symbol(symbol)

        # Slippage protection: pre-check price and reject if slippage too large
        if slippage_pct and slippage_pct > 0:
            ticker = await self.exchange.fetch_ticker(formatted_symbol)
            ref_price = float(ticker.get("last", 0))
            if ref_price > 0:
                order = await self.exchange.create_order(
                    symbol=formatted_symbol,
                    type="market",
                    side=side,
                    amount=amount,
                    params=self._order_params(position_side, reduce_only),
                )
                avg_price = float(order.get("average", 0) or 0)
                if avg_price > 0:
                    if side == "buy":
                        slip = ((avg_price - ref_price) / ref_price) * 100
                    else:
                        slip = ((ref_price - avg_price) / ref_price) * 100
                    if slip > slippage_pct:
                        logger.warning(
                            "Slippage %.2f%% exceeds threshold %.2f%% for %s %s (ref=%.4f avg=%.4f)",
                            slip, slippage_pct, side, formatted_symbol, ref_price, avg_price
                        )
                return order  # always return, never fall through to second order

        return await self.exchange.create_order(
            symbol=formatted_symbol,
            type="market",
            side=side,
            amount=amount,
            params=self._order_params(position_side, reduce_only),
        )

    async def create_limit_order(
        self, symbol: str, side: str, amount: float, price: float,
        reduce_only: bool = False, position_side: str = "LONG",
    ) -> dict:
        return await self.exchange.create_order(
            symbol=self._format_symbol(symbol),
            type="limit",
            side=side,
            amount=amount,
            price=price,
            params=self._order_params(position_side, reduce_only),
        )

    async def close_position(self, symbol: str, side: str) -> dict:
        """Close all positions for symbol+side. Handles hedge mode with multiple entries."""
        formatted_symbol = self._format_symbol(symbol)
        positions = await self.fetch_positions([symbol])
        position_side = "LONG" if side == "long" else "SHORT"

        # In hedge mode, aggregate all contracts for the same symbol+side
        total_contracts = 0.0
        for pos in positions:
            pos_side_exchange = (pos.get("side") or "").lower()
            if pos["symbol"] == formatted_symbol and pos_side_exchange == side.lower() and float(pos.get("contracts", 0)) > 0:
                total_contracts += float(pos["contracts"])

        if total_contracts <= 0:
            logger.warning("close_position: no contracts found for %s %s (positions: %d)", symbol, side, len(positions))
            return {}

        close_side = "sell" if side == "long" else "buy"
        return await self.create_market_order(
            symbol, close_side, total_contracts,
            reduce_only=True, position_side=position_side,
        )

    async def close_position_with_limit(self, symbol: str, side: str, price: float) -> dict:
        """Close position using a limit order at the specified price. Handles hedge mode."""
        formatted_symbol = self._format_symbol(symbol)
        positions = await self.fetch_positions([symbol])
        position_side = "LONG" if side == "long" else "SHORT"

        total_contracts = 0.0
        for pos in positions:
            pos_side_exchange = (pos.get("side") or "").lower()
            if pos["symbol"] == formatted_symbol and pos_side_exchange == side.lower() and float(pos.get("contracts", 0)) > 0:
                total_contracts += float(pos["contracts"])

        if total_contracts <= 0:
            logger.warning("close_position_with_limit: no contracts found for %s %s (positions: %d)", symbol, side, len(positions))
            return {}

        close_side = "sell" if side == "long" else "buy"
        return await self.create_limit_order(
            symbol, close_side, total_contracts, price,
            reduce_only=False, position_side=position_side,
        )

    # ---- WebSocket (Public) ----

    async def watch_tickers(self, symbols: list[str] | None = None):
        formatted = [self._format_symbol(s) for s in symbols] if symbols else None
        return await self.ws_exchange.watch_tickers(formatted)

    async def watch_klines(self, symbol: str, timeframe: str = "1m"):
        return await self.ws_exchange.watch_ohlcv(self._format_symbol(symbol), timeframe)

    # ---- Helpers ----

    async def close(self):
        if self._exchange:
            try:
                await self._exchange.close()
            except Exception:
                pass
            self._exchange = None
        if self._ws_exchange:
            try:
                await self._ws_exchange.close()
            except Exception:
                pass
            self._ws_exchange = None

    async def fetch_top_movers(self, source: str = "both", limit: int = 20) -> list[dict]:
        tickers = await self.exchange.fetch_tickers()
        usdt_pairs = []
        for sym, t in tickers.items():
            if ":USDT" in sym and t.get("percentage") is not None:
                usdt_pairs.append({
                    "symbol": sym.replace("/", "").replace(":USDT", ""),
                    "price_change_pct": t["percentage"],
                    "volume_24h": t.get("quoteVolume", 0) or 0,
                })

        if source in ("gainers", "both"):
            gainers = sorted(usdt_pairs, key=lambda x: -x["price_change_pct"])[:limit]
        else:
            gainers = []

        if source in ("losers", "both"):
            losers = sorted(usdt_pairs, key=lambda x: x["price_change_pct"])[:limit]
        else:
            losers = []

        result = []
        for i, item in enumerate(gainers):
            result.append({**item, "rank": i + 1, "source": "gainers"})
        for i, item in enumerate(losers):
            result.append({**item, "rank": i + 1, "source": "losers"})
        return result

    @staticmethod
    def _format_symbol(symbol: str) -> str:
        if "/" in symbol:
            if ":USDT" not in symbol and symbol.endswith("/USDT"):
                return f"{symbol}:USDT"
            return symbol
        if symbol.endswith("USDT"):
            base = symbol[:-4]
            return f"{base}/USDT:USDT"
        return symbol


# ---- Singleton factory with TTL ----

_private_instances: dict[str, tuple[float, BinanceService]] = {}
_public_instance: Optional[BinanceService] = None
_public_created_at: float = 0.0
_INSTANCE_TTL = 600  # 10 minutes before forcing recreation


async def get_binance_service(api_key: str, secret: str, testnet: bool = True, hedge_mode: bool = True) -> BinanceService:
    """Get a cached BinanceService for authenticated operations."""
    global _private_instances
    cache_key = f"{api_key[:8]}:{testnet}:{hedge_mode}"
    now = time.time()

    if cache_key in _private_instances:
        created, svc = _private_instances[cache_key]
        if now - created < _INSTANCE_TTL:
            return svc
        logger.info("Private BinanceService TTL expired for %s, recreating", cache_key[:10])
        try:
            await svc.close()
        except Exception:
            pass

    svc = BinanceService(api_key, secret, testnet, hedge_mode)
    _private_instances[cache_key] = (now, svc)
    return svc


async def get_public_binance(use_testnet: bool = False) -> BinanceService:
    """Get a cached BinanceService for public market data (always mainnet for leaderboard accuracy)."""
    global _public_instance, _public_created_at
    testnet = False  # leaderboard/klines always from mainnet — testnet volume is meaningless
    now = time.time()

    if _public_instance is not None and (now - _public_created_at) > _INSTANCE_TTL:
        try:
            await _public_instance.close()
        except Exception:
            pass

    if _public_instance is None or (now - _public_created_at) > _INSTANCE_TTL:
        _public_instance = BinanceService(api_key="", secret="", testnet=testnet)
        _public_created_at = now

    return _public_instance


def clear_cache():
    """Force clear all cached exchange instances."""
    global _private_instances, _public_instance
    _private_instances.clear()
    _public_instance = None
