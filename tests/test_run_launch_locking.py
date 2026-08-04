"""Regression: launching a run must not deadlock on SQLite's writer lock.

The implement endpoint updates ``sessions.branch_name`` (FR-27) and then asks the
supervisor to insert a ``runs`` row. Those are two transactions, and SQLite
permits a single writer — holding the request's write lock across the supervisor
call produced ``sqlite3.OperationalError: database is locked`` and the run never
started. WAL (NFR-5) fixes reader/writer contention, not writer/writer.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from httpx import AsyncClient

from workflow_orchestrator.config import Settings
from workflow_orchestrator.models import Phase

from .conftest import worktree_for
from .test_api_approvals import advance_to


@pytest.fixture
def harness_stub(tmp_path: Path) -> Path:
    """A harness that exits immediately with a well-formed result event."""
    path = tmp_path / "claude-stub"
    payload = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "done",
            "total_cost_usd": 0.001,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    )
    path.write_text(
        "#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\n"
        f"print({json.dumps(payload)})\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


async def test_implement_run_launches_without_locking_the_database(
    client: AsyncClient, session: dict, repo: Path, settings: Settings, harness_stub: Path
) -> None:
    settings.WORKFLOW_CLAUDE_BIN = str(harness_stub)
    settings.DOCKER_IMAGE_AGENT = None
    settings.WORKFLOW_ALLOW_HOST_RUNNER = True

    await advance_to(client, session["id"], repo, Phase.IMPLEMENT)

    response = await client.post(f"/sessions/{session['id']}/implement/run")
    assert response.status_code == 200, response.text

    run = response.json()
    assert run["id"]
    assert run["harness"] == "claude_code"
    # FR-33 — the handle is persisted at launch, not after the first poll.
    assert run["container_id"]
    assert run["log_path"]

    # FR-27 — the branch update committed alongside.
    detail = (await client.get(f"/sessions/{session['id']}")).json()
    assert detail["branch_name"] == f"workflow/{session['id']}"

    # The run is visible through the API, so both writes landed.
    listed = (await client.get(f"/sessions/{session['id']}/runs")).json()
    assert [r["id"] for r in listed] == [run["id"]]


async def test_two_runs_can_be_launched_in_sequence(
    client: AsyncClient, session: dict, repo: Path, settings: Settings, harness_stub: Path
) -> None:
    """A second launch must not be blocked by the first run's bookkeeping."""
    settings.WORKFLOW_CLAUDE_BIN = str(harness_stub)
    settings.DOCKER_IMAGE_AGENT = None
    settings.WORKFLOW_ALLOW_HOST_RUNNER = True

    await advance_to(client, session["id"], repo, Phase.IMPLEMENT)

    first = await client.post(f"/sessions/{session['id']}/implement/run")
    second = await client.post(f"/sessions/{session['id']}/implement/run")
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["id"] != second.json()["id"]
