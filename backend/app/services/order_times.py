"""Parse fill / exit timestamps from ccxt or Binance order payloads."""
from datetime import datetime
from typing import Optional

from ..config import now_beijing, BEIJING_TZ


def naive_beijing_from_ms_or_s(ts) -> Optional[datetime]:
    if ts is None:
        return None
    try:
        t = float(ts)
        if t > 1e12:
            t = t / 1000.0
        return datetime.fromtimestamp(t, BEIJING_TZ).replace(tzinfo=None)
    except (TypeError, ValueError, OSError):
        return None


def exit_time_from_order(order: dict | None, *, fallback: datetime | None = None) -> datetime:
    """Best-effort exchange fill time; fallback to detection time if unavailable."""
    if order:
        parsed = _parse_order_exit_time(order)
        if parsed:
            return parsed
    return fallback or now_beijing()


def _parse_order_exit_time(order: dict) -> Optional[datetime]:
    for key in ("lastTradeTimestamp", "lastUpdateTimestamp"):
        dt = naive_beijing_from_ms_or_s(order.get(key))
        if dt:
            return dt
    info = order.get("info") or {}
    if isinstance(info, dict):
        for k in ("updateTime", "transactTime", "workingTime", "time"):
            dt = naive_beijing_from_ms_or_s(info.get(k))
            if dt:
                return dt
    return naive_beijing_from_ms_or_s(order.get("timestamp"))
