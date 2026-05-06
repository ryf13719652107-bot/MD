import asyncio
import json
from fastapi import WebSocket
from typing import Any


class WebSocketManager:
    def __init__(self):
        self._connections: dict[str, set[WebSocket]] = {
            "market": set(),
            "dashboard": set(),
        }
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, channel: str):
        await websocket.accept()
        async with self._lock:
            if channel not in self._connections:
                self._connections[channel] = set()
            self._connections[channel].add(websocket)

    async def disconnect(self, websocket: WebSocket, channel: str):
        async with self._lock:
            if channel in self._connections:
                self._connections[channel].discard(websocket)

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
