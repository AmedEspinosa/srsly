"""Detached run supervision — SRS §2.9, FR-31..FR-34, NFR-1, NFR-4."""

from .base import LaunchSpec, RunHandle, RunnerBackend, RunState

__all__ = ["LaunchSpec", "RunHandle", "RunState", "RunnerBackend"]
