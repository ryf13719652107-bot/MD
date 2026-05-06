"""In-memory log buffer for strategy execution, exposed via REST API."""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from ..config import now_beijing

MAX_ENTRIES = 200


@dataclass
class LogEntry:
    time: str
    level: str  # 'info', 'warning', 'error', 'success'
    message: str


class StrategyLogService:
    def __init__(self):
        self._buffers: dict[int, list[LogEntry]] = defaultdict(list)

    def add(self, strategy_id: int, level: str, message: str):
        entry = LogEntry(
            time=now_beijing().strftime("%H:%M:%S"),
            level=level,
            message=message,
        )
        buf = self._buffers[strategy_id]
        buf.append(entry)
        if len(buf) > MAX_ENTRIES:
            # Keep most recent entries
            self._buffers[strategy_id] = buf[-MAX_ENTRIES // 2:]

    def info(self, strategy_id: int, msg: str):
        self.add(strategy_id, "info", msg)

    def warning(self, strategy_id: int, msg: str):
        self.add(strategy_id, "warning", msg)

    def error(self, strategy_id: int, msg: str):
        self.add(strategy_id, "error", msg)

    def success(self, strategy_id: int, msg: str):
        self.add(strategy_id, "success", msg)

    def get(self, strategy_id: int, limit: int = 100) -> list[dict]:
        buf = self._buffers.get(strategy_id, [])
        # Return newest first
        return [
            {"time": e.time, "level": e.level, "message": e.message}
            for e in buf[-limit:][::-1]
        ]

    def clear(self, strategy_id: int):
        self._buffers.pop(strategy_id, None)


# Singleton
strategy_log_service = StrategyLogService()
