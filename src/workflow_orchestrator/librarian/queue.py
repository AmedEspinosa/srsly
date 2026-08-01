"""Serialized wiki write queue — SRS FR-42, AC-6.

    "All wiki writes (index.md, log.md, page files) MUST be serialised through a
     single async queue. Concurrent writes MUST NOT be permitted."

Every mutation of the wiki repository goes through :func:`submit`. A single
consumer coroutine drains the queue, so two post-merge write-backs triggered at
the same moment execute strictly one after the other and cannot interleave their
edits to ``index.md`` or ``log.md``.

``submit`` returns a future that resolves when *that* task has finished, so
callers can await their own write without being able to run it concurrently with
anyone else's.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..logging import get_logger

log = get_logger(__name__)


@dataclass
class WikiTask:
    name: str
    run: Callable[[], Awaitable[Any]]
    future: asyncio.Future[Any] = field(repr=False, default=None)  # type: ignore[assignment]


class WikiWriteQueue:
    """Single-consumer queue guaranteeing serialised wiki writes."""

    def __init__(self) -> None:
        # Created in start(), not here: an asyncio.Queue binds to the loop that
        # constructs it, and this object is a process-wide singleton that may
        # outlive any single loop (notably across tests).
        self._queue: asyncio.Queue[WikiTask | None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._consumer: asyncio.Task[None] | None = None
        self._running = False
        #: Tasks that have executed, in order. Used to assert serialisation.
        self.completed: list[str] = []

    @property
    def is_running(self) -> bool:
        if not self._running or self._consumer is None or self._consumer.done():
            return False
        # A queue bound to a dead loop is not usable.
        try:
            return self._loop is asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no loop running
            return False

    def _ensure_queue(self) -> asyncio.Queue[WikiTask | None]:
        loop = asyncio.get_running_loop()
        if self._queue is None or self._loop is not loop:
            self._queue = asyncio.Queue()
            self._loop = loop
        return self._queue

    def start(self) -> None:
        if self.is_running:
            return
        self._ensure_queue()
        self._running = True
        self._consumer = asyncio.create_task(self._consume())
        log.info("wiki.queue_started")

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._queue is not None:
            await self._queue.put(None)  # sentinel
        if self._consumer is not None:
            try:
                await asyncio.wait_for(self._consumer, timeout=30)
            except (TimeoutError, asyncio.CancelledError):  # pragma: no cover
                self._consumer.cancel()
        self._consumer = None
        log.info("wiki.queue_stopped")

    async def _consume(self) -> None:
        queue = self._ensure_queue()
        while True:
            task = await queue.get()
            if task is None:
                queue.task_done()
                return
            try:
                result = await task.run()
                self.completed.append(task.name)
                if not task.future.done():
                    task.future.set_result(result)
            except Exception as exc:
                log.error("wiki.task_failed", task=task.name, error=str(exc))
                if not task.future.done():
                    task.future.set_exception(exc)
            finally:
                queue.task_done()

    def submit(self, name: str, run: Callable[[], Awaitable[Any]]) -> asyncio.Future[Any]:
        """Enqueue a wiki mutation. Returns a future for *this* task's result."""
        if not self.is_running:
            self.start()
        queue = self._ensure_queue()
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        queue.put_nowait(WikiTask(name=name, run=run, future=future))
        log.info("wiki.task_queued", task=name, depth=queue.qsize())
        return future

    async def drain(self) -> None:
        """Wait for everything currently queued to finish."""
        if self._queue is not None:
            await self._queue.join()

    @property
    def depth(self) -> int:
        return self._queue.qsize() if self._queue is not None else 0


_queue: WikiWriteQueue | None = None


def get_queue() -> WikiWriteQueue:
    global _queue
    if _queue is None:
        _queue = WikiWriteQueue()
    return _queue


def reset_queue() -> None:
    """Test hook."""
    global _queue
    _queue = None
