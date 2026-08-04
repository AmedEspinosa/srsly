"""Adapter lookup.

This is the single place that maps the ``Harness`` enum to a concrete backend.
Everything above the ``harness`` package asks for an adapter by enum value and
never names a backend (AC-9).
"""

from __future__ import annotations

from ..config import Settings
from ..models import Harness
from .base import HarnessAdapter
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter

_ADAPTERS = {
    Harness.CLAUDE_CODE: ClaudeCodeAdapter,
    Harness.CODEX: CodexAdapter,
}

#: FR-8 — "The default pairing MUST be: implement with Claude Code, review with
#: Codex." The pairing names backends, so it lives here rather than in the API
#: schema layer (AC-9).
DEFAULT_IMPLEMENT_HARNESS = Harness.CLAUDE_CODE
DEFAULT_REVIEW_HARNESS = Harness.CODEX


def get_adapter(harness: Harness | str, settings: Settings) -> HarnessAdapter:
    key = Harness(harness) if not isinstance(harness, Harness) else harness
    try:
        factory = _ADAPTERS[key]
    except KeyError:  # pragma: no cover - the enum makes this unreachable
        raise ValueError(f"no adapter registered for harness {key!r}") from None
    return factory(settings)  # type: ignore[return-value]


def available_harnesses() -> list[Harness]:
    return list(_ADAPTERS)
