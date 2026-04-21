from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator


class SSEHub:
    def __init__(self) -> None:
        self._clients: set[asyncio.Queue[str]] = set()
        self._lock = asyncio.Lock()

    async def subscribe(self) -> AsyncIterator[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
        async with self._lock:
            self._clients.add(queue)
        try:
            while True:
                payload = await queue.get()
                yield payload
        finally:
            async with self._lock:
                self._clients.discard(queue)

    async def broadcast(self, event: str, data: dict) -> None:
        payload = f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
        async with self._lock:
            stale: list[asyncio.Queue[str]] = []
            for queue in self._clients:
                try:
                    queue.put_nowait(payload)
                except asyncio.QueueFull:
                    stale.append(queue)
            for queue in stale:
                self._clients.discard(queue)

    async def client_count(self) -> int:
        async with self._lock:
            return len(self._clients)

