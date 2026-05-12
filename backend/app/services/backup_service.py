"""Append-only JSONL backup for every Trade record — survives DB wipes."""
import json
import os
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Project-root-relative; adjust if running from elsewhere
_BACKUP_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "backups"
_BACKUP_FILE = _BACKUP_DIR / "trades.jsonl"


def _ensure_dir() -> None:
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)


def backup_trade(trade) -> None:
    """Append a single Trade row to the append-only JSONL backup.

    Accepts a Trade ORM object or a dict with the same keys.
    """
    try:
        _ensure_dir()
        if hasattr(trade, "__dict__"):
            d = {
                "id": getattr(trade, "id", None),
                "strategy_id": getattr(trade, "strategy_id", None),
                "account_id": getattr(trade, "account_id", None),
                "symbol": getattr(trade, "symbol", None),
                "side": getattr(trade, "side", None),
                "quantity": getattr(trade, "quantity", None),
                "entry_price": getattr(trade, "entry_price", None),
                "exit_price": getattr(trade, "exit_price", None),
                "realized_pnl": getattr(trade, "realized_pnl", None),
                "pnl_pct": getattr(trade, "pnl_pct", None),
                "entry_time": _serialize_dt(getattr(trade, "entry_time", None)),
                "exit_time": _serialize_dt(getattr(trade, "exit_time", None)),
                "layer": getattr(trade, "layer", None),
                "close_reason": getattr(trade, "close_reason", None),
            }
        else:
            d = dict(trade)
        with open(_BACKUP_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(d, ensure_ascii=False, default=str) + "\n")
    except Exception:
        logger.exception("Failed to write trade backup — record may be lost if DB is wiped")


def restore_trades_from_backup() -> list[dict]:
    """Read all backed-up trades from JSONL. Returns list of dicts, newest first."""
    trades: list[dict] = []
    try:
        if not _BACKUP_FILE.exists():
            return trades
        with open(_BACKUP_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    trades.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("Skipping corrupt backup line: %.80s", line)
        trades.reverse()  # oldest first in file → newest first for consistency
    except Exception:
        logger.exception("Failed to read trade backup")
    return trades


def backup_stats() -> dict:
    """Return count and file size of the backup."""
    try:
        if not _BACKUP_FILE.exists():
            return {"count": 0, "size_bytes": 0, "path": str(_BACKUP_FILE)}
        size = _BACKUP_FILE.stat().st_size
        count = 0
        with open(_BACKUP_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    count += 1
        return {"count": count, "size_bytes": size, "path": str(_BACKUP_FILE)}
    except Exception:
        return {"count": 0, "size_bytes": 0, "path": str(_BACKUP_FILE)}


def _serialize_dt(dt) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)
