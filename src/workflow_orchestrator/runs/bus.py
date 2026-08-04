"""In-process pub/sub for live run output — SRS FR-20, NFR-8.

Each run has a topic. The supervisor publishes translated
:class:`~workflow_orchestrator.harness.base.RunEvent` objects; SSE responses
subscribe.

A bounded replay buffer is kept per run so a browser that connects late — or
reconnects after the server restarted — is handed what it missed before the live
feed starts, rather than joining mid-sentence. The authoritative record is always
``.workflow/run-<id>.log`` on disk; this buffer is a convenience.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from ..harness.base import RunEvent
from ..logging import get_logger

log = get_logger(__name__)

REPLAY_LIMIT = 2000
QUEUE_MAXSIZE = 1000


@dataclass
class Topic:
    replay: deque[RunEvent] = field(default_factory=lambda: deque(maxlen=REPLAY_LIMIT))
    subscribers: set[asyncio.Queue[RunEvent | None]] = field(default_factory=set)
    closed: bool = False


class EventBus:
    def __init__(self) -> None:
        self._topics: dict[str, Topic] = {}

    def topic(self, run_id: str) -> Topic:
        return self._topics.setdefault(run_id, Topic())

    def publish(self, run_id: str, event: RunEvent) -> None:
        topic = self.topic(run_id)
        topic.replay.append(event)
        for queue in list(topic.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A subscriber that cannot keep up is dropped rather than
                # allowed to stall the run.
                log.warning("bus.subscriber_dropped", run_id=run_id)
                topic.subscribers.discard(queue)

    def close(self, run_id: str) -> None:
        topic = self.topic(run_id)
        topic.closed = True
        for queue in list(topic.subscribers):
            try:
                queue.put_nowait(None)  # sentinel: stream complete
            except asyncio.QueueFull:  # pragma: no cover
                pass

    def reset(self, run_id: str) -> None:
        """Clear a topic before a reattach replays the persisted log."""
        self._topics[run_id] = Topic()

    def is_closed(self, run_id: str) -> bool:
        return self.topic(run_id).closed

    async def subscribe(
        self, run_id: str, *, replay: bool = True
    ) -> AsyncIterator[RunEvent]:
        topic = self.topic(run_id)
        queue: asyncio.Queue[RunEvent | None] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)

        # Snapshot the replay buffer before subscribing so no event is missed or
        # duplicated in the gap between the two.
        backlog = list(topic.replay) if replay else []
        topic.subscribers.add(queue)

        try:
            for event in backlog:
                yield event
            if topic.closed and queue.empty():
                return
            while True:
                event = await queue.get()
                if event is None:
                    return
                yield event
        finally:
            topic.subscribers.discard(queue)


_bus: EventBus | None = None


def get_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus


def reset_bus() -> None:
    """Test hook."""
    global _bus
    _bus = None
