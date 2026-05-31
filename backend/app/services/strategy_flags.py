"""策略布尔开关与选币来源兼容。"""


def exclude_delisting_enabled(strategy) -> bool:
    v = getattr(strategy, "exclude_delisting", None)
    if v is None:
        return True
    return bool(v)


def exclude_mainstream_enabled(strategy) -> bool:
    v = getattr(strategy, "exclude_mainstream", None)
    if v is None:
        return True
    return bool(v)


def exclude_funding_enabled(strategy) -> bool:
    return bool(getattr(strategy, "exclude_funding", False))


def funding_rate_threshold_pct(strategy) -> float:
    v = getattr(strategy, "funding_rate_threshold_pct", None)
    if v is None:
        return 0.0
    return float(v)


def funding_rate_blocks_new_entry(direction: str, rate_pct: float, threshold_pct: float) -> bool:
    """最近结算资金费率(%)。做多：rate > 阈值过滤；做空：rate < 阈值过滤。"""
    d = (direction or "").lower()
    if d == "long":
        return rate_pct > threshold_pct
    if d == "short":
        return rate_pct < threshold_pct
    return False


def normalize_coin_pool_source(source: str | None) -> str:
    if not source:
        return "gainers"
    return source
