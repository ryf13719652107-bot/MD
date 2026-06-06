from app.routers.trades import _apply_trade_filters, _like_pattern, _normalize_symbol_query
from app.models.trade import Trade
from sqlalchemy import select, func


def test_normalize_symbol_query():
    assert _normalize_symbol_query(" btc/usdt ") == "BTCUSDT"
    assert _normalize_symbol_query("ETH/USDT:USDT") == "ETHUSDT"


def test_like_pattern_escapes_wildcards():
    assert _like_pattern("BTC") == "%BTC%"
    assert _like_pattern("100%") == "%100\\%%"
    assert _like_pattern("A_B") == "%A\\_B%"


def test_apply_trade_filters_symbol_and_side():
    stmt = select(Trade).order_by(Trade.exit_time.desc())
    count_stmt = select(func.count(Trade.id))
    stmt, count_stmt = _apply_trade_filters(stmt, count_stmt, symbol="btc", side="long")
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "symbol" in sql.lower()
    assert "long" in sql.lower()
