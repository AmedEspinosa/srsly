"""The post-merge as-built record.

The wiki ingested ``srs.md`` and nothing else about what shipped, so it recorded
a *proposal* as though it were a fact. Session 3f1a2879's own review said the
plan "explicitly drops AC-13" and left NFR-7 unverified; neither statement ever
reached the wiki, so the next session would be told provider fallback exists.

``review.md`` cannot fill that gap — it is written before the fix runs it
triggers, so ingesting it asserts bugs that were already repaired. The as-built
record is generated after the pull request merges, which is the only point at
which it can be true: fixes land during PR review, maintainers amend, and a
squash rewrites the branch outright.
"""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path

import pytest

from workflow_orchestrator.config import Settings
from workflow_orchestrator.harness.base import HarnessOperation
from workflow_orchestrator.harness.claude_code import ClaudeCodeAdapter
from workflow_orchestrator.harness.codex import CodexAdapter
from workflow_orchestrator.harness.prompts import as_built_prompt
from workflow_orchestrator.harness.runner import SourceModified, run_as_built_operation
from workflow_orchestrator.librarian import writeback
from workflow_orchestrator.librarian.scaffold import init_wiki_repo
from workflow_orchestrator.services import as_built as as_built_service
from workflow_orchestrator.services import review as review_service

from .conftest import git


# --- the operation is read-only (FR-16) ----------------------------------------


def test_as_built_is_read_only() -> None:
    assert HarnessOperation.AS_BUILT.read_only
    assert HarnessOperation.PLAN.read_only
    assert HarnessOperation.REVIEW.read_only
    assert not HarnessOperation.IMPLEMENT.read_only


@pytest.mark.parametrize("adapter_cls", [ClaudeCodeAdapter, CodexAdapter])
def test_both_adapters_build_a_read_only_spec(
    adapter_cls, settings: Settings, repo: Path
) -> None:
    """Whichever backend runs it, reconciling the record must not edit the code."""
    spec = adapter_cls(settings).command(
        HarnessOperation.AS_BUILT, worktree=repo, prompt="reconcile", output_file=None
    )
    assert spec.read_only is True


def test_artifact_name_comes_from_the_operation() -> None:
    """The enum value is the artifact stem, so the two cannot drift apart."""
    assert HarnessOperation.AS_BUILT.value == "as-built"


# --- the prompt ----------------------------------------------------------------


def test_prompt_survives_a_missing_merged_diff() -> None:
    """``gh`` may be absent or the PR number unknown; the record still runs."""
    prompt = as_built_prompt("srs.md", "plan.md", "review.md", triage="t.json")
    assert "Merged change" not in prompt
    assert "srs.md" in prompt


def test_prompt_includes_the_merged_diff_when_available() -> None:
    prompt = as_built_prompt(
        "srs.md", "plan.md", "review.md", triage="t.json", merged_diff="merged.diff"
    )
    assert "merged.diff" in prompt


def test_prompt_tells_the_harness_not_to_trust_the_review() -> None:
    """The stale-review trap is the whole reason review.md is not ingested."""
    prompt = as_built_prompt("srs.md", "plan.md", "review.md", triage="t.json")
    assert "written before the fixes" in prompt
    assert "dropped" in prompt


# --- run_as_built_operation ----------------------------------------------------


def make_stub(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


AS_BUILT_DOC = "# As-built\n\nAC-13 was dropped.\n"


@pytest.fixture
def as_built_stub(tmp_path: Path) -> Path:
    payload = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": AS_BUILT_DOC,
            "total_cost_usd": 0.2,
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
    )
    body = f"import sys, json\nsys.stdin.read()\nprint({payload!r})\n"
    return make_stub(tmp_path / "claude-asbuilt", body)


@pytest.fixture
def worktree(repo: Path) -> Path:
    target = repo / "worktrees" / "sess"
    git("worktree", "add", "--detach", str(target), "HEAD", cwd=repo)
    workflow = target / ".workflow"
    workflow.mkdir(exist_ok=True)
    for name in ("srs.md", "plan.md", "review.md"):
        (workflow / name).write_text(f"# {name}\n", encoding="utf-8")
    return target


async def test_writes_the_as_built_artifact(
    settings: Settings, as_built_stub: Path, worktree: Path
) -> None:
    settings.WORKFLOW_CLAUDE_BIN = str(as_built_stub)
    adapter = ClaudeCodeAdapter(settings)

    artifact = await run_as_built_operation(
        adapter,
        worktree=worktree,
        context_files=[worktree / ".workflow" / name for name in ("srs.md", "plan.md", "review.md")],
    )

    assert artifact == worktree / ".workflow" / "as-built.md"
    assert "AC-13 was dropped" in artifact.read_text(encoding="utf-8")


async def test_a_harness_that_edits_source_is_rejected(
    settings: Settings, tmp_path: Path, worktree: Path
) -> None:
    """FR-16 — reconciling the record must not quietly edit what it records."""
    body = (
        "import sys, json, pathlib\n"
        "sys.stdin.read()\n"
        "pathlib.Path('README.md').write_text('tampered\\n')\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,\n"
        "  'result': '# As-built', 'total_cost_usd': 0.1,\n"
        "  'usage': {'input_tokens': 1, 'output_tokens': 1}}))\n"
    )
    settings.WORKFLOW_CLAUDE_BIN = str(make_stub(tmp_path / "claude-tamper", body))

    with pytest.raises(SourceModified) as excinfo:
        await run_as_built_operation(
            ClaudeCodeAdapter(settings), worktree=worktree, context_files=[]
        )
    assert "README.md" in str(excinfo.value)


# --- the selection table -------------------------------------------------------


def test_as_built_is_ingested() -> None:
    assert writeback.should_ingest(".workflow/as-built.md")


def test_as_built_follows_the_srs_so_it_corrects_the_record() -> None:
    order = [p.relative for p in writeback.SELECTION_TABLE if p.ingest]
    assert order == [".workflow/srs.md", ".workflow/as-built.md"]


def test_review_and_plan_are_still_excluded() -> None:
    """review.md is a pre-fix defect list; as-built supersedes it."""
    assert not writeback.should_ingest(".workflow/review.md")
    assert not writeback.should_ingest(".workflow/plan.md")
    assert not writeback.should_ingest(".workflow/merged.diff")


def test_each_ingested_artifact_is_framed_for_what_it_is() -> None:
    """An as-built record read as a specification has its deviations extracted
    as though they were requirements — the exact inversion we are fixing."""
    by_path = {p.relative: p for p in writeback.SELECTION_TABLE}
    assert by_path[".workflow/srs.md"].framing == writeback.SPEC_FRAMING
    assert by_path[".workflow/as-built.md"].framing == writeback.AS_BUILT_FRAMING
    assert "this document wins" in writeback.AS_BUILT_FRAMING.lower()


# --- generation is wired into the post-merge write-back ------------------------


@pytest.fixture
def stub_lint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "workflow_orchestrator.librarian.lint.run_lint",
        lambda *a, **k: asyncio.sleep(0, result=None),
    )


async def test_as_built_is_generated_before_the_ingest_decision(
    client, session: dict, repo: Path, settings: Settings, wiki_repo: Path,
    stub_lint, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generated first, or the selection table would miss it until next time."""
    init_wiki_repo(wiki_repo)
    session_id = session["id"]
    workflow_dir = repo / "worktrees" / session_id / ".workflow"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "srs.md").write_text("# SRS\n", encoding="utf-8")

    async def fake_generate(settings_, session_, worktree_: Path):
        target = worktree_ / ".workflow" / "as-built.md"
        target.write_text("# As-built\n", encoding="utf-8")
        return target

    ingested: list[str] = []

    async def record(settings_, project_, source: Path, policy_) -> list[str]:
        ingested.append(source.name)
        return []

    monkeypatch.setattr(writeback, "generate_as_built", fake_generate)
    monkeypatch.setattr(writeback, "_ingest_one", record)

    await writeback.run_post_merge(settings, session["project_id"], session_id)
    assert ingested == ["srs.md", "as-built.md"]


async def test_a_failed_as_built_still_ingests_the_specification(
    client, session: dict, repo: Path, settings: Settings, wiki_repo: Path,
    stub_lint, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Losing the write-back entirely would make this feature a regression."""
    init_wiki_repo(wiki_repo)
    session_id = session["id"]
    workflow_dir = repo / "worktrees" / session_id / ".workflow"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "srs.md").write_text("# SRS\n", encoding="utf-8")

    ingested: list[str] = []

    async def record(settings_, project_, source: Path, policy_) -> list[str]:
        ingested.append(source.name)
        return []

    # The real generator, with a harness binary that does not exist.
    settings.WORKFLOW_CODEX_BIN = "/nonexistent/codex"
    monkeypatch.setattr(writeback, "_ingest_one", record)

    await writeback.run_post_merge(settings, session["project_id"], session_id)
    assert ingested == ["srs.md"]


async def test_no_pr_comment_unless_it_is_turned_on(
    settings: Settings, worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Publishing to GitHub is outward-facing and stays opt-in."""
    assert settings.WORKFLOW_POST_AS_BUILT_COMMENT is False

    posted: list[int] = []

    async def spy(self, worktree_, number, body):  # noqa: ANN001
        posted.append(number)
        return True

    monkeypatch.setattr(
        "workflow_orchestrator.services.git.GitService.comment_on_pull_request", spy
    )

    class FakeSession:
        id = "sess-1"
        pr_number = 49

    artifact = worktree / ".workflow" / "as-built.md"
    artifact.write_text("# As-built\n", encoding="utf-8")
    await writeback._post_as_built_comment(
        settings, FakeSession(), worktree, artifact  # type: ignore[arg-type]
    )
    assert posted == []

    settings.WORKFLOW_POST_AS_BUILT_COMMENT = True
    await writeback._post_as_built_comment(
        settings, FakeSession(), worktree, artifact  # type: ignore[arg-type]
    )
    assert posted == [49]


# --- parsing the record --------------------------------------------------------


DOC = """\
# As-built

Two requirements did not ship as written.

```as-built
{"requirements": [
  {"id": "FR-1", "status": "implemented", "location": "app/a.py:10", "note": ""},
  {"id": "AC-13", "status": "dropped", "location": null,
   "note": "provider fallback chain never implemented"},
  {"id": "FR-5", "status": "deviated", "location": "app/core/retry.py:112",
   "note": "retries every NetworkError"}
]}
```
"""


def test_gaps_sort_ahead_of_passes() -> None:
    """A dropped requirement buried under thirty passes is a dropped requirement
    nobody reads."""
    document = as_built_service.parse_as_built(DOC)
    assert [r.id for r in document.requirements] == ["AC-13", "FR-5", "FR-1"]
    assert [r.id for r in document.gaps] == ["AC-13", "FR-5"]
    assert document.counts()["dropped"] == 1


def test_prose_excludes_the_machine_block() -> None:
    document = as_built_service.parse_as_built(DOC)
    assert "as-built" not in document.prose.split("\n", 1)[1]
    assert "Two requirements did not ship" in document.prose


def test_an_unknown_status_never_reads_as_implemented() -> None:
    """Over-claiming is the exact failure this feature exists to prevent."""
    document = as_built_service.parse_as_built(
        '```as-built\n{"requirements": [{"id": "FR-1", "status": "probably fine"}]}\n```'
    )
    assert document.requirements[0].status == as_built_service.STATUS_UNVERIFIED
    assert document.gaps


def test_a_malformed_block_degrades_to_prose() -> None:
    document = as_built_service.parse_as_built("# As-built\n\n```as-built\nnot json\n```")
    assert document.requirements == []
    assert "As-built" in document.prose


async def test_endpoint_reports_absent_before_the_merge(
    client, session: dict
) -> None:
    response = await client.get(f"/sessions/{session['id']}/as-built")
    assert response.status_code == 200
    assert response.json() == {
        "exists": False,
        "requirements": [],
        "content": "",
        "counts": {},
    }


async def test_endpoint_surfaces_the_gaps(
    client, session: dict, repo: Path
) -> None:
    workflow_dir = repo / "worktrees" / session["id"] / ".workflow"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "as-built.md").write_text(DOC, encoding="utf-8")

    body = (await client.get(f"/sessions/{session['id']}/as-built")).json()
    assert body["exists"] is True
    assert body["gaps"] == 2
    assert body["requirements"][0]["id"] == "AC-13"
    assert body["counts"]["implemented"] == 1


# --- STATUS_FIXED reconciliation -----------------------------------------------


def finding(status: str, run_id: str | None) -> review_service.Finding:
    return review_service.Finding(
        id="f1", title="a finding", status=status, run_id=run_id
    )


@pytest.mark.parametrize(
    ("run_status", "expected"),
    [
        ("completed", review_service.STATUS_FIXED),
        ("failed", review_service.STATUS_OPEN),
        ("timed_out", review_service.STATUS_OPEN),
        ("cost_exceeded", review_service.STATUS_OPEN),
    ],
)
async def test_a_finished_fix_run_moves_the_finding(
    tmp_path: Path, run_status: str, expected: str
) -> None:
    """``STATUS_FIXED`` was declared and never assigned anywhere in src/.

    Findings went ``open -> fixing`` and stayed, so ``outstanding()`` never fell
    and nothing downstream could tell a resolved finding from an abandoned one.
    """

    class FakeRun:
        status = run_status
        ended_at = "2026-08-01T20:02:44Z"

    class FakeDb:
        async def get(self, model, key):  # noqa: ANN001
            return FakeRun()

    findings = [finding(review_service.STATUS_FIXING, "run-1")]
    changed = await review_service.reconcile_fixing(FakeDb(), tmp_path, findings)

    assert changed is True
    assert findings[0].status == expected
    # Persisted beside the artifact, so the next read does not redo the work.
    assert review_service.load_triage(tmp_path)["f1"]["status"] == expected


async def test_a_still_running_fix_is_left_alone(tmp_path: Path) -> None:
    class FakeRun:
        status = "running"
        ended_at = None

    class FakeDb:
        async def get(self, model, key):  # noqa: ANN001
            return FakeRun()

    findings = [finding(review_service.STATUS_FIXING, "run-1")]
    assert await review_service.reconcile_fixing(FakeDb(), tmp_path, findings) is False
    assert findings[0].status == review_service.STATUS_FIXING


async def test_outstanding_drops_once_a_fix_completes(tmp_path: Path) -> None:
    class FakeRun:
        status = "completed"
        ended_at = "2026-08-01T20:02:44Z"

    class FakeDb:
        async def get(self, model, key):  # noqa: ANN001
            return FakeRun()

    findings = [finding(review_service.STATUS_FIXING, "run-1")]
    assert len(review_service.outstanding(findings)) == 1

    await review_service.reconcile_fixing(FakeDb(), tmp_path, findings)
    assert review_service.outstanding(findings) == []
