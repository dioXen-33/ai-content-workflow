"""Bus d'evenements en memoire, diffuse vers l'UI via Server-Sent Events."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from . import db


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, payload: dict[str, Any]) -> None:
        """Non bloquant : un abonne lent est ignore plutot que de freiner le pipeline."""
        dead = []
        for q in self._subscribers:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subscribers.discard(q)

    def emit(
        self,
        job_id: str | None,
        message: str,
        level: str = "info",
        video_id: str | None = None,
        **extra: Any,
    ) -> None:
        """Journalise en base ET diffuse a l'UI."""
        db.log(job_id, message, level=level, video_id=video_id)
        self.publish(
            {
                "type": "log",
                "job_id": job_id,
                "video_id": video_id,
                "level": level,
                "message": message,
                **extra,
            }
        )

    def state(self, job_id: str, video_id: str, state: str, **extra: Any) -> None:
        self.publish(
            {
                "type": "video_state",
                "job_id": job_id,
                "video_id": video_id,
                "state": state,
                **extra,
            }
        )

    def progress(self, job_id: str, **extra: Any) -> None:
        self.publish({"type": "progress", "job_id": job_id, **extra})


bus = EventBus()


def sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
