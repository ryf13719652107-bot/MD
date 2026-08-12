"""Gate.io USDT 永续（ccxt gate）封装 — 与 BinanceService 对齐的表面 API。"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Optional

import ccxt.async_support as ccxt_async
import ccxt.pro as ccxtpro

from .binance_service import EXCLUDED_COMMODITY_SYMBOLS

logger = logging.getLogger(__name__)

_TRADEFI_CACHE: dict[str, tuple[float, frozenset[str]]] = {}
_FUNDING_CACHE: dict[str, tuple[float, dict[str, float]]] = {}
_CACHE_TTL_TRADEFI = 3600.0
_CACHE_TTL_FUNDING = 300.0

# Gate contract_type 中视为 TradFi / 非加密的分类
_GATE_TRADEFI_CONTRACT_TYPES = frozenset({
    "stocks",
    "stock",
    "metals",
    "metal",
    "indices",
    "index",
    "forex",
    "fx",
    "commodities",
    "commodity",
    "etf",
    "bond",
    "bonds",
})

# 开仓：换算后币数量 / 意图币数量 超过此倍数则拒单（防 contractSize 错误放大）
_GATE_OPEN_SIZE_MAX_RATIO = 2.5


def gate_cache_clear() -> None:
    _TRADEFI_CACHE.clear()
    _FUNDING_CACHE.clear()


def _float_or_zero(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _gate_account_info_row(balance: dict) -> dict:
    """取出 Gate /futures/{settle}/accounts 的 info 行（dict 或 USDT 列表项）。"""
    if not isinstance(balance, dict):
        return {}
    info = balance.get("info")
    if isinstance(info, dict):
        return info
    if isinstance(info, list):
        for row in info:
            if not isinstance(row, dict):
                continue
            currency = (row.get("currency") or row.get("asset") or "").upper()
            if currency in ("USDT", ""):
                return row
    return {}


def extract_gate_usdt_unrealized_pnl(balance: dict) -> float:
    """Gate 合约未实现盈亏（info.unrealised_pnl / cross_unrealised_pnl）。"""
    info = _gate_account_info_row(balance)
    if not info:
        return 0.0
    for key in (
        "cross_unrealised_pnl",
        "cross_unrealized_pnl",
        "unrealised_pnl",
        "unrealized_pnl",
        "unrealisedPnl",
        "unrealizedPnl",
    ):
        if info.get(key) is not None and info.get(key) != "":
            return _float_or_zero(info.get(key))
    return 0.0


def extract_gate_usdt_wallet_balance(balance: dict) -> float:
    """Gate 合约「钱包余额」≈ info.total（历史净流入，不含未实现盈亏）。

    对应币安 totalWalletBalance；勿与权益/保证金余额混淆。
    """
    if not isinstance(balance, dict):
        return 0.0

    info = _gate_account_info_row(balance)
    if info and info.get("total") is not None and info.get("total") != "":
        return _float_or_zero(info.get("total"))

    # ccxt 统一结构（通常已是 total，不含 upnl）
    for section in ("total", "free"):
        data = balance.get(section) or {}
        if isinstance(data, dict):
            v = _float_or_zero(data.get("USDT"))
            if v > 0:
                return v

    usdt_row = balance.get("USDT") or {}
    if isinstance(usdt_row, dict):
        for key in ("total", "free"):
            v = _float_or_zero(usdt_row.get(key))
            if v > 0:
                return v
    return 0.0


def extract_gate_usdt_margin_balance(balance: dict) -> float:
    """Gate 合约权益（对齐币安「保证金余额」= 钱包 + 未实现盈亏）。

    优先 cross_margin_balance（全仓经典账户）；否则 total + unrealised_pnl。
    用于收益曲线快照 / 保证金阈值 / 单币止损分母。
    """
    if not isinstance(balance, dict):
        return 0.0

    info = _gate_account_info_row(balance)
    if info:
        for key in ("cross_margin_balance", "crossMarginBalance"):
            raw = info.get(key)
            if raw is not None and raw != "":
                return _float_or_zero(raw)
        wallet = extract_gate_usdt_wallet_balance(balance)
        upnl = extract_gate_usdt_unrealized_pnl(balance)
        if wallet > 0 or abs(upnl) > 1e-12:
            return wallet + upnl

    return extract_gate_usdt_wallet_balance(balance)


def normalize_gate_symbol(symbol: str) -> str:
    """统一为 BTCUSDT 形式。"""
    s = (symbol or "").strip().upper()
    s = s.replace("/", "").replace(":USDT", "").replace("_", "")
    return s


class GateService:
    """Wrapper around ccxt gate USDT-settled perpetual swaps."""

    exchange_id = "gate"
    _TTL_SECONDS = 7200

    def __init__(self, api_key: str = "", secret: str = "", hedge_mode: bool = True):
        self.api_key = api_key
        self.secret = secret
        self.testnet = False
        self.hedge_mode = hedge_mode
        self._exchange: Optional[ccxt_async.Exchange] = None
        self._ws_exchange: Optional[ccxtpro.Exchange] = None
        self._created_at: float = time.time()
        self._pinned: bool = False
        self._markets_loaded: bool = False
        self._leverage_cache: dict[str, int] = {}
        self._dual_mode_ensured: bool = False

    def _is_expired(self) -> bool:
        if self._pinned:
            return False
        return (time.time() - self._created_at) > self._TTL_SECONDS

    def pin(self):
        self._pinned = True

    def unpin(self):
        self._pinned = False
        self._created_at = time.time()

    @property
    def exchange(self):
        if self._exchange is None or self._is_expired():
            self._recreate()
        return self._exchange

    @property
    def ws_exchange(self):
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
            pass
        self._exchange = self._create_exchange(False)
        self._ws_exchange = self._create_exchange(True)
        self._created_at = time.time()
        self._markets_loaded = False
        self._leverage_cache.clear()
        self._dual_mode_ensured = False
        logger.info("GateService TTL expired, recreated exchange instances")

    def begin_tick(self) -> None:
        pass

    async def _safe_close(self, ex):
        try:
            await ex.close()
        except Exception:
            pass

    def _create_exchange(self, pro: bool = False):
        from ..config import settings

        cls = ccxtpro.gate if pro else ccxt_async.gate
        config = {
            "apiKey": self.api_key,
            "secret": self.secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap",
                "defaultSettle": "usdt",
                "settle": "usdt",
            },
        }
        if settings.http_proxy:
            config["proxies"] = {"http": settings.http_proxy, "https": settings.http_proxy}
        return cls(config)

    async def ensure_markets_loaded(self) -> None:
        if not self._markets_loaded:
            await self.exchange.load_markets()
            self._markets_loaded = True

    async def ensure_dual_mode(self) -> None:
        """确保账户处于双向持仓；失败必须阻断下单，避免单向账户被反向开仓。"""
        if not self.hedge_mode or self._dual_mode_ensured:
            return
        if not self.api_key:
            return
        try:
            await self.exchange.set_position_mode(True)
            self._dual_mode_ensured = True
        except Exception as e:
            msg = str(e).lower().replace("-", "_").replace(" ", "_")
            # Gate 已是双向时常返回 label=NO_CHANGE；视为成功，勿阻断开仓
            if any(
                k in msg
                for k in (
                    "already",
                    "no_change",
                    "same_mode",
                    "position_mode_is_already",
                    "dual_plus_is_already",
                )
            ):
                self._dual_mode_ensured = True
                logger.debug("Gate dual mode already set: %s", e)
                return
            raise RuntimeError(
                f"Gate 无法切换到双向持仓模式，已阻止下单（请清空仓位/挂单后重试）: {e}"
            ) from e

    # ---- Market meta / filters ----

    async def fetch_futures_contracts_raw(self) -> list[dict]:
        try:
            # Gate private/public futures contracts
            if hasattr(self.exchange, "publicFuturesGetSettleContracts"):
                rows = await self.exchange.publicFuturesGetSettleContracts({"settle": "usdt"})
            elif hasattr(self.exchange, "fetch_markets"):
                await self.ensure_markets_loaded()
                rows = []
                for m in self.exchange.markets.values():
                    if m.get("swap") and (m.get("settle") or "").lower() == "usdt":
                        rows.append(m.get("info") or m)
                return rows
            else:
                return []
            return list(rows) if isinstance(rows, list) else []
        except Exception as e:
            logger.warning("Gate fetch contracts failed: %s", e)
            return []

    async def fetch_tradefi_perpetual_symbols_raw(self) -> set[str]:
        """contract_type 为 stocks/metals/indices/forex/commodities 等的合约 → BTCUSDT 风格。"""
        out: set[str] = set()
        for row in await self.fetch_futures_contracts_raw():
            ctype = (
                row.get("contract_type")
                or row.get("contractType")
                or row.get("type_name")
                or ""
            )
            ctype = str(ctype).strip().lower()
            if ctype and ctype not in _GATE_TRADEFI_CONTRACT_TYPES:
                # 加密永续常见为空或 crypto；仅收录明确 TradFi 分类
                continue
            if not ctype:
                continue
            name = row.get("name") or row.get("symbol") or ""
            if not name:
                continue
            out.add(normalize_gate_symbol(name))
        # 静态商品表兜底
        out |= set(EXCLUDED_COMMODITY_SYMBOLS)
        return out

    async def fetch_delisting_soon_symbols_raw(self) -> set[str]:
        """首期不做 GATE 下架过滤。"""
        return set()

    async def fetch_last_funding_rates_pct_raw(self) -> dict[str, float]:
        """从 tickers / contracts 取资金费率 → normalized → %。"""
        out: dict[str, float] = {}
        try:
            tickers = await self.exchange.fetch_tickers()
            for sym, t in (tickers or {}).items():
                if not self._is_usdt_perp_symbol(sym):
                    continue
                info = t.get("info") or {}
                rate = None
                for key in ("funding_rate", "fundingRate", "lastFundingRate"):
                    if info.get(key) is not None:
                        rate = info.get(key)
                        break
                if rate is None:
                    rate = t.get("fundingRate")
                if rate is None:
                    continue
                try:
                    rate_pct = float(rate) * 100.0
                except (TypeError, ValueError):
                    continue
                out[normalize_gate_symbol(sym)] = rate_pct
        except Exception as e:
            logger.warning("Gate fetch funding from tickers failed: %s", e)

        if out:
            return out

        # fallback: contracts endpoint
        for row in await self.fetch_futures_contracts_raw():
            name = row.get("name") or row.get("symbol")
            if not name:
                continue
            try:
                rate = row.get("funding_rate") or row.get("fundingRate")
                if rate is None:
                    continue
                out[normalize_gate_symbol(name)] = float(rate) * 100.0
            except (TypeError, ValueError):
                continue
        return out

    # ---- Market Data ----

    async def fetch_balance(self) -> dict:
        # 读余额不强制切双向：有仓位时 Gate 拒切模式也不应阻断 dashboard/救援
        return await self.exchange.fetch_balance({"type": "swap", "settle": "usdt"})

    async def fetch_ticker(self, symbol: str) -> dict:
        return await self.exchange.fetch_ticker(self._format_symbol(symbol))

    async def fetch_tickers(self, symbols: list[str] | None = None) -> dict:
        formatted = [self._format_symbol(s) for s in symbols] if symbols else None
        return await self.exchange.fetch_tickers(formatted)

    async def fetch_klines(self, symbol: str, timeframe: str = "1m", limit: int = 100) -> list:
        return await self.exchange.fetch_ohlcv(
            self._format_symbol(symbol), timeframe=timeframe, limit=limit
        )

    def _contract_size(self, formatted: str, *, require_quanto: bool = False) -> float:
        """一张合约对应的标的币数量。

        Gate 真实规格在 info.quanto_multiplier；部分币种 ccxt 的 contractSize 会落成 1，
        若误用会导致下单张数放大数十～上百倍（如 BTW quanto=100 → 6U 变成 ~600U）。

        require_quanto=True（开仓）：没有 quanto 直接拒绝，禁止静默 fallback。
        """
        try:
            market = self.exchange.market(formatted)
        except Exception:
            if require_quanto:
                raise RuntimeError(
                    f"Gate {formatted} 无法读取市场信息，拒绝开仓以防数量错误"
                ) from None
            logger.error("Gate %s market lookup failed; contractSize fallback 1.0", formatted)
            return 1.0
        info = market.get("info") if isinstance(market.get("info"), dict) else {}
        for key in ("quanto_multiplier", "quantoMultiplier"):
            raw = info.get(key)
            if raw is None or raw == "":
                continue
            try:
                q = float(raw)
            except (TypeError, ValueError):
                continue
            if q > 0:
                try:
                    cs_ccxt = float(market.get("contractSize") or 0) or 0.0
                except (TypeError, ValueError):
                    cs_ccxt = 0.0
                if cs_ccxt > 0 and abs(cs_ccxt - q) / max(q, 1e-12) > 0.01:
                    logger.warning(
                        "Gate %s contractSize mismatch: ccxt=%s quanto_multiplier=%s; using quanto",
                        formatted,
                        cs_ccxt,
                        q,
                    )
                return q
        if require_quanto:
            raise RuntimeError(
                f"Gate {formatted} 缺少 quanto_multiplier，拒绝开仓以防数量错误"
            )
        try:
            cs = float(market.get("contractSize") or 1) or 1.0
        except (TypeError, ValueError):
            cs = 1.0
        if cs <= 0:
            cs = 1.0
        if abs(cs - 1.0) < 1e-12:
            logger.warning(
                "Gate %s missing quanto_multiplier and contractSize=1; size may be wrong",
                formatted,
            )
        return cs

    @staticmethod
    def _assert_open_contracts_not_inflated(
        formatted: str,
        base_amount: float,
        contracts: float,
        cs: float,
    ) -> None:
        """开仓前核对：张×乘数 不得远超意图币数量。"""
        intended = float(base_amount)
        if intended <= 0 or contracts <= 0 or cs <= 0:
            return
        actual = float(contracts) * float(cs)
        ratio = actual / intended
        if ratio > _GATE_OPEN_SIZE_MAX_RATIO:
            raise RuntimeError(
                f"Gate 开仓数量异常放大已拒绝 {formatted}: "
                f"意图币数={intended:.6g} 实际={actual:.6g} "
                f"(张={contracts:g}×乘数={cs:g}) ratio={ratio:.2f} "
                f"> {_GATE_OPEN_SIZE_MAX_RATIO}"
            )

    def _min_open_contracts(self, formatted: str) -> float:
        """交易所允许的最小开仓张数（默认 1）。"""
        min_contracts = 1.0
        try:
            market = self.exchange.market(formatted)
        except Exception:
            return min_contracts
        limits = market.get("limits") if isinstance(market.get("limits"), dict) else {}
        amount_lim = limits.get("amount") if isinstance(limits.get("amount"), dict) else {}
        raw_min = amount_lim.get("min")
        if raw_min is not None and raw_min != "":
            try:
                v = float(raw_min)
                if v > 0:
                    min_contracts = max(min_contracts, v)
            except (TypeError, ValueError):
                pass
        info = market.get("info") if isinstance(market.get("info"), dict) else {}
        for key in ("order_size_min", "order_size"):
            raw = info.get(key)
            if raw is None or raw == "":
                continue
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            if v > 0:
                min_contracts = max(min_contracts, v)
                break
        return min_contracts

    async def estimate_min_open_notional(self, symbol: str, price: float) -> float | None:
        """最小开仓名义(USDT) ≈ min_contracts × quanto × price。"""
        if price is None or float(price) <= 0:
            return None
        await self.ensure_markets_loaded()
        formatted = self._format_symbol(symbol)
        try:
            cs = self._contract_size(formatted, require_quanto=True)
        except RuntimeError:
            return None
        if cs <= 0:
            return None
        min_contracts = self._min_open_contracts(formatted)
        min_n = float(min_contracts) * float(cs) * float(price)
        try:
            market = self.exchange.market(formatted)
            limits = market.get("limits") if isinstance(market.get("limits"), dict) else {}
            cost_lim = limits.get("cost") if isinstance(limits.get("cost"), dict) else {}
            cost_min = cost_lim.get("min")
            if cost_min is not None and cost_min != "":
                cm = float(cost_min)
                if cm > 0:
                    min_n = max(min_n, cm)
        except Exception:
            pass
        return min_n if min_n > 0 else None

    async def _base_amount_to_contracts(
        self,
        formatted: str,
        base_amount: float,
        *,
        guard_open: bool = False,
    ) -> tuple[float, float]:
        """业务层按币数量传入；Gate/ccxt 下单要「张」。返回 (contracts, contractSize)。"""
        await self.ensure_markets_loaded()
        cs = self._contract_size(formatted, require_quanto=guard_open)
        raw = float(base_amount) / cs
        try:
            contracts = float(self.exchange.amount_to_precision(formatted, raw))
        except Exception:
            contracts = raw
        # enable_decimal=false 时精度可能把 <1 张打成 0；勿静默抬到 1 张（会放大名义）
        if contracts <= 0:
            raise RuntimeError(
                f"Gate 下单数量过小无法换算为合约张数: base={base_amount} cs={cs} raw={raw}"
            )
        if guard_open:
            self._assert_open_contracts_not_inflated(formatted, base_amount, contracts, cs)
        return contracts, cs

    @staticmethod
    def _order_qty_to_base(order: dict, contract_size: float) -> dict:
        """把成交回报里的张数换回币数量，供 position_manager / DB 与币安语义一致。"""
        if not order or contract_size <= 0:
            return order or {}
        out = dict(order)
        for key in ("filled", "amount", "remaining"):
            if out.get(key) is None:
                continue
            try:
                out[key] = float(out[key]) * contract_size
            except (TypeError, ValueError):
                pass
        return out

    @staticmethod
    def _dual_mode_side(row: dict) -> str | None:
        """Gate 双向仓位：用 info.mode=dual_long|dual_short 纠正 side（两侧 size 常为正）。"""
        info = row.get("info") if isinstance(row.get("info"), dict) else {}
        mode = str(info.get("mode") or row.get("mode") or "").lower()
        if mode in ("dual_long", "long"):
            return "long"
        if mode in ("dual_short", "short"):
            return "short"
        return None

    def _normalize_positions_to_base(self, positions: list[dict]) -> list[dict]:
        """contracts→币数量；强制 dual_long/dual_short → side；contractSize 置 1。

        乘数一律走 _contract_size（优先 quanto），不信任仓位行里可能错误的 contractSize=1。
        """
        out: list[dict] = []
        for p in positions or []:
            row = dict(p)
            dual_side = self._dual_mode_side(row)
            if dual_side:
                row["side"] = dual_side
            try:
                contracts = float(row.get("contracts", 0) or 0)
            except (TypeError, ValueError):
                contracts = 0.0
            sym = row.get("symbol") or ""
            try:
                cs = self._contract_size(self._format_symbol(sym))
            except Exception:
                cs = 1.0
            if cs <= 0:
                cs = 1.0
            if contracts != 0:
                row["contracts"] = abs(contracts) * cs
                row["contractSize"] = 1.0
                mark = float(row.get("markPrice", 0) or 0)
                if mark <= 0:
                    try:
                        mark = float(row.get("entryPrice", 0) or 0)
                    except (TypeError, ValueError):
                        mark = 0.0
                if mark > 0 and row.get("notional") in (None, 0, 0.0):
                    row["notional"] = float(row["contracts"]) * mark
            out.append(row)
        return out

    async def fetch_positions(self, symbols: list[str] | None = None) -> list[dict]:
        # 读仓不强制切双向，避免有仓时无法对账/平仓救援
        params = {"settle": "usdt"}
        if not symbols:
            raw = await self.exchange.fetch_positions(None, params)
            return self._normalize_positions_to_base(raw)
        formatted = [self._format_symbol(s) for s in symbols]
        try:
            raw = await self.exchange.fetch_positions(formatted, params)
            return self._normalize_positions_to_base(raw)
        except Exception as e:
            msg = str(e).lower()
            if "does not have market symbol" in msg or "invalid symbol" in msg:
                try:
                    await self.exchange.load_markets(True)
                    raw = await self.exchange.fetch_positions(formatted, params)
                    return self._normalize_positions_to_base(raw)
                except Exception as e2:
                    logger.debug("Gate fetch_positions retry failed: %s", e2)
                raw = await self.exchange.fetch_positions(None, params)
                want = {normalize_gate_symbol(s) for s in symbols}
                filtered = [
                    p for p in raw
                    if normalize_gate_symbol(p.get("symbol") or "") in want
                ]
                return self._normalize_positions_to_base(filtered)
            raise

    async def set_symbol_leverage(self, symbol: str, leverage: int) -> tuple[int, bool]:
        # Gate/ccxt 常见上限 100；双向账户必须走 dual_comp，并显式 cross 避免被改成逐仓
        lev = max(1, min(100, int(leverage)))
        formatted = self._format_symbol(symbol)
        key = normalize_gate_symbol(symbol)
        cached = self._leverage_cache.get(key)
        if cached == lev:
            return lev, True
        params = {"settle": "usdt", "marginMode": "cross"}
        try:
            if self.hedge_mode and hasattr(
                self.exchange, "privateFuturesPostSettleDualCompPositionsContractLeverage"
            ):
                # BTC/USDT:USDT → BTC_USDT
                base = formatted.split("/")[0] if "/" in formatted else key[:-4]
                contract = f"{base}_USDT"
                await self.exchange.privateFuturesPostSettleDualCompPositionsContractLeverage(
                    {
                        "settle": "usdt",
                        "contract": contract,
                        "leverage": "0",
                        "cross_leverage_limit": str(lev),
                    }
                )
            else:
                await self.exchange.set_leverage(lev, formatted, params)
            self._leverage_cache[key] = lev
            return lev, False
        except Exception as e:
            msg = str(e).lower()
            if "already" in msg:
                self._leverage_cache[key] = lev
                return lev, False
            try:
                await self.exchange.set_leverage(lev, formatted, params)
                self._leverage_cache[key] = lev
                return lev, False
            except Exception as e2:
                raise RuntimeError(f"Gate 设置杠杆失败 {key} {lev}x: {e2}") from e2

    def _order_params(self, position_side: str, reduce_only: bool = False) -> dict:
        """Gate FuturesOrder 合法字段：settle + reduceOnly。

        多空由 ccxt 根据 buy/sell 生成有符号 size；勿传 dual_side/positionSide（会污染请求体）。
        """
        params: dict = {"settle": "usdt"}
        if reduce_only:
            params["reduceOnly"] = True
        return params

    async def cancel_order(self, order_id: str, symbol: str) -> dict:
        return await self.exchange.cancel_order(
            order_id, self._format_symbol(symbol), {"settle": "usdt"}
        )

    async def fetch_order(self, order_id: str, symbol: str) -> dict:
        formatted = self._format_symbol(symbol)
        order = await self.exchange.fetch_order(order_id, formatted, {"settle": "usdt"})
        cs = self._contract_size(formatted)
        return self._order_qty_to_base(order, cs)

    async def fetch_open_orders(self, symbol: str | None = None) -> list[dict]:
        formatted = self._format_symbol(symbol) if symbol else None
        params = {"settle": "usdt"}
        orders = await self.exchange.fetch_open_orders(formatted, params=params)
        out: list[dict] = []
        for o in orders or []:
            sym = o.get("symbol") or formatted or ""
            try:
                cs = self._contract_size(sym if "/" in str(sym) else self._format_symbol(str(sym)))
            except Exception:
                cs = 1.0
            out.append(self._order_qty_to_base(dict(o), cs))
        return out

    async def create_market_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        reduce_only: bool = False,
        position_side: str = "LONG",
        slippage_pct: float | None = None,
    ) -> dict:
        """amount 为币数量（与币安业务层一致），内部换算为合约张数。"""
        # 仅增仓路径强制双向；reduce-only 平仓/止盈不阻断
        if not reduce_only:
            await self.ensure_dual_mode()
        formatted = self._format_symbol(symbol)
        contracts, cs = await self._base_amount_to_contracts(
            formatted, amount, guard_open=not reduce_only
        )
        params = self._order_params(position_side, reduce_only)

        if slippage_pct and slippage_pct > 0:
            ticker = await self.exchange.fetch_ticker(formatted)
            ref_price = float(ticker.get("last", 0) or 0)
            order = await self.exchange.create_order(
                formatted, "market", side, contracts, None, params
            )
            order = self._order_qty_to_base(order, cs)
            if ref_price > 0:
                avg_price = float(order.get("average", 0) or 0)
                if avg_price > 0:
                    if side == "buy":
                        slip = ((avg_price - ref_price) / ref_price) * 100
                    else:
                        slip = ((ref_price - avg_price) / ref_price) * 100
                    if slip > slippage_pct:
                        logger.warning(
                            "Gate slippage %.2f%% > %.2f%% for %s %s",
                            slip, slippage_pct, side, formatted,
                        )
        else:
            order = await self.exchange.create_order(
                formatted, "market", side, contracts, None, params
            )
            order = self._order_qty_to_base(order, cs)

        filled = float(order.get("filled") or 0)
        if filled <= 0 and not reduce_only:
            status = str(order.get("status") or "").lower()
            if status not in ("closed", "filled"):
                raise RuntimeError(
                    f"Gate 市价开仓未成交 {symbol} {side}: status={status} filled={filled}"
                )
        return order

    async def create_limit_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float,
        reduce_only: bool = False,
        position_side: str = "LONG",
    ) -> dict:
        """amount 为币数量（与币安业务层一致），内部换算为合约张数。"""
        if not reduce_only:
            await self.ensure_dual_mode()
        formatted = self._format_symbol(symbol)
        contracts, cs = await self._base_amount_to_contracts(
            formatted, amount, guard_open=not reduce_only
        )
        order = await self.exchange.create_order(
            formatted,
            "limit",
            side,
            contracts,
            price,
            self._order_params(position_side, reduce_only),
        )
        return self._order_qty_to_base(order, cs)

    async def close_position_qty(self, symbol: str, side: str, amount: float) -> dict:
        """按数量减仓平仓（reduceOnly），不扫整腿。"""
        qty = float(amount or 0)
        if qty <= 0:
            logger.warning("Gate close_position_qty: non-positive amount for %s %s", symbol, side)
            return {}
        await self.ensure_markets_loaded()
        position_side = "LONG" if side == "long" else "SHORT"
        close_side = "sell" if side == "long" else "buy"
        return await self.create_market_order(
            symbol,
            close_side,
            qty,
            reduce_only=True,
            position_side=position_side,
        )

    async def close_position(self, symbol: str, side: str) -> dict:
        """Close entire symbol+side leg（账户删除等）。策略平仓请用 close_position_qty。"""
        formatted = self._format_symbol(symbol)
        await self.ensure_markets_loaded()
        want = side.lower()
        positions = await self.fetch_positions([symbol])
        total_base = 0.0
        for pos in positions:
            if normalize_gate_symbol(pos.get("symbol") or "") != normalize_gate_symbol(symbol):
                continue
            pos_side = (pos.get("side") or "").lower()
            if not pos_side:
                pos_side = self._dual_mode_side(pos) or ""
            if pos_side == want and float(pos.get("contracts", 0) or 0) > 0:
                total_base += float(pos["contracts"])

        if total_base <= 0:
            logger.warning("Gate close_position: no contracts for %s %s", symbol, side)
            return {}

        try:
            return await self.close_position_qty(symbol, side, total_base)
        except Exception as e:
            logger.warning(
                "Gate close_position sized reduce failed for %s %s, try auto_size: %s",
                symbol, side, e,
            )

        position_side = "LONG" if side == "long" else "SHORT"
        close_side = "sell" if side == "long" else "buy"
        auto = "close_long" if want == "long" else "close_short"
        params = {"settle": "usdt", "reduceOnly": True, "auto_size": auto}
        try:
            order = await self.exchange.create_order(
                formatted, "market", close_side, 0, None, params
            )
            cs = self._contract_size(formatted)
            return self._order_qty_to_base(order, cs)
        except Exception as e2:
            raise RuntimeError(f"Gate 平仓失败 {symbol} {side}: {e2}") from e2

    async def close_position_with_limit(self, symbol: str, side: str, price: float) -> dict:
        position_side = "LONG" if side == "long" else "SHORT"
        close_side = "sell" if side == "long" else "buy"
        want = side.lower()
        positions = await self.fetch_positions([symbol])
        total_base = 0.0
        for pos in positions:
            if normalize_gate_symbol(pos.get("symbol") or "") != normalize_gate_symbol(symbol):
                continue
            pos_side = (pos.get("side") or "").lower() or (self._dual_mode_side(pos) or "")
            if pos_side == want and float(pos.get("contracts", 0) or 0) > 0:
                total_base += float(pos["contracts"])
        if total_base <= 0:
            logger.warning("Gate close_position_with_limit: no contracts for %s %s", symbol, side)
            return {}
        return await self.create_limit_order(
            symbol, close_side, total_base, price,
            reduce_only=True, position_side=position_side,
        )

    async def watch_tickers(self, symbols: list[str] | None = None):
        formatted = [self._format_symbol(s) for s in symbols] if symbols else None
        return await self.ws_exchange.watch_tickers(formatted)

    async def watch_klines(self, symbol: str, timeframe: str = "1m"):
        return await self.ws_exchange.watch_ohlcv(self._format_symbol(symbol), timeframe)

    async def watch_positions(self, symbols: list[str] | None = None):
        raise NotImplementedError("Gate 账户持仓推送未接入")

    async def watch_trades(self, symbol: str):
        raise NotImplementedError("毫秒接针价流暂不支持 Gate")

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

    @staticmethod
    def _is_usdt_perp_symbol(sym: str) -> bool:
        """仅 USDT 永续：BTC/USDT:USDT 或 BTC_USDT；排除现货/交割。"""
        s = str(sym or "").strip()
        if not s:
            return False
        if ":" in s:
            # ccxt：BTC/USDT:USDT；交割 BTC/USDT:USDT-240628
            return s.rsplit(":", 1)[-1] == "USDT"
        su = s.upper()
        if "/" in su:
            return False  # 现货 BTC/USDT
        return su.endswith("_USDT") and "-" not in su

    async def _fetch_futures_usdt_tickers_raw(self) -> list[dict]:
        """Gate 官方合约 ticker（含 change_percentage），与 App 涨幅榜同源。"""
        ex = self.exchange
        if hasattr(ex, "publicFuturesGetSettleTickers"):
            rows = await ex.publicFuturesGetSettleTickers({"settle": "usdt"})
            return list(rows) if isinstance(rows, list) else []
        # 无原生方法时：从 ccxt ticker.info 拼出同源字段
        tickers = await ex.fetch_tickers()
        out: list[dict] = []
        for sym, t in (tickers or {}).items():
            if not self._is_usdt_perp_symbol(sym):
                continue
            info = t.get("info") if isinstance(t.get("info"), dict) else {}
            contract = info.get("contract") or sym
            out.append({
                "contract": contract,
                "change_percentage": info.get("change_percentage", t.get("percentage")),
                "volume_24h_quote": info.get("volume_24h_quote") or t.get("quoteVolume"),
                "volume_24h_settle": info.get("volume_24h_settle"),
                "volume_24h": info.get("volume_24h"),
            })
        return out

    @staticmethod
    def _ticker_row_to_mover(t: dict) -> dict | None:
        contract = str(t.get("contract") or t.get("symbol") or "")
        if not GateService._is_usdt_perp_symbol(contract):
            return None
        pct_raw = t.get("change_percentage")
        if pct_raw is None:
            return None
        try:
            pct = float(pct_raw)
        except (TypeError, ValueError):
            return None
        vol = 0.0
        for key in ("volume_24h_quote", "volume_24h_settle", "volume_24h"):
            if t.get(key) is None:
                continue
            try:
                vol = float(t.get(key) or 0)
                break
            except (TypeError, ValueError):
                continue
        return {
            "symbol": normalize_gate_symbol(contract),
            "price_change_pct": pct,
            "volume_24h": vol,
        }

    async def fetch_top_movers(self, source: str = "both", limit: int = 20) -> list[dict]:
        """涨跌榜：用 Gate /futures/usdt/tickers 的 change_percentage（勿用 ccxt.percentage）。"""
        usdt_pairs: list[dict] = []
        try:
            raw = await self._fetch_futures_usdt_tickers_raw()
            for t in raw:
                if isinstance(t, dict):
                    item = self._ticker_row_to_mover(t)
                    if item:
                        usdt_pairs.append(item)
        except Exception as e:
            logger.warning("Gate futures tickers failed, fallback ccxt fetch_tickers: %s", e)
            tickers = await self.exchange.fetch_tickers()
            for sym, t in (tickers or {}).items():
                if not self._is_usdt_perp_symbol(sym):
                    continue
                info = t.get("info") if isinstance(t.get("info"), dict) else {}
                item = self._ticker_row_to_mover({
                    "contract": info.get("contract") or sym,
                    "change_percentage": info.get("change_percentage", t.get("percentage")),
                    "volume_24h_quote": info.get("volume_24h_quote") or t.get("quoteVolume"),
                    "volume_24h_settle": info.get("volume_24h_settle"),
                    "volume_24h": info.get("volume_24h"),
                })
                if item:
                    usdt_pairs.append(item)

        gainers = (
            sorted(usdt_pairs, key=lambda x: -x["price_change_pct"])[:limit]
            if source in ("gainers", "both") else []
        )
        losers = (
            sorted(usdt_pairs, key=lambda x: x["price_change_pct"])[:limit]
            if source in ("losers", "both") else []
        )
        result = []
        for i, item in enumerate(gainers):
            result.append({**item, "rank": i + 1, "source": "gainers"})
        for i, item in enumerate(losers):
            result.append({**item, "rank": i + 1, "source": "losers"})
        return result

    @staticmethod
    def _format_symbol(symbol: str) -> str:
        """业务 BTCUSDT / Gate BTC_USDT → ccxt BTC/USDT:USDT。"""
        if "/" in symbol:
            if ":USDT" not in symbol and symbol.endswith("/USDT"):
                return f"{symbol}:USDT"
            return symbol
        s = symbol.replace("_", "").upper()
        if s.endswith("USDT"):
            base = s[:-4]
            return f"{base}/USDT:USDT"
        return symbol


# ---- Cached filter helpers (Gate-specific module caches) ----

async def get_cached_gate_tradefi_symbols(gate: GateService) -> frozenset[str]:
    now = time.time()
    hit = _TRADEFI_CACHE.get("gate")
    if hit is not None and now - hit[0] < _CACHE_TTL_TRADEFI:
        return hit[1]
    raw = await gate.fetch_tradefi_perpetual_symbols_raw()
    norm = frozenset(normalize_gate_symbol(s) for s in raw) | EXCLUDED_COMMODITY_SYMBOLS
    # 也并入主流表外的商品前缀判断由调用方处理
    _TRADEFI_CACHE["gate"] = (now, norm)
    return norm


async def get_cached_gate_funding_rates_pct(gate: GateService) -> dict[str, float]:
    now = time.time()
    hit = _FUNDING_CACHE.get("gate")
    if hit is not None and now - hit[0] < _CACHE_TTL_FUNDING:
        return hit[1]
    raw = await gate.fetch_last_funding_rates_pct_raw()
    _FUNDING_CACHE["gate"] = (now, raw)
    return raw


# ---- Singleton factory ----

_private_instances: dict[str, tuple[float, GateService]] = {}
_public_instance: Optional[GateService] = None
_public_created_at: float = 0.0
_INSTANCE_TTL = 7200


def _private_cache_key(api_key: str, secret: str, hedge_mode: bool) -> str:
    digest = hashlib.sha256(f"{api_key}\0{secret}".encode("utf-8")).hexdigest()
    return f"gate:{digest}:{hedge_mode}"


async def get_gate_service(api_key: str, secret: str, hedge_mode: bool = True) -> GateService:
    global _private_instances
    cache_key = _private_cache_key(api_key, secret, hedge_mode)
    now = time.time()
    if cache_key in _private_instances:
        created, svc = _private_instances[cache_key]
        if now - created < _INSTANCE_TTL:
            return svc
        try:
            await svc.close()
        except Exception:
            pass
    svc = GateService(api_key, secret, hedge_mode=hedge_mode)
    _private_instances[cache_key] = (now, svc)
    return svc


async def clear_private_gate_service(api_key: str, secret: str, hedge_mode: bool = True) -> None:
    cache_key = _private_cache_key(api_key, secret, hedge_mode)
    entry = _private_instances.pop(cache_key, None)
    if not entry:
        return
    _, svc = entry
    try:
        await svc.close()
    except Exception:
        pass


async def get_public_gate() -> GateService:
    global _public_instance, _public_created_at
    now = time.time()
    if _public_instance is not None and (now - _public_created_at) > _INSTANCE_TTL:
        try:
            await _public_instance.close()
        except Exception:
            pass
        _public_instance = None
    if _public_instance is None or (now - _public_created_at) > _INSTANCE_TTL:
        _public_instance = GateService(api_key="", secret="", hedge_mode=True)
        _public_created_at = now
    return _public_instance
