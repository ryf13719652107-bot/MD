"""策略布尔开关与选币来源兼容。"""


def exclude_delisting_enabled(strategy) -> bool:
    v = getattr(strategy, "exclude_delisting", None)
    if v is None:
        return True
    return bool(v)


def normalize_coin_pool_source(source: str | None) -> str:
    if not source:
        return "gainers"
    return source
