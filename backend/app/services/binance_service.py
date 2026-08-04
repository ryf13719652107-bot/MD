import math
import time
import logging
import hashlib
import ccxt.async_support as ccxt_async
import ccxt.pro as ccxtpro
from typing import Optional

logger = logging.getLogger(__name__)

# 按 exchange_id 分片缓存，避免多交易所互相覆盖
_TRADEFI_SYMBOLS_CACHE: dict[str, tuple[float, frozenset[str]]] = {}
_DELISTING_SOON_CACHE: dict[str, tuple[float, frozenset[str]]] = {}
_LAST_FUNDING_RATES_CACHE: dict[str, tuple[float, dict[str, float]]] = {}
_TRADEFI_CACHE_TTL = 3600.0
_FUNDING_CACHE_TTL = 300.0
DELIST_LOOKAHEAD_DAYS = 14
DELIST_LOOKAHEAD_MS = DELIST_LOOKAHEAD_DAYS * 24 * 3600 * 1000


def tradefi_cache_clear():
    _TRADEFI_SYMBOLS_CACHE.clear()
    _DELISTING_SOON_CACHE.clear()
    _LAST_FUNDING_RATES_CACHE.clear()


def _client_exchange_id(client) -> str:
    return getattr(client, "exchange_id", None) or "binance"


def _float_or_zero(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def extract_usdt_pure_wallet_balance(balance: dict) -> float:
    """币安 App「钱包余额」= totalWalletBalance（不含未实现盈亏）。"""
    if not isinstance(balance, dict):
        return 0.0
    info = balance.get("info")
    if isinstance(info, dict):
        v = _float_or_zero(info.get("totalWalletBalance"))
        if v > 0:
            return v
    for section in ("total",):
        data = balance.get(section) or {}
        if isinstance(data, dict):
            v = _float_or_zero(data.get("USDT"))
            if v > 0:
                return v
    usdt_row = balance.get("USDT") or {}
    if isinstance(usdt_row, dict):
        v = _float_or_zero(usdt_row.get("total"))
        if v > 0:
            return v
    return 0.0


def extract_usdt_margin_balance(balance: dict) -> float:
    """币安 App「保证金余额」= totalMarginBalance（钱包 + 未实现盈亏）。

    曲线快照 / 保证金止损 / 单币止损均以此为准（非 totalWalletBalance）。
    """
    if not isinstance(balance, dict):
        return 0.0
    info = balance.get("info")
    if isinstance(info, dict):
        raw = info.get("totalMarginBalance")
        if raw is not None and raw != "":
            return _float_or_zero(raw)
        # 缺字段时用 钱包 + 未实现 推导（与 App 保证金余额一致）
        wallet = _float_or_zero(info.get("totalWalletBalance"))
        upnl = _float_or_zero(info.get("totalUnrealizedProfit"))
        if wallet > 0 or abs(upnl) > 1e-12:
            derived = wallet + upnl
            if derived > 0 or abs(upnl) > 1e-12:
                return derived
    return extract_usdt_pure_wallet_balance(balance)


def extract_usdt_wallet_balance(balance: dict) -> float:
    """策略/风控用账户权益：= 保证金余额（随浮盈亏变动）。"""
    v = extract_usdt_margin_balance(balance)
    if v > 0:
        return v
    if not isinstance(balance, dict):
        return 0.0
    for section in ("total", "free"):
        data = balance.get(section) or {}
        if isinstance(data, dict):
            x = _float_or_zero(data.get("USDT"))
            if x > 0:
                return x
    info = balance.get("info")
    if isinstance(info, dict):
        x = _float_or_zero(info.get("availableBalance"))
        if x > 0:
            return x
    return 0.0


def _normalize_symbol_for_tradefi(s: str) -> str:
    return (s or "").replace("/", "").replace(":USDT", "").replace("_", "").upper()


# 币安 USDT-M 上的黄金/白银/原油等（含 TRADIFI 与部分永续命名）
EXCLUDED_COMMODITY_SYMBOLS: frozenset[str] = frozenset({
    "XAUUSDT",
    "XAGUSDT",
    "XPTUSDT",
    "XPDUSDT",
    "USOILUSDT",
    "UKOILUSDT",
    "OILUSDT",
    "BRENTUSDT",
    "WTIUSDT",
    "CRUDEUSDT",
    "NATGASUSDT",
    "COPPERUSDT",
})

# 主流加密货币（选币池模式排除；含 MATIC/POL 两种 ticker）
EXCLUDED_MAINSTREAM_SYMBOLS: frozenset[str] = frozenset({
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "DOTUSDT",
    "MATICUSDT",
    "POLUSDT",
    "LTCUSDT",
    "TRXUSDT",
    "SHIBUSDT",
    "BCHUSDT",
    "UNIUSDT",
    "ATOMUSDT",
    "ETCUSDT",
    "FILUSDT",
    "NEARUSDT",
})

# 前缀匹配（base = 去掉 USDT 后），勿用 "GAS" 以免误伤加密货币 GASUSDT
_NON_CRYPTO_BASE_PREFIXES: tuple[str, ...] = (
    "XAU",
    "XAG",
    "XPT",
    "XPD",
    "USOIL",
    "UKOIL",
    "BRENT",
    "WTI",
    "CRUDE",
    "NATGAS",
    "COPPER",
    "SILVER",
    "GOLD",
)


def is_non_crypto_commodity_symbol(symbol: str) -> bool:
    """黄金/白银/原油等非加密货币合约（静态表 + 前缀）。"""
    s = _normalize_symbol_for_tradefi(symbol)
    if s in EXCLUDED_COMMODITY_SYMBOLS:
        return True
    base = s[:-4] if s.endswith("USDT") and len(s) > 4 else s
    return any(base.startswith(p) for p in _NON_CRYPTO_BASE_PREFIXES)


def is_tradefi_or_commodity_symbol(symbol: str, tradefi_norm: frozenset[str]) -> bool:
    s = _normalize_symbol_for_tradefi(symbol)
    return s in tradefi_norm or is_non_crypto_commodity_symbol(s)


def _symbol_delisting_soon(info: dict, now_ms: int) -> bool:
    """非 TRADING 或 deliveryDate 在 14 天内视为将下架。"""
    status = (info.get("status") or "").upper()
    if status != "TRADING":
        return True
    dd = info.get("deliveryDate")
    if dd is None:
        return False
    try:
        dms = int(dd)
    except (TypeError, ValueError):
        return False
    # 0/缺失常表示永续无固定交割；勿当成已过期
    if dms <= 0:
        return False
    if dms <= now_ms:
        return True
    return (dms - now_ms) <= DELIST_LOOKAHEAD_MS


async def get_cached_delisting_soon_symbols(client) -> frozenset[str]:
    """14 天内下架或非 TRADING 的 USDT 永续（normalized），缓存 1h。GATE 返回空集。"""
    eid = _client_exchange_id(client)
    if eid == "gate":
        return frozenset()
    now = time.time()
    hit = _DELISTING_SOON_CACHE.get(eid)
    if hit is not None and now - hit[0] < _TRADEFI_CACHE_TTL:
        return hit[1]
    raw = await client.fetch_delisting_soon_symbols_raw()
    norm = frozenset(_normalize_symbol_for_tradefi(s) for s in raw)
    _DELISTING_SOON_CACHE[eid] = (now, norm)
    logger.info("Delisting-soon symbols [%s] (<%dd): %d", eid, DELIST_LOOKAHEAD_DAYS, len(norm))
    return norm


async def get_strategy_pool_exclude_symbols(
    client,
    *,
    exclude_tradefi: bool = False,
    exclude_delisting: bool = True,
    exclude_mainstream: bool = False,
) -> frozenset[str] | None:
    """策略选币/开仓用排除集合（normalized）。"""
    eid = _client_exchange_id(client)
    merged: set[str] = set()
    if exclude_tradefi:
        merged |= set(await get_cached_tradefi_symbols(client))
    # GATE 首期不做下架过滤
    if exclude_delisting and eid != "gate":
        merged |= set(await get_cached_delisting_soon_symbols(client))
    if exclude_mainstream:
        merged |= set(EXCLUDED_MAINSTREAM_SYMBOLS)
    return frozenset(merged) if merged else None


async def get_cached_last_funding_rates_pct(client) -> dict[str, float]:
    """最近一次已结算资金费率(%，normalized symbol → pct)，缓存 5min。"""
    eid = _client_exchange_id(client)
    now = time.time()
    hit = _LAST_FUNDING_RATES_CACHE.get(eid)
    if hit is not None and now - hit[0] < _FUNDING_CACHE_TTL:
        return hit[1]
    raw = await client.fetch_last_funding_rates_pct_raw()
    _LAST_FUNDING_RATES_CACHE[eid] = (now, raw)
    logger.info("Last funding rates cached [%s]: %d symbols", eid, len(raw))
    return raw


async def filter_pool_symbols_by_funding(
    client,
    symbols: list[str],
    *,
    direction: str,
    threshold_pct: float,
) -> list[str]:
    from .strategy_flags import funding_rate_blocks_new_entry

    if not symbols:
        return symbols
    rates = await get_cached_last_funding_rates_pct(client)
    out: list[str] = []
    for sym in symbols:
        key = _normalize_symbol_for_tradefi(sym)
        rate = rates.get(key, 0.0)
        if not funding_rate_blocks_new_entry(direction, rate, threshold_pct):
            out.append(sym)
    return out


async def get_cached_tradefi_symbols(client) -> frozenset[str]:
    """TradFi + 黄金白银原油等；normalized，按交易所缓存 1h。"""
    eid = _client_exchange_id(client)
    now = time.time()
    hit = _TRADEFI_SYMBOLS_CACHE.get(eid)
    if hit is not None and now - hit[0] < _TRADEFI_CACHE_TTL:
        return hit[1]
    raw = await client.fetch_tradefi_perpetual_symbols_raw()
    norm = frozenset(_normalize_symbol_for_tradefi(s) for s in raw)
    norm = norm | EXCLUDED_COMMODITY_SYMBOLS
    _TRADEFI_SYMBOLS_CACHE[eid] = (now, norm)
    return norm


class BinanceService:
    """Wrapper around ccxt binanceusdm (USD-M Futures) with TTL cache."""

    exchange_id = "binance"
    _TTL_SECONDS = 7200  # 2 hours; avoid frequent market/leverage/K-line cold starts

    def __init__(self, api_key: str = "", secret: str = "", testnet: bool = True, hedge_mode: bool = True):
        self.api_key = api_key
        self.secret = secret
        self.testnet = testnet
        self.hedge_mode = hedge_mode
        self._exchange: Optional[ccxt_async.binanceusdm] = None
        self._ws_exchange: Optional[ccxtpro.binanceusdm] = None
        self._created_at: float = time.time()
        self._pinned: bool = False
        self._markets_loaded: bool = False
        self._leverage_cache: dict[str, int] = {}

    def _is_expired(self) -> bool:
        if self._pinned:
            return False
        return (time.time() - self._created_at) > self._TTL_SECONDS

    def pin(self):
        """Pin the exchange instance so TTL won't expire during long operations."""
        self._pinned = True

    def unpin(self):
        """Unpin and refresh creation timestamp so next access won't immediately expire."""
        self._pinned = False
        self._created_at = time.time()

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
        self._markets_loaded = False
        self._leverage_cache.clear()
        logger.info("BinanceService TTL expired, recreated exchange instances")

    def begin_tick(self) -> None:
        """Per-tick hook; leverage cache is kept so pool/strategy prewarm hits cache at open."""
        pass

    async def ensure_markets_loaded(self) -> None:
        if not self._markets_loaded:
            await self.exchange.load_markets()
            self._markets_loaded = True

    async def fetch_exchange_info_symbols(self) -> list[dict]:
        try:
            r = await self.exchange.fapiPublicGetExchangeInfo()
        except Exception as e:
            logger.warning("fapiPublicGetExchangeInfo failed: %s", e)
            return []
        return list(r.get("symbols") or [])

    async def fetch_tradefi_perpetual_symbols_raw(self) -> set[str]:
        """USDM symbols (BTCUSDT-style) that are TRADIFI_PERPETUAL and TRADING."""
        out: set[str] = set()
        for x in await self.fetch_exchange_info_symbols():
            if x.get("status") == "TRADING" and x.get("contractType") == "TRADIFI_PERPETUAL":
                sym = x.get("symbol")
                if sym:
                    out.add(sym)
        return out

    async def fetch_delisting_soon_symbols_raw(self) -> set[str]:
        """USDT 本位合约中 14 天内下架或非 TRADING 的 symbol（BTCUSDT 格式）。"""
        now_ms = int(time.time() * 1000)
        out: set[str] = set()
        for x in await self.fetch_exchange_info_symbols():
            if x.get("quoteAsset") != "USDT":
                continue
            sym = x.get("symbol")
            if sym and _symbol_delisting_soon(x, now_ms):
                out.add(sym)
        return out

    async def fetch_last_funding_rates_pct_raw(self) -> dict[str, float]:
        """premiumIndex lastFundingRate → normalized symbol → 最近结算费率(%)."""
        try:
            rows = await self.exchange.fapiPublicGetPremiumIndex()
        except Exception as e:
            logger.warning("fapiPublicGetPremiumIndex failed: %s", e)
            return {}
        if isinstance(rows, dict):
            rows = [rows]
        out: dict[str, float] = {}
        for row in rows or []:
            sym = row.get("symbol")
            if not sym:
                continue
            try:
                rate_pct = float(row.get("lastFundingRate") or 0) * 100.0
            except (TypeError, ValueError):
                rate_pct = 0.0
            out[_normalize_symbol_for_tradefi(sym)] = rate_pct
        return out

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

    async def fetch_income_history(
        self,
        *,
        income_type: str | None = None,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        """GET /fapi/v1/income — 合约账户流水（划转/盈亏/资金费等）。"""
        params: dict = {"limit": min(int(limit), 1000)}
        if income_type:
            params["incomeType"] = income_type
        if start_time_ms is not None:
            params["startTime"] = int(start_time_ms)
        if end_time_ms is not None:
            params["endTime"] = int(end_time_ms)
        raw = await self.exchange.fapiPrivateGetIncome(params)
        return list(raw) if isinstance(raw, list) else []

    async def fetch_ticker(self, symbol: str) -> dict:
        return await self.exchange.fetch_ticker(self._format_symbol(symbol))

    async def fetch_tickers(self, symbols: list[str] | None = None) -> dict:
        formatted = [self._format_symbol(s) for s in symbols] if symbols else None
        return await self.exchange.fetch_tickers(formatted)

    async def fetch_klines(
        self,
        symbol: str,
        timeframe: str = "1m",
        limit: int = 100,
        since: int | None = None,
    ) -> list:
        kwargs: dict = {"timeframe": timeframe, "limit": limit}
        if since is not None:
            kwargs["since"] = since
        return await self.exchange.fetch_ohlcv(self._format_symbol(symbol), **kwargs)

    async def fetch_positions(self, symbols: list[str] | None = None) -> list[dict]:
        if not symbols:
            return await self.exchange.fetch_positions(None)
        formatted = [self._format_symbol(s) for s in symbols]
        try:
            return await self.exchange.fetch_positions(formatted)
        except Exception as e:
            msg = str(e).lower()
            if "does not have market symbol" in msg or "marketsymbol" in msg or "invalid symbol" in msg:
                # 新上币/本地 markets 缓存旧：重载后再试；仍失败则全量拉仓位再按代码过滤
                try:
                    await self.exchange.load_markets(True)
                    return await self.exchange.fetch_positions(formatted)
                except Exception as e2:
                    logger.debug(
                        "fetch_positions(%s) after load_markets still failed: %s",
                        symbols,
                        e2,
                    )
                logger.warning(
                    "fetch_positions(%s) unified symbol missing in ccxt, fallback fetch all: %s",
                    symbols,
                    e,
                )
                raw = await self.exchange.fetch_positions(None)
                want = {_normalize_symbol_for_tradefi(s) for s in symbols}
                out: list[dict] = []
                for p in raw:
                    sym = p.get("symbol") or ""
                    if _normalize_symbol_for_tradefi(sym) in want:
                        out.append(p)
                return out
            raise

    def _binance_futures_symbol_id(self, symbol: str) -> str:
        formatted = self._format_symbol(symbol)
        return formatted.replace("/", "").replace(":USDT", "")

    @staticmethod
    def _leverage_already_set_error(exc: Exception) -> bool:
        """Binance -4028 when target leverage already active — safe to proceed."""
        msg = str(exc).lower()
        return "already exist" in msg or ("-4028" in msg and "already" in msg)

    async def fetch_leverage(self, symbol: str) -> float | None:
        """Query exchange leverage for a symbol; None if unavailable."""
        try:
            bin_sym = self._binance_futures_symbol_id(symbol)
            response = await self.exchange.fapiPrivate_get_leverage({"symbol": bin_sym})
            return float(response.get("leverage", 0) or 0)
        except Exception:
            return None

    def is_leverage_cached(self, symbol: str, leverage: int) -> bool:
        """True if in-memory cache already has the target leverage (no REST)."""
        lev = max(1, min(125, int(leverage)))
        bin_sym = self._binance_futures_symbol_id(symbol)
        return self._leverage_cache.get(bin_sym) == lev

    async def set_symbol_leverage(self, symbol: str, leverage: int) -> tuple[int, bool]:
        """
        Set USDT-M contract leverage on Binance for symbol (POST /fapi/v1/leverage).
        Returns (applied_leverage, cache_hit). Raises on API failure.
        """
        lev = max(1, min(125, int(leverage)))
        formatted = self._format_symbol(symbol)
        bin_sym = self._binance_futures_symbol_id(symbol)
        cached = self._leverage_cache.get(bin_sym)
        if cached == lev:
            return lev, True
        try:
            await self.exchange.set_leverage(lev, formatted)
            self._leverage_cache[bin_sym] = lev
            return lev, False
        except Exception as e1:
            if self._leverage_already_set_error(e1):
                logger.debug("leverage already %sx for %s", lev, bin_sym)
                self._leverage_cache[bin_sym] = lev
                return lev, False
            logger.debug("set_leverage ccxt path failed for %s: %s", bin_sym, e1)
            try:
                await self.exchange.fapiPrivatePostLeverage({"symbol": bin_sym, "leverage": lev})
                self._leverage_cache[bin_sym] = lev
                return lev, False
            except Exception as e2:
                if self._leverage_already_set_error(e2):
                    self._leverage_cache[bin_sym] = lev
                    return lev, False
                raise RuntimeError(f"设置杠杆失败 {bin_sym} {lev}x: {e2}") from e2

    # ---- Orders (Private) ----

    def _order_params(self, position_side: str, reduce_only: bool = False) -> dict:
        """Build params dict. positionSide and reduceOnly only sent in hedge mode."""
        params: dict = {}
        if self.hedge_mode:
            params["positionSide"] = position_side
            if reduce_only:
                params["reduceOnly"] = True
        return params

    def _futures_market_max_order_qty(self, formatted_symbol: str) -> float | None:
        """Binance USDM MARKET_LOT_SIZE.maxQty (fallback LOT_SIZE), or None if unknown."""
        try:
            market = self.exchange.market(formatted_symbol)
        except Exception:
            return None
        filters = (market.get("info") or {}).get("filters") or []
        for f in filters:
            if f.get("filterType") == "MARKET_LOT_SIZE":
                mx = f.get("maxQty")
                if mx is not None and float(mx) > 0:
                    return float(mx)
        for f in filters:
            if f.get("filterType") == "LOT_SIZE":
                mx = f.get("maxQty")
                if mx is not None and float(mx) > 0:
                    return float(mx)
        return None

    def _lot_step_and_min(self, formatted_symbol: str) -> tuple[float, float]:
        step = 0.0
        min_q = 0.0
        try:
            market = self.exchange.market(formatted_symbol)
            for f in (market.get("info") or {}).get("filters") or []:
                if f.get("filterType") == "LOT_SIZE":
                    step = float(f.get("stepSize", 0) or 0)
                    min_q = float(f.get("minQty", 0) or 0)
                    break
        except Exception:
            pass
        return step, min_q

    async def _create_market_reduce_chunked(
        self,
        symbol: str,
        close_side: str,
        total_contracts: float,
        position_side: str,
    ) -> dict:
        """Close a hedge leg with multiple MARKET reduce orders when size exceeds exchange max."""
        formatted = self._format_symbol(symbol)
        await self.exchange.load_markets()
        max_mkt = self._futures_market_max_order_qty(formatted)
        step, min_q = self._lot_step_and_min(formatted)

        remaining = float(total_contracts)
        total_filled = 0.0
        vwap_num = 0.0
        last_order: dict | None = None
        for _ in range(512):
            if remaining <= 1e-12:
                break
            cap = remaining if max_mkt is None or max_mkt <= 0 else min(remaining, max_mkt)
            if step > 0:
                chunk = math.floor(cap / step + 1e-12) * step
            else:
                try:
                    chunk = float(self.exchange.amount_to_precision(formatted, cap))
                except Exception:
                    chunk = cap
            if chunk <= 0:
                break
            if min_q > 0 and chunk < min_q:
                if remaining + 1e-12 >= min_q:
                    if step > 0:
                        n = max(1, math.ceil(min_q / step - 1e-12))
                        chunk = n * step
                    else:
                        chunk = min_q
                    if max_mkt and max_mkt > 0 and chunk > max_mkt + 1e-12:
                        chunk = math.floor(max_mkt / step + 1e-12) * step if step > 0 else max_mkt
                else:
                    break
            try:
                order = await self.exchange.create_order(
                    formatted,
                    "market",
                    close_side,
                    chunk,
                    None,
                    self._order_params(position_side, reduce_only=True),
                )
            except Exception as e:
                if "-1106" in str(e):
                    order = await self.exchange.create_order(
                        formatted,
                        "market",
                        close_side,
                        chunk,
                        None,
                        self._order_params(position_side, reduce_only=False),
                    )
                else:
                    raise
            last_order = order
            filled = float(order.get("filled") or 0)
            if filled <= 0:
                filled = float(order.get("amount") or chunk)
            avg = float(order.get("average") or order.get("price") or 0)
            if filled > 0 and avg > 0:
                vwap_num += avg * filled
            total_filled += filled
            remaining -= filled

        if last_order is None:
            return {}
        out_avg = (vwap_num / total_filled) if total_filled > 0 else float(last_order.get("average") or 0)
        merged = dict(last_order)
        merged["average"] = out_avg
        merged["filled"] = total_filled
        return merged

    async def cancel_order(self, order_id: str, symbol: str) -> dict:
        """Cancel an existing order by ID."""
        formatted_symbol = self._format_symbol(symbol)
        return await self.exchange.cancel_order(order_id, formatted_symbol)

    async def estimate_min_open_notional(self, symbol: str, price: float) -> float | None:
        """币安最小开仓名义(USDT)：取 cost.min 与 amount.min×price 的较大值。"""
        if price is None or float(price) <= 0:
            return None
        await self.ensure_markets_loaded()
        formatted = self._format_symbol(symbol)
        try:
            market = self.exchange.market(formatted)
        except Exception:
            return None
        candidates: list[float] = []
        limits = market.get("limits") if isinstance(market.get("limits"), dict) else {}
        cost_lim = limits.get("cost") if isinstance(limits.get("cost"), dict) else {}
        amount_lim = limits.get("amount") if isinstance(limits.get("amount"), dict) else {}
        for raw in (cost_lim.get("min"),):
            if raw is None or raw == "":
                continue
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            if v > 0:
                candidates.append(v)
        raw_amt = amount_lim.get("min")
        if raw_amt is not None and raw_amt != "":
            try:
                am = float(raw_amt)
            except (TypeError, ValueError):
                am = 0.0
            if am > 0:
                candidates.append(am * float(price))
        if not candidates:
            return None
        return max(candidates)

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
        """Close entire symbol+side leg. Hedge: prefer Binance closePosition; else chunked reduce."""
        formatted_symbol = self._format_symbol(symbol)
        await self.exchange.load_markets()
        position_side = "LONG" if side == "long" else "SHORT"
        close_side = "sell" if side == "long" else "buy"

        if self.hedge_mode:
            params = dict(self._order_params(position_side, reduce_only=True))
            params["closePosition"] = True
            try:
                return await self.exchange.create_order(
                    formatted_symbol, "market", close_side, 0, None, params
                )
            except Exception as e:
                logger.warning(
                    "close_position: closePosition failed for %s %s, falling back to chunked reduce: %s",
                    symbol,
                    side,
                    e,
                )

        positions = await self.fetch_positions([symbol])
        total_contracts = 0.0
        for pos in positions:
            pos_side_exchange = (pos.get("side") or "").lower()
            if pos["symbol"] == formatted_symbol and pos_side_exchange == side.lower() and float(pos.get("contracts", 0)) > 0:
                total_contracts += float(pos["contracts"])

        if total_contracts <= 0:
            logger.warning("close_position: no contracts found for %s %s (positions: %d)", symbol, side, len(positions))
            return {}

        max_mkt = self._futures_market_max_order_qty(formatted_symbol)
        if max_mkt is None or max_mkt <= 0 or total_contracts <= max_mkt:
            try:
                return await self.create_market_order(
                    symbol, close_side, total_contracts,
                    reduce_only=True, position_side=position_side,
                )
            except Exception as e:
                err = str(e)
                if "-1106" in err:
                    return await self.create_market_order(
                        symbol, close_side, total_contracts,
                        reduce_only=False, position_side=position_side,
                    )
                if "-4005" in err:
                    return await self._create_market_reduce_chunked(symbol, close_side, total_contracts, position_side)
                raise
        return await self._create_market_reduce_chunked(symbol, close_side, total_contracts, position_side)

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

    async def watch_trades(self, symbol: str):
        """Public aggTrade / trades stream for millisecond last price."""
        return await self.ws_exchange.watch_trades(self._format_symbol(symbol))

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
_INSTANCE_TTL = 7200  # 2 hours before forcing recreation


def _private_cache_key(api_key: str, secret: str, testnet: bool, hedge_mode: bool) -> str:
    digest = hashlib.sha256(f"{api_key}\0{secret}".encode("utf-8")).hexdigest()
    return f"{digest}:{testnet}:{hedge_mode}"


async def get_binance_service(api_key: str, secret: str, testnet: bool = True, hedge_mode: bool = True) -> BinanceService:
    """Get a cached BinanceService for authenticated operations."""
    global _private_instances
    cache_key = _private_cache_key(api_key, secret, testnet, hedge_mode)
    now = time.time()

    if cache_key in _private_instances:
        created, svc = _private_instances[cache_key]
        if now - created < _INSTANCE_TTL:
            return svc
        logger.info("Private BinanceService TTL expired for %s, recreating", cache_key[:12])
        try:
            await svc.close()
        except Exception:
            pass

    svc = BinanceService(api_key, secret, testnet, hedge_mode)
    _private_instances[cache_key] = (now, svc)
    return svc


async def clear_private_binance_service(
    api_key: str,
    secret: str,
    testnet: bool = True,
    hedge_mode: bool = True,
) -> None:
    """Close and remove one authenticated exchange cache entry."""
    cache_key = _private_cache_key(api_key, secret, testnet, hedge_mode)
    entry = _private_instances.pop(cache_key, None)
    if not entry:
        return
    _, svc = entry
    try:
        await svc.close()
    except Exception:
        pass


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
    tradefi_cache_clear()
