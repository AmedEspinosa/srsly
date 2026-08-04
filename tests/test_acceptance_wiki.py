"""AC-6 — concurrent post-merge write-backs are serialised.

    "Trigger two concurrent post-merge write-backs (simulate via test harness).
     Expected: wiki_writes table shows sequential written_at timestamps with no
     overlap. index.md is not corrupted."

The wiki agent itself is replaced with a fake that performs a deliberately
interleaving read-modify-write on ``index.md``. Without the FR-42 queue that
pattern loses one of the two appends; with it, both survive.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select

from workflow_orchestrator.config import Settings
from workflow_orchestrator.db import session_scope
from workflow_orchestrator.librarian import writeback
from workflow_orchestrator.librarian.layout import WIKI_ROOT, layout_for
from workflow_orchestrator.librarian.queue import get_queue
from workflow_orchestrator.librarian.scaffold import init_wiki_repo
from workflow_orchestrator.models import Harness, Phase, Project, Session, WikiWrite

pytestmark = pytest.mark.acceptance


@pytest.fixture
async def two_sessions(client, project: dict, repo: Path, wiki_repo: Path):
    """Two merged-ready sessions sharing one wiki repo."""
    init_wiki_repo(wiki_repo)

    ids: list[str] = []
    for i in range(2):
        response = await client.post(
            f"/projects/{project['id']}/sessions",
            json={"feature_prompt": f"feature {i}"},
        )
        session_id = response.json()["id"]
        srs = repo / "worktrees" / session_id / ".workflow" / "srs.md"
        srs.parent.mkdir(parents=True, exist_ok=True)
        srs.write_text(f"# SRS {i}\n\nSomething durable.\n", encoding="utf-8")
        ids.append(session_id)
    return project["id"], ids


async def test_ac6_concurrent_writebacks_do_not_interleave(
    settings: Settings, two_sessions, wiki_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, session_ids = two_sessions
    layout = layout_for(wiki_repo)

    async def fake_ingest(settings_, project_, source: Path, policy_) -> list[str]:
        """Stand-in for the wiki agent.

        Reads index.md, yields to the loop, then writes back. Two of these
        running concurrently would lose an append; serialised, both land.
        """
        name = source.parent.parent.name  # the session id
        page = f"{WIKI_ROOT}/concepts/{name}.md"

        current = layout.index_md.read_text(encoding="utf-8")
        await asyncio.sleep(0.05)  # the interleaving window
        layout.index_md.write_text(current + f"\n- [[{name}]]\n", encoding="utf-8")

        target = layout.resolve(page)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {name}\n\nIngested.\n", encoding="utf-8")
        return [page]

    monkeypatch.setattr(writeback, "_ingest_one", fake_ingest)
    # Lint would shell out to the wiki agent; not what this criterion is about.
    monkeypatch.setattr(
        "workflow_orchestrator.librarian.lint.run_lint",
        lambda *a, **k: asyncio.sleep(0, result=None),
    )
    # As would the as-built run, which is a harness invocation of its own.
    monkeypatch.setattr(
        writeback, "generate_as_built", lambda *a, **k: asyncio.sleep(0, result=None)
    )

    queue = get_queue()
    queue.start()

    # Two write-backs triggered at the same moment (FR-42 / AC-6).
    await asyncio.gather(
        writeback.enqueue_post_merge(settings, project_id, session_ids[0]),
        writeback.enqueue_post_merge(settings, project_id, session_ids[1]),
    )
    await queue.drain()

    # index.md is not corrupted: both appends survived.
    index_text = layout.index_md.read_text(encoding="utf-8")
    for session_id in session_ids:
        assert f"[[{session_id}]]" in index_text, "an index.md append was lost"

    # Both pages were written.
    for session_id in session_ids:
        assert layout.resolve(f"{WIKI_ROOT}/concepts/{session_id}.md").exists()

    # wiki_writes shows sequential, non-overlapping writes.
    async with session_scope() as db:
        rows = (
            await db.execute(select(WikiWrite).order_by(WikiWrite.written_at))
        ).scalars().all()
        recorded = [(r.page_path, r.written_at, r.needs_review) for r in rows]

    assert len(recorded) == 2
    timestamps = [t for _, t, _ in recorded]
    assert timestamps == sorted(timestamps), "writes were not recorded in order"
    # FR-39 — every automated write is flagged for review.
    assert all(flag == 1 for _, _, flag in recorded)

    # The queue executed them one at a time, in submission order.
    assert queue.completed == [
        f"post-merge:{session_ids[0]}",
        f"post-merge:{session_ids[1]}",
    ]

    await queue.stop()


async def test_writeback_skips_an_uninitialized_wiki(
    settings: Settings, client, project: dict, repo: Path
) -> None:
    """A project whose wiki repo was never scaffolded must not crash the merge."""
    response = await client.post(
        f"/projects/{project['id']}/sessions", json={"feature_prompt": "x"}
    )
    session_id = response.json()["id"]

    written = await writeback.run_post_merge(settings, project["id"], session_id)
    assert written == []


async def test_only_the_srs_is_ingested(
    settings: Settings, two_sessions, wiki_repo: Path, repo: Path,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-38 / §2.11 — plan.md, review.md and run logs are never ingested."""
    project_id, session_ids = two_sessions
    session_id = session_ids[0]

    workflow_dir = repo / "worktrees" / session_id / ".workflow"
    (workflow_dir / "plan.md").write_text("# Plan\n", encoding="utf-8")
    (workflow_dir / "review.md").write_text("# Review\n", encoding="utf-8")
    (workflow_dir / "run-abc.log").write_text("noise\n", encoding="utf-8")

    ingested: list[str] = []

    async def record(settings_, project_, source: Path, policy_) -> list[str]:
        ingested.append(source.name)
        return []

    monkeypatch.setattr(writeback, "_ingest_one", record)
    monkeypatch.setattr(
        "workflow_orchestrator.librarian.lint.run_lint",
        lambda *a, **k: asyncio.sleep(0, result=None),
    )
    monkeypatch.setattr(
        writeback, "generate_as_built", lambda *a, **k: asyncio.sleep(0, result=None)
    )

    await writeback.run_post_merge(settings, project_id, session_id)
    assert ingested == ["srs.md"]
