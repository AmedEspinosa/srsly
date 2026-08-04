"""Byte-stream to line assembly for harness output.

Both harness CLIs emit JSONL, and both routinely emit *long* lines: the streams
echo whole tool payloads inline, so a single ``Read`` of a moderately large
source file becomes one JSON object far past any convenient buffer size.

That rules out ``StreamReader.readline()``, which raises once a line exceeds the
reader's ``limit`` (64 KiB by default) — the failure that killed the first plan
run against a real repository. Reading fixed-size chunks and splitting on
newlines here sidesteps the limit entirely, because ``StreamReader.read(n)``
does not enforce it.

The same logic drives the supervisor's log-file tail, so it lives in one place
rather than being written twice with two sets of edge cases.
"""

from __future__ import annotations

from collections.abc import Iterator

from ..logging import get_logger

log = get_logger(__name__)

#: Beyond this, a "line" is assumed to be corruption rather than a real event.
#: Generous on purpose: real payloads reach into the megabytes, and the cost of
#: guessing low is silently dropping legitimate output.
MAX_LINE_BYTES = 32 * 1024 * 1024


class LineBuffer:
    """Accumulates bytes and yields complete decoded lines.

    Not thread-safe and not reusable across streams — one per stream.
    """

    def __init__(self, *, max_line_bytes: int = MAX_LINE_BYTES) -> None:
        self._pending = b""
        self._max = max_line_bytes
        #: Set while discarding an over-long line, so its remaining chunks are
        #: dropped too instead of being emitted as a spurious fragment.
        self._discarding = False

    def feed(self, chunk: bytes) -> Iterator[str]:
        """Append ``chunk`` and yield every line it completed."""
        if not chunk:
            return

        self._pending += chunk
        *complete, self._pending = self._pending.split(b"\n")

        for raw in complete:
            if self._discarding:
                # This is the tail of a line already reported as too long.
                self._discarding = False
                continue
            if len(raw) > self._max:
                self._too_long(len(raw))
                continue
            yield raw.decode("utf-8", errors="replace")

        # A partial line that has already blown the cap will never be usable,
        # so drop it now rather than growing the buffer without bound.
        if len(self._pending) > self._max:
            self._too_long(len(self._pending))
            self._pending = b""
            self._discarding = True

    def flush(self) -> str | None:
        """The trailing fragment at end of stream, if any.

        A CLI that omits the final newline would otherwise lose its last event.
        """
        if not self._pending or self._discarding:
            self._pending = b""
            return None
        raw, self._pending = self._pending, b""
        return raw.decode("utf-8", errors="replace")

    def _too_long(self, size: int) -> None:
        # Dropped, not raised: the adapters' ``parse_line`` already returns None
        # for anything unparseable, so losing one event degrades gracefully —
        # aborting a multi-minute run over it does not.
        log.warning("harness.line_too_long", bytes=size, cap=self._max)
