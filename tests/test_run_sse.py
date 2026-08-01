"""SSE run streaming — FR-20, NFR-8."""

from __future__ import annotations

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient

from workflow_orchestrator.app import create_app
from workflow_orchestrator.config import Settings
from workflow_orchestrator.db import dispose_engine
from workflow_orchestrator.harness.base import RunEvent
from workflow_orchestrator.models import Harness, Phase, RunStatus
from workflow_orchestrator.runs.bus import get_bus, reset_bus
from workflow_orchestrator.runs.supervisor import reset_supervisor


@pytest.fixture(autouse=True)
def _isolated_globals():
    reset_bus()
    reset_supervisor()
    yield
    reset_bus()
    reset_supervisor()


async def parse_sse(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    name = None
    for line in text.splitlines():
        if line.startswith("event:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and name:
            payload = line.split(":", 1)[1].strip()
            try:
                events.append((name, json.loads(payload)))
            except json.JSONDecodeError:
                pass
    return events


async def test_stream_replays_buffered_events_then_closes(
    settings: Settings, repo, wiki_repo
) -> None:
    """A client connecting after the run started still sees the whole run."""
    app = create_app(settings, create_schema=True)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            project = (
                await client.post(
                    "/projects",
                    json={
                        "name": "sse",
                        "repo_path": str(repo),
                        "wiki_repo_path": str(wiki_repo),
                        "wiki_super_summary_path": "llm-wiki/super-summaries/sse.md",
                    },
                )
            ).json()
            session = (
                await client.post(
                    f"/projects/{project['id']}/sessions",
                    json={"feature_prompt": "stream me"},
                )
            ).json()

            # Create a run row directly; the supervisor path is covered elsewhere.
            from workflow_orchestrator.db import session_scope
            from workflow_orchestrator.models import Run

            async with session_scope() as db:
                run = Run(
                    session_id=session["id"],
                    phase=Phase.IMPLEMENT.value,
                    harness=Harness.CLAUDE_CODE.value,
                    status=RunStatus.RUNNING.value,
                    container_id="host:1:x",
                )
                db.add(run)
                await db.flush()
                run_id = run.id

            bus = get_bus()
            bus.publish(run_id, RunEvent(event_type="text_delta", text="hello "))
            bus.publish(run_id, RunEvent(event_type="text_delta", text="world"))
            bus.publish(
                run_id,
                RunEvent(event_type="result", text="done", token_count=42, cost_usd=0.05),
            )
            bus.close(run_id)

            response = await client.get(
                f"/sessions/{session['id']}/runs/{run_id}/stream"
            )
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")

            events = await parse_sse(response.text)
            run_events = [payload for name, payload in events if name == "run"]
            assert [e["text"] for e in run_events] == ["hello ", "world", "done"]
            assert run_events[-1]["token_count"] == 42
            assert run_events[-1]["cost_usd"] == 0.05
            # Timestamps are ISO-8601 UTC for the UI.
            assert run_events[0]["timestamp"].endswith("Z")

            assert [name for name, _ in events][-1] == "done"

    await dispose_engine()


async def test_stream_404s_for_a_run_in_another_session(
    settings: Settings, repo, wiki_repo
) -> None:
    app = create_app(settings, create_schema=True)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            project = (
                await client.post(
                    "/projects",
                    json={
                        "name": "sse2",
                        "repo_path": str(repo),
                        "wiki_repo_path": str(wiki_repo),
                        "wiki_super_summary_path": "llm-wiki/super-summaries/x.md",
                    },
                )
            ).json()
            session = (
                await client.post(
                    f"/projects/{project['id']}/sessions",
                    json={"feature_prompt": "x"},
                )
            ).json()
            response = await client.get(
                f"/sessions/{session['id']}/runs/does-not-exist/stream"
            )
            assert response.status_code == 404

    await dispose_engine()


async def test_bus_replay_is_bounded_and_ordered() -> None:
    from workflow_orchestrator.runs.bus import REPLAY_LIMIT, EventBus

    bus = EventBus()
    for i in range(REPLAY_LIMIT + 50):
        bus.publish("r", RunEvent(event_type="text_delta", text=str(i)))
    bus.close("r")

    seen = [event.text async for event in bus.subscribe("r")]
    assert len(seen) == REPLAY_LIMIT
    assert seen[-1] == str(REPLAY_LIMIT + 49)  # newest retained
    assert seen == sorted(seen, key=int)  # order preserved


async def test_late_subscriber_receives_live_events() -> None:
    from workflow_orchestrator.runs.bus import EventBus

    bus = EventBus()
    bus.publish("r", RunEvent(event_type="text_delta", text="early"))

    received: list[str] = []

    async def consume() -> None:
        async for event in bus.subscribe("r"):
            received.append(event.text)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    bus.publish("r", RunEvent(event_type="text_delta", text="late"))
    await asyncio.sleep(0.05)
    bus.close("r")
    await asyncio.wait_for(task, timeout=5)

    assert received == ["early", "late"]
