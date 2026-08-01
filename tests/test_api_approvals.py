"""Approval gate and immutability — FR-13, FR-14, FR-17, FR-22, FR-26, FR-44, FR-45."""

from __future__ import annotations

from pathlib import Path

from httpx import AsyncClient

from workflow_orchestrator.api import sessions as sessions_api
from workflow_orchestrator.phases import PHASE_ARTIFACT
from workflow_orchestrator.models import Phase

from .conftest import write_artifact


async def advance_to(
    client: AsyncClient, session_id: str, repo: Path, target: Phase
) -> None:
    """Write each phase's artifact and approve it until ``target`` is current."""
    for phase in (Phase.SRS, Phase.PLAN, Phase.IMPLEMENT, Phase.REVIEW, Phase.MERGE):
        current = (await client.get(f"/sessions/{session_id}")).json()["current_phase"]
        if current == target.value:
            return
        write_artifact(repo, session_id, Path(PHASE_ARTIFACT[phase]).name)
        response = await client.post(
            f"/sessions/{session_id}/approve", json={"phase": phase.value}
        )
        assert response.status_code == 200, response.text


async def test_approving_srs_advances_to_plan(
    client: AsyncClient, session: dict, repo: Path
) -> None:
    write_artifact(repo, session["id"], "srs.md")
    response = await client.post(
        f"/sessions/{session['id']}/approve",
        json={"phase": "srs", "notes": "looks right"},
    )
    assert response.status_code == 200
    approval = response.json()
    assert approval["phase"] == "srs"
    assert approval["approved_by"] == "user"
    assert approval["approved_at"].endswith("Z")  # §5.2 ISO-8601 UTC
    assert approval["notes"] == "looks right"

    detail = (await client.get(f"/sessions/{session['id']}")).json()
    assert detail["current_phase"] == "plan"


async def test_phase_transition_blocked_without_approval(
    client: AsyncClient, session: dict, repo: Path
) -> None:
    """AC-2 — approving ``plan`` before ``srs`` is 422 phase_not_ready."""
    write_artifact(repo, session["id"], "srs.md")
    write_artifact(repo, session["id"], "plan.md")

    response = await client.post(
        f"/sessions/{session['id']}/approve", json={"phase": "plan"}
    )
    assert response.status_code == 422
    assert response.json() == {"error": "phase_not_ready", "required_approval": "srs"}

    # The session must not have moved.
    detail = (await client.get(f"/sessions/{session['id']}")).json()
    assert detail["current_phase"] == "srs"
    assert detail["approvals"] == []


async def test_session_stays_in_qa_until_srs_md_is_written(
    client: AsyncClient, session: dict, repo: Path
) -> None:
    """FR-12 — the QA loop completes by producing ``srs.md``, not by approval."""
    assert (await client.get(f"/sessions/{session['id']}")).json()["current_phase"] == "qa"

    # Approving ``srs`` while still in QA is a transition error, not a gate error:
    # the session genuinely has not left the entry phase yet.
    response = await client.post(
        f"/sessions/{session['id']}/approve", json={"phase": "srs"}
    )
    assert response.status_code == 422
    assert response.json() == {
        "error": "invalid_transition",
        "current_phase": "qa",
        "requested_phase": "srs",
    }

    write_artifact(repo, session["id"], "srs.md")
    assert (await client.get(f"/sessions/{session['id']}")).json()["current_phase"] == "srs"


async def test_approval_requires_the_artifact_to_exist(
    client: AsyncClient, session: dict, repo: Path
) -> None:
    """Approving the current phase without its artifact is 422 artifact_missing."""
    write_artifact(repo, session["id"], "srs.md")
    await client.post(f"/sessions/{session['id']}/approve", json={"phase": "srs"})

    # Now at ``plan``, but no plan.md has been produced.
    response = await client.post(
        f"/sessions/{session['id']}/approve", json={"phase": "plan"}
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "artifact_missing"
    assert body["artifact_path"].endswith(".workflow/plan.md")


async def test_rejection_returns_to_the_previous_phase(
    client: AsyncClient, session: dict, repo: Path
) -> None:
    """FR-13 — rejecting the SRS returns to QA."""
    write_artifact(repo, session["id"], "srs.md")
    await client.post(f"/sessions/{session['id']}/approve", json={"phase": "srs"})
    assert (await client.get(f"/sessions/{session['id']}")).json()["current_phase"] == "plan"

    response = await client.post(
        f"/sessions/{session['id']}/reject", json={"notes": "plan is wrong"}
    )
    assert response.status_code == 200
    assert response.json()["current_phase"] == "srs"


async def test_walking_every_gate_completes_the_session(
    client: AsyncClient, session: dict, repo: Path
) -> None:
    """FR-5 / FR-30 — the full ordered progression ends at ``completed``."""
    await advance_to(client, session["id"], repo, Phase.COMPLETED)

    detail = (await client.get(f"/sessions/{session['id']}")).json()
    assert detail["current_phase"] == "completed"
    assert detail["completed_at"] is not None
    assert [a["phase"] for a in detail["approvals"]] == [
        "srs",
        "plan",
        "implement",
        "review",
        "merge",
    ]


async def test_approvals_have_no_update_or_delete_route() -> None:
    """FR-45 — the approvals table is append-only from the app's perspective."""
    offending = []
    for route in sessions_api.router.routes:
        methods = getattr(route, "methods", set())
        path = getattr(route, "path", "")
        if "approval" not in path:
            continue
        if methods & {"PUT", "PATCH", "DELETE"}:
            offending.append((sorted(methods), path))
    assert offending == []


async def test_approval_timestamps_are_stable_across_reads(
    client: AsyncClient, session: dict, repo: Path
) -> None:
    write_artifact(repo, session["id"], "srs.md")
    created = (
        await client.post(f"/sessions/{session['id']}/approve", json={"phase": "srs"})
    ).json()

    listed = (await client.get(f"/sessions/{session['id']}/approvals")).json()
    assert listed[0]["approved_at"] == created["approved_at"]
    assert listed[0]["id"] == created["id"]


async def test_artifact_endpoint_reports_missing_files(
    client: AsyncClient, session: dict, repo: Path
) -> None:
    missing = (await client.get(f"/sessions/{session['id']}/artifacts/plan")).json()
    assert missing["exists"] is False

    write_artifact(repo, session["id"], "plan.md", "# The Plan\n\nstep one\n")
    present = (await client.get(f"/sessions/{session['id']}/artifacts/plan")).json()
    assert present["exists"] is True
    assert "step one" in present["content"]
