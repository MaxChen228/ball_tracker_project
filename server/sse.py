from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator


# Internal pubsub for server-side state-change events. Current transport
# consumer: GET /stream formats each (event, data) tuple as SSE text for
# the dashboard (browser EventSource). Subscribers receive structured
# tuples; formatting belongs to the transport layer, not the hub. This
# keeps the hub a generic pubsub and lets non-SSE consumers (future WS
# bridges, test harness, etc.) iterate without parsing SSE strings.
class SSEHub:
    def __init__(self) -> None:
        self._clients: set[asyncio.Queue[tuple[str, dict]]] = set()
        self._lock = asyncio.Lock()

    async def subscribe(self) -> AsyncIterator[tuple[str, dict]]:
        queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue(maxsize=1000)
        async with self._lock:
            self._clients.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            async with self._lock:
                self._clients.discard(queue)

    async def broadcast(self, event: str, data: dict) -> None:
        async with self._lock:
            stale: list[asyncio.Queue[tuple[str, dict]]] = []
            for queue in self._clients:
                try:
                    queue.put_nowait((event, data))
                except asyncio.QueueFull:
                    stale.append(queue)
            for queue in stale:
                self._clients.discard(queue)


def format_sse(event: str, data: dict) -> str:
    """Render an (event, data) tuple as one SSE text frame (used by the
    /stream endpoint). Keep separators tight so the dashboard's bytes/sec
    stays predictable."""
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
