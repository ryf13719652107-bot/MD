"""GATE 接入：符号规范化、涨跌榜、TradFi 过滤、工厂路由、下架跳过。"""

import pytest

from app.services.exchange_factory import (
    account_exchange_id,
    normalize_exchange_id,
)
from app.services.gate_service import (
    GateService,
    normalize_gate_symbol,
)
from app.services.binance_service import (
    get_strategy_pool_exclude_symbols,
    EXCLUDED_MAINSTREAM_SYMBOLS,
)


def test_normalize_exchange_id():
    assert normalize_exchange_id(None) == "binance"
    assert normalize_exchange_id("GATE") == "gate"
    assert normalize_exchange_id("binance") == "binance"
    assert normalize_exchange_id("unknown") == "binance"


def test_coin_pool_config_isolated_by_exchange():
    from app.services.coin_pool_service import CoinPoolService

    svc = CoinPoolService()
    svc.config_for("binance").update(
        refresh_interval_seconds=3600, fetch_mode="interval"
    )
    svc.config_for("gate").update(
        refresh_interval_seconds=600, fetch_mode="scheduled", anchor_hour=3
    )
    # 币安节奏不被 GATE 覆盖
    assert svc.config_for("binance")["refresh_interval_seconds"] == 3600
    assert svc.config_for("binance")["fetch_mode"] == "interval"
    assert svc.config_for("gate")["refresh_interval_seconds"] == 600
    assert svc._seconds_until_next_refresh(None, exchange="binance") == 0.0


def test_account_exchange_id():
    class Acc:
        exchange = "gate"

    assert account_exchange_id(Acc()) == "gate"
    assert account_exchange_id(object()) == "binance"


def test_normalize_gate_symbol():
    assert normalize_gate_symbol("BTC_USDT") == "BTCUSDT"
    assert normalize_gate_symbol("BTC/USDT:USDT") == "BTCUSDT"
    assert normalize_gate_symbol("btcusdt") == "BTCUSDT"


def test_gate_format_symbol():
    assert GateService._format_symbol("BTCUSDT") == "BTC/USDT:USDT"
    assert GateService._format_symbol("BTC_USDT") == "BTC/USDT:USDT"
    assert GateService._format_symbol("ETH/USDT") == "ETH/USDT:USDT"


@pytest.mark.asyncio
async def test_gate_fetch_top_movers_sort():
    svc = GateService()

    class FakeEx:
        async def publicFuturesGetSettleTickers(self, params):
            assert params.get("settle") == "usdt"
            return [
                {"contract": "AAA_USDT", "change_percentage": "10.0", "volume_24h_quote": "100"},
                {"contract": "BBB_USDT", "change_percentage": "-8.0", "volume_24h_quote": "200"},
                {"contract": "CCC_USDT", "change_percentage": "5.0", "volume_24h_quote": "50"},
                {"contract": "DDD_BTC", "change_percentage": "99.0", "volume_24h_quote": "1"},
                # 交割合约应排除
                {"contract": "EEE_USDT-240628", "change_percentage": "50.0", "volume_24h_quote": "999"},
                # 模拟 ccxt.percentage 错误、官方 change_percentage 正确
                {"contract": "BLESS_USDT", "change_percentage": "74.46", "volume_24h_quote": "15573500"},
            ]

    svc._exchange = FakeEx()
    svc._created_at = __import__("time").time()
    svc._pinned = True

    movers = await svc.fetch_top_movers("both", limit=2)
    gainers = [m for m in movers if m["source"] == "gainers"]
    losers = [m for m in movers if m["source"] == "losers"]
    assert [m["symbol"] for m in gainers] == ["BLESSUSDT", "AAAUSDT"]
    assert abs(gainers[0]["price_change_pct"] - 74.46) < 1e-9
    assert losers[0]["symbol"] == "BBBUSDT"
    assert losers[0]["rank"] == 1
    assert all("EEE" not in m["symbol"] for m in movers)


def test_gate_is_usdt_perp_symbol():
    assert GateService._is_usdt_perp_symbol("BTC/USDT:USDT")
    assert GateService._is_usdt_perp_symbol("BTC_USDT")
    assert not GateService._is_usdt_perp_symbol("BTC/USDT:USDT-240628")
    assert not GateService._is_usdt_perp_symbol("BTC/USDT")


def test_gate_order_params_clean():
    svc = GateService(hedge_mode=True)
    p = svc._order_params("LONG", reduce_only=True)
    assert p == {"settle": "usdt", "reduceOnly": True}
    assert "positionSide" not in p
    assert "dual_side" not in p


def test_tp_limit_reduce_only_only_for_gate():
    from app.services.position_manager import _tp_limit_reduce_only

    class BinanceLike:
        exchange_id = "binance"

    class GateLike:
        exchange_id = "gate"

    assert _tp_limit_reduce_only(BinanceLike()) is False
    assert _tp_limit_reduce_only(GateLike()) is True
    assert _tp_limit_reduce_only(object()) is False


@pytest.mark.asyncio
async def test_gate_ensure_dual_mode_accepts_no_change():
    """交易所已是双向时 Gate 返回 NO_CHANGE，应视为成功而非阻断开仓。"""
    svc = GateService(api_key="k", secret="s", hedge_mode=True)
    svc._pinned = True
    svc._created_at = __import__("time").time()

    class FakeEx:
        async def set_position_mode(self, hedged):
            raise Exception('gate {"label":"NO_CHANGE"}')

    svc._exchange = FakeEx()
    await svc.ensure_dual_mode()
    assert svc._dual_mode_ensured is True


def test_gate_dual_mode_side_and_normalize():
    svc = GateService()
    svc._markets_loaded = True

    class FakeEx:
        def market(self, _sym):
            return {"contractSize": 0.0001}

    svc._exchange = FakeEx()
    rows = svc._normalize_positions_to_base(
        [
            {
                "symbol": "BTC/USDT:USDT",
                "side": "long",
                "contracts": 100,
                "contractSize": 0.0001,
                "info": {"mode": "dual_long"},
            },
            {
                "symbol": "BTC/USDT:USDT",
                "side": "long",  # 双向空仓 size 也可能为正，side 不可靠
                "contracts": 50,
                "contractSize": 0.0001,
                "info": {"mode": "dual_short"},
            },
        ]
    )
    assert rows[0]["side"] == "long"
    assert abs(rows[0]["contracts"] - 0.01) < 1e-12
    assert rows[1]["side"] == "short"
    assert abs(rows[1]["contracts"] - 0.005) < 1e-12


@pytest.mark.asyncio
async def test_gate_tradefi_from_contract_type():
    svc = GateService()

    async def fake_contracts():
        return [
            {"name": "BTC_USDT", "contract_type": ""},
            {"name": "AAPL_USDT", "contract_type": "stocks"},
            {"name": "XAU_USDT", "contract_type": "metals"},
            {"name": "ETH_USDT", "contract_type": "crypto"},
        ]

    svc.fetch_futures_contracts_raw = fake_contracts  # type: ignore
    raw = await svc.fetch_tradefi_perpetual_symbols_raw()
    assert "AAPLUSDT" in raw
    assert "XAUUSDT" in raw
    assert "BTCUSDT" not in raw


@pytest.mark.asyncio
async def test_gate_delisting_skipped_in_exclude():
    class FakeGate:
        exchange_id = "gate"

        async def fetch_tradefi_perpetual_symbols_raw(self):
            return set()

        async def fetch_delisting_soon_symbols_raw(self):
            return {"SHOULDNOTAPPEARUSDT"}

    excluded = await get_strategy_pool_exclude_symbols(
        FakeGate(),
        exclude_tradefi=False,
        exclude_delisting=True,
        exclude_mainstream=True,
    )
    assert excluded is not None
    assert "SHOULDNOTAPPEARUSDT" not in excluded
    assert "BTCUSDT" in excluded
    assert excluded <= EXCLUDED_MAINSTREAM_SYMBOLS or "BTCUSDT" in excluded


@pytest.mark.asyncio
async def test_gate_fetch_delisting_empty():
    svc = GateService()
    assert await svc.fetch_delisting_soon_symbols_raw() == set()


def test_gate_order_qty_to_base():
    order = {"filled": 10.0, "amount": 10.0, "remaining": 0.0}
    out = GateService._order_qty_to_base(order, 0.0001)
    assert abs(out["filled"] - 0.001) < 1e-12
    assert abs(out["amount"] - 0.001) < 1e-12


def test_extract_gate_balance_prefers_total():
    from app.services.gate_service import extract_gate_usdt_wallet_balance

    bal = {
        "total": {"USDT": 100.0},
        "free": {"USDT": 80.0},
        "info": {"total": "100", "unrealised_pnl": "999"},
    }
    assert extract_gate_usdt_wallet_balance(bal) == 100.0


def test_extract_gate_margin_includes_unrealized():
    from app.services.exchange_factory import extract_dashboard_balances, extract_margin_balance
    from app.services.gate_service import extract_gate_usdt_margin_balance

    bal = {
        "total": {"USDT": 300.0},
        "free": {"USDT": 280.0},
        "info": {
            "total": "300",
            "unrealised_pnl": "12.5",
            "currency": "USDT",
        },
    }
    assert extract_gate_usdt_margin_balance(bal) == 312.5

    class _Gate:
        exchange_id = "gate"

    assert extract_margin_balance(_Gate(), bal) == 312.5
    wallet, margin = extract_dashboard_balances(_Gate(), bal, unrealized_pnl=0.0)
    assert wallet == 300.0
    assert margin == 312.5


def test_extract_gate_margin_prefers_cross_margin_balance():
    from app.services.gate_service import extract_gate_usdt_margin_balance

    bal = {
        "info": {
            "total": "300",
            "unrealised_pnl": "10",
            "cross_margin_balance": "315.5",
            "cross_unrealised_pnl": "15.5",
        }
    }
    assert extract_gate_usdt_margin_balance(bal) == 315.5


@pytest.mark.asyncio
async def test_gate_base_to_contracts():
    svc = GateService()
    svc._pinned = True
    svc._created_at = 0
    svc._markets_loaded = True

    class FakeEx:
        def market(self, _sym):
            return {"contractSize": 0.0001}

        def amount_to_precision(self, _sym, amount):
            return f"{float(amount):.0f}"

    svc._exchange = FakeEx()
    contracts, cs = await svc._base_amount_to_contracts("BTC/USDT:USDT", 0.01)
    assert cs == 0.0001
    assert contracts == 100.0


@pytest.mark.asyncio
async def test_gate_prefers_quanto_multiplier_over_wrong_contract_size():
    """BTW 类：ccxt contractSize=1 但 Gate quanto=100 → 必须用 100，否则 6U→~600U。"""
    svc = GateService()
    svc._pinned = True
    svc._created_at = 0
    svc._markets_loaded = True

    class FakeEx:
        def market(self, _sym):
            return {
                "contractSize": 1,
                "info": {"quanto_multiplier": "100", "name": "BTW_USDT"},
            }

        def amount_to_precision(self, _sym, amount):
            # Gate enable_decimal=false：整数张
            return str(int(float(amount)))

    svc._exchange = FakeEx()
    # 6 USDT @ 0.1312 → base≈45.73 BTW → /100 ≈ 0.457 → 精度后 0 → 应拒绝而非抬到 1 张
    base = 6.0 / 0.1312
    with pytest.raises(RuntimeError, match="过小"):
        await svc._base_amount_to_contracts("BTW/USDT:USDT", base, guard_open=True)

    # 足够大的名义：60 USDT → base≈457 → 4 张
    base60 = 60.0 / 0.1312
    contracts, cs = await svc._base_amount_to_contracts(
        "BTW/USDT:USDT", base60, guard_open=True
    )
    assert cs == 100.0
    assert contracts == 4.0


@pytest.mark.asyncio
async def test_gate_estimate_min_open_notional_btw():
    """1 张 BTW ≈ 100×price；6U 意图时最小名义应 > 6。"""
    svc = GateService()
    svc._pinned = True
    svc._created_at = 0
    svc._markets_loaded = True

    class FakeEx:
        def market(self, _sym):
            return {
                "contractSize": 1,
                "limits": {"amount": {"min": 1}},
                "info": {"quanto_multiplier": "100", "order_size_min": "1"},
            }

    svc._exchange = FakeEx()
    price = 0.1312
    min_n = await svc.estimate_min_open_notional("BTWUSDT", price)
    assert min_n is not None
    assert abs(min_n - 100 * price) < 1e-9
    assert min_n > 6.0


@pytest.mark.asyncio
async def test_gate_open_requires_quanto_multiplier():
    svc = GateService()
    svc._pinned = True
    svc._created_at = 0
    svc._markets_loaded = True

    class FakeEx:
        def market(self, _sym):
            return {"contractSize": 1, "info": {}}

        def amount_to_precision(self, _sym, amount):
            return str(int(float(amount)))

    svc._exchange = FakeEx()
    with pytest.raises(RuntimeError, match="quanto_multiplier"):
        await svc._base_amount_to_contracts("XXX/USDT:USDT", 10.0, guard_open=True)


@pytest.mark.asyncio
async def test_gate_open_rejects_inflated_contracts():
    """精度/异常路径若把张数抬爆，开仓护栏拒单。"""
    svc = GateService()
    svc._pinned = True
    svc._created_at = 0
    svc._markets_loaded = True

    class FakeEx:
        def market(self, _sym):
            return {
                "contractSize": 1,
                "info": {"quanto_multiplier": "1"},
            }

        def amount_to_precision(self, _sym, amount):
            # 故意放大
            return str(int(float(amount) * 100))

    svc._exchange = FakeEx()
    with pytest.raises(RuntimeError, match="异常放大"):
        await svc._base_amount_to_contracts("AAA/USDT:USDT", 10.0, guard_open=True)


def test_gate_normalize_uses_quanto_not_row_contract_size():
    svc = GateService()
    svc._markets_loaded = True

    class FakeEx:
        def market(self, _sym):
            return {
                "contractSize": 1,
                "info": {"quanto_multiplier": "100"},
            }

    svc._exchange = FakeEx()
    rows = svc._normalize_positions_to_base(
        [
            {
                "symbol": "BTW/USDT:USDT",
                "side": "short",
                "contracts": 46,  # Gate 张数
                "contractSize": 1,  # ccxt 错误
                "markPrice": 0.1312,
                "info": {"mode": "dual_short"},
            }
        ]
    )
    assert rows[0]["side"] == "short"
    assert abs(rows[0]["contracts"] - 4600.0) < 1e-6  # 46×100
    assert abs(rows[0]["notional"] - 4600.0 * 0.1312) < 1e-3
