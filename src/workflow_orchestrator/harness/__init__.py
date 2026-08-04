"""Harness adapters.

Everything backend-specific about Claude Code and Codex lives in this package.
Callers work through :class:`~workflow_orchestrator.harness.base.HarnessAdapter`
and :func:`~workflow_orchestrator.harness.registry.get_adapter`; nothing above
this package branches on harness identity (AC-9).
"""

from .base import CommandSpec, HarnessAdapter, HarnessOperation, RunEvent
from .registry import get_adapter

__all__ = [
    "CommandSpec",
    "HarnessAdapter",
    "HarnessOperation",
    "RunEvent",
    "get_adapter",
]
