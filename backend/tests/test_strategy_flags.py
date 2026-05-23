from app.services.strategy_flags import normalize_coin_pool_source


def test_normalize_coin_pool_source_none_to_gainers():
    assert normalize_coin_pool_source(None) == "gainers"


def test_normalize_coin_pool_source_passthrough():
    assert normalize_coin_pool_source("losers") == "losers"
