from app.services.strategy_flags import exclude_mainstream_enabled, normalize_coin_pool_source


def test_normalize_coin_pool_source_none_to_gainers():
    assert normalize_coin_pool_source(None) == "gainers"


def test_normalize_coin_pool_source_passthrough():
    assert normalize_coin_pool_source("losers") == "losers"


def test_exclude_mainstream_enabled_default_true():
    class S:
        exclude_mainstream = None

    assert exclude_mainstream_enabled(S()) is True
