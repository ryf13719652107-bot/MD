import asyncio
import json
import logging
from fastapi import WebSocket
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 与前端 StatusBar 轮询对齐；避免每个 WS 连接各自 sleep(3) 导致多标签页疯狂刷新
_DASHBOARD_SNAPSHOT_INTERVAL_SEC = 30


class WebSocketManager:
    def __init__(self):
        self._connections: dict[str, set[WebSocket]] = {
            "market": set(),
            "dashboard": set(),
        }
        self._lock = asyncio.Lock()
        self._dashboard_snapshot_task: Optional[asyncio.Task] = None

    async def _dashboard_snapshot_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(_DASHBOARD_SNAPSHOT_INTERVAL_SEC)
                async with self._lock:
                    if not self._connections.get("dashboard"):
                        break
                await self.broadcast(
                    "dashboard",
                    {
                        "type": "snapshot",
                        "timestamp": int(asyncio.get_event_loop().time() * 1000),
                        "message": "request_update",
                    },
                )
        except asyncio.CancelledError:
            raise

    async def connect(self, websocket: WebSocket, channel: str):
        await websocket.accept()
        start_dashboard_loop = False
        async with self._lock:
            if channel not in self._connections:
                self._connections[channel] = set()
            self._connections[channel].add(websocket)
            if channel == "dashboard":
                t = self._dashboard_snapshot_task
                if t is None or t.done():
                    start_dashboard_loop = True
        if start_dashboard_loop:
            self._dashboard_snapshot_task = asyncio.create_task(
                self._dashboard_snapshot_loop()
            )

    async def disconnect(self, websocket: WebSocket, channel: str):
        cancel_task: Optional[asyncio.Task] = None
        async with self._lock:
            if channel in self._connections:
                self._connections[channel].discard(websocket)
            if channel == "dashboard" and not self._connections.get("dashboard"):
                cancel_task = self._dashboard_snapshot_task
        if cancel_task is not None and not cancel_task.done():
            cancel_task.cancel()
            try:
                await cancel_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug("dashboard snapshot task end: %s", e)

    async def broadcast(self, channel: str, message: dict[str, Any]):
        async with self._lock:
            connections = self._connections.get(channel, set())
        dead: list[WebSocket] = []
        payload = json.dumps(message)
        for ws in connections:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._connections[channel].discard(ws)


ws_manager = WebSocketManager()
