"""策略布尔开关：兼容旧库 NULL 默认值。"""


def exclude_delisting_enabled(strategy) -> bool:
    v = getattr(strategy, "exclude_delisting", None)
    if v is None:
        return True
    return bool(v)
