import asyncio
from collections import defaultdict
from typing import Any

from fastapi import WebSocket


class EventHub:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    async def publish(self, event: str, payload: dict[str, Any]) -> None:
        message = {"event": event, "payload": payload}
        for queue in tuple(self._subscribers[event]):
            await queue.put(message)
        dead: list[WebSocket] = []
        for connection in tuple(self._connections):
            try:
                await connection.send_json(message)
            except Exception:
                dead.append(connection)
        for connection in dead:
            self.disconnect(connection)

    async def subscribe(self, event: str):
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers[event].add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers[event].discard(queue)


events = EventHub()
