"""ASGI entrypoint for uvicorn (``workflow_orchestrator.asgi:app``)."""

from __future__ import annotations

from .app import create_app

app = create_app()
