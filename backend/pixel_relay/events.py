from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(slots=True)
class Event:
    id: int
    kind: str
    data: dict[str, Any]
    created_at: str

    def encode(self) -> str:
        payload = json.dumps(
            {"kind": self.kind, "data": self.data, "created_at": self.created_at},
            separators=(",", ":"),
        )
        return f"id: {self.id}\nevent: {self.kind}\ndata: {payload}\n\n"


class EventBroker:
    def __init__(self, history_size: int = 500):
        self._history: deque[Event] = deque(maxlen=history_size)
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._next_id = 1
        self._lock = asyncio.Lock()
        self._shutdown_event: Event | None = None

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown_event is not None

    def request_shutdown(self, action: str = "shutdown") -> Event:
        """Wake every SSE subscriber before the HTTP server starts draining."""
        if self._shutdown_event is not None:
            return self._shutdown_event
        event = Event(
            id=self._next_id,
            kind="server",
            data={"action": action},
            created_at=datetime.now(UTC).isoformat(),
        )
        self._next_id += 1
        self._history.append(event)
        self._shutdown_event = event
        for queue in tuple(self._subscribers):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(event)
        return event

    async def publish(self, kind: str, data: dict[str, Any]) -> Event:
        async with self._lock:
            event = Event(
                id=self._next_id,
                kind=kind,
                data=data,
                created_at=datetime.now(UTC).isoformat(),
            )
            self._next_id += 1
            self._history.append(event)
            for queue in tuple(self._subscribers):
                if queue.full():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
                queue.put_nowait(event)
            return event

    async def subscribe(self, after_id: int = 0):
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=100)
        async with self._lock:
            backlog = [event for event in self._history if event.id > after_id]
            self._subscribers.add(queue)
        try:
            for event in backlog:
                yield event
                if event is self._shutdown_event:
                    return
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=20)
                    yield event
                    if event is self._shutdown_event:
                        return
                except TimeoutError:
                    yield None
        finally:
            async with self._lock:
                self._subscribers.discard(queue)


import contextlib  # noqa: E402
