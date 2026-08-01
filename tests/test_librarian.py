"""Librarian — FR-35..FR-43, NFR-6, AC-6, AC-7, AC-8."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from workflow_orchestrator.config import Settings
from workflow_orchestrator.librarian.layout import PAGE_DIRS, WIKI_ROOT, layout_for
from workflow_orchestrator.librarian.lint import parse_lint_output
from workflow_orchestrator.librarian.query import parse_selected_pages
from workflow_orchestrator.librarian.queue import WikiWriteQueue
from workflow_orchestrator.librarian.scaffold import init_wiki_repo
from workflow_orchestrator.librarian.writeback import (
    SELECTION_TABLE,
    ensure_needs_review,
    estimate_tokens,
    has_needs_review,
    parse_written_pages,
    should_ingest,
)


# --- §4.5 layout / scaffolding -------------------------------------------------


def test_scaffold_creates_the_srs_layout(tmp_path: Path) -> None:
    """§4.5 — page directories sit directly under llm-wiki/."""
    root = tmp_path / "wiki"
    init_wiki_repo(root)

    layout = layout_for(root)
    assert layout.is_initialized()
    for name in PAGE_DIRS:
        assert layout.page_dir(name).is_dir(), name
    assert layout.index_md.exists()
    assert layout.log_md.exists()
    assert layout.schema_md.exists()
    assert layout.agents_md.exists()
    # Not the vault's nested shape.
    assert not (layout.wiki_dir / "wiki").exists()
    assert layout.missing_entries() == []


def test_scaffold_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "wiki"
    first = init_wiki_repo(root)
    (layout_for(root).index_md).write_text("# customised\n", encoding="utf-8")
    second = init_wiki_repo(root)

    assert first != []
    assert second == []  # nothing re-created
    assert "customised" in layout_for(root).index_md.read_text()


def test_scaffold_seeds_schema_from_a_vault(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / WIKI_ROOT).mkdir(parents=True)
    (vault / WIKI_ROOT / "schema.md").write_text("# Real schema\n", encoding="utf-8")
    (vault / "AGENTS.md").write_text("# Real agents\n", encoding="utf-8")

    root = tmp_path / "wiki"
    init_wiki_repo(root, seed_from=vault)

    layout = layout_for(root)
    assert "Real schema" in layout.schema_md.read_text()
    assert "Real agents" in layout.agents_md.read_text()


def test_layout_refuses_paths_that_escape_the_repo(tmp_path: Path) -> None:
    layout = layout_for(tmp_path / "wiki")
    with pytest.raises(ValueError):
        layout.resolve("../../etc/passwd")


# --- §2.11 selection table (FR-38) ---------------------------------------------


def test_selection_table_matches_the_srs() -> None:
    decisions = {p.relative: p.ingest for p in SELECTION_TABLE}
    assert decisions[".workflow/srs.md"] is True
    assert decisions[".workflow/plan.md"] is False
    assert decisions[".workflow/review.md"] is False


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (".workflow/srs.md", True),
        (".workflow/plan.md", False),
        (".workflow/review.md", False),
        (".workflow/diff.patch", False),
        (".workflow/run-abc123.log", False),  # never ingest
        (".workflow/session-transcript.md", False),  # never ingest
        (".workflow/run-x.prompt.txt", False),
        (".workflow/something-new.md", False),  # unlisted defaults to no
    ],
)
def test_ingest_decisions(path: str, expected: bool) -> None:
    assert should_ingest(path) is expected


def test_run_logs_are_never_ingested_regardless_of_name() -> None:
    for name in (
        ".workflow/run-1.log",
        ".workflow/run-deadbeef-0000.log",
        ".workflow/TRANSCRIPT.md",
    ):
        assert should_ingest(name) is False


# --- FR-39: needs_review -------------------------------------------------------


def test_adds_frontmatter_when_absent() -> None:
    result = ensure_needs_review("# Page\n\nBody.\n")
    assert result.startswith("---\nneeds_review: true\n---\n")
    assert "# Page" in result
    assert has_needs_review(result)


def test_sets_flag_in_existing_frontmatter() -> None:
    text = "---\ntitle: Thing\nneeds_review: false\n---\n\n# Page\n"
    result = ensure_needs_review(text)
    assert has_needs_review(result)
    assert "title: Thing" in result
    assert "needs_review: false" not in result


def test_appends_flag_to_frontmatter_that_lacks_it() -> None:
    text = "---\ntitle: Thing\n---\n\n# Page\n"
    result = ensure_needs_review(text)
    assert has_needs_review(result)
    assert "title: Thing" in result


def test_has_needs_review_is_false_without_frontmatter() -> None:
    assert has_needs_review("# Page\n") is False


# --- FR-42 / AC-6: write serialisation -----------------------------------------


async def test_ac6_concurrent_writes_are_serialised() -> None:
    """AC-6 — two concurrent write-backs must not overlap."""
    queue = WikiWriteQueue()
    queue.start()

    active = 0
    max_concurrent = 0
    order: list[str] = []

    async def write(name: str, duration: float):
        nonlocal active, max_concurrent
        active += 1
        max_concurrent = max(max_concurrent, active)
        order.append(f"{name}:start")
        await asyncio.sleep(duration)
        order.append(f"{name}:end")
        active -= 1
        return name

    first = queue.submit("a", lambda: write("a", 0.05))
    second = queue.submit("b", lambda: write("b", 0.01))
    results = await asyncio.gather(first, second)

    assert results == ["a", "b"]
    assert max_concurrent == 1, "writes overlapped"
    # Strictly sequential: a finishes entirely before b starts.
    assert order == ["a:start", "a:end", "b:start", "b:end"]
    assert queue.completed == ["a", "b"]

    await queue.stop()


async def test_a_failing_task_does_not_stop_the_queue() -> None:
    queue = WikiWriteQueue()
    queue.start()

    async def boom():
        raise RuntimeError("ingest failed")

    async def fine():
        return "ok"

    failing = queue.submit("boom", boom)
    following = queue.submit("fine", fine)

    with pytest.raises(RuntimeError, match="ingest failed"):
        await failing
    assert await following == "ok"

    await queue.stop()


async def test_queue_preserves_submission_order() -> None:
    queue = WikiWriteQueue()
    queue.start()

    async def noop(name: str):
        await asyncio.sleep(0)
        return name

    futures = [queue.submit(str(i), lambda i=i: noop(str(i))) for i in range(10)]
    await asyncio.gather(*futures)
    assert queue.completed == [str(i) for i in range(10)]

    await queue.stop()


# --- NFR-6 / AC-7: index.md token logging --------------------------------------


async def test_ac7_index_metrics_are_logged(tmp_path: Path) -> None:
    """AC-7 — structured log with timestamp, token_count, page_count, session id.

    Asserts on the structlog event dict rather than rendered text, so the
    required *keys* are pinned rather than however the renderer happens to
    format them.
    """
    from structlog.testing import capture_logs

    from workflow_orchestrator.librarian.writeback import log_index_metrics

    root = tmp_path / "wiki"
    init_wiki_repo(root)
    layout = layout_for(root)
    layout.index_md.write_text("# Index\n\n" + ("word " * 500), encoding="utf-8")
    (layout.page_dir("concepts") / "a.md").write_text("# A\n", encoding="utf-8")

    with capture_logs() as logs:
        tokens = await log_index_metrics(root, session_id="sess-42", project_id="proj-1")

    assert tokens > 0
    entry = next(e for e in logs if e.get("event") == "wiki.index_metrics")
    # AC-7: "structured log entry with keys: timestamp, token_count,
    # page_count, triggering_session_id. token_count > 0."
    assert entry["token_count"] > 0
    assert entry["page_count"] >= 1
    assert entry["triggering_session_id"] == "sess-42"
    assert entry["timestamp"].endswith("Z")


def test_token_estimate_scales_with_content() -> None:
    assert estimate_tokens("") == 0
    small = estimate_tokens("hello world")
    large = estimate_tokens("hello world " * 100)
    assert large > small > 0


# --- parsing helpers -----------------------------------------------------------


def test_parses_selected_query_pages() -> None:
    output = f"""
Some prose.

```wiki-pages
{WIKI_ROOT}/concepts/auth.md
{WIKI_ROOT}/summaries/2026-01-01__auth.md
```
"""
    assert parse_selected_pages(output) == [
        f"{WIKI_ROOT}/concepts/auth.md",
        f"{WIKI_ROOT}/summaries/2026-01-01__auth.md",
    ]


def test_query_page_parsing_rejects_traversal_and_foreign_paths() -> None:
    output = f"""```wiki-pages
{WIKI_ROOT}/../../etc/passwd
/etc/passwd
notes/other.md
{WIKI_ROOT}/concepts/ok.md
```"""
    assert parse_selected_pages(output) == [f"{WIKI_ROOT}/concepts/ok.md"]


def test_query_page_parsing_falls_back_without_a_fence() -> None:
    output = f"I would read {WIKI_ROOT}/concepts/auth.md for this."
    assert parse_selected_pages(output) == [f"{WIKI_ROOT}/concepts/auth.md"]


def test_query_page_parsing_caps_the_result() -> None:
    lines = "\n".join(f"{WIKI_ROOT}/concepts/p{i}.md" for i in range(20))
    assert len(parse_selected_pages(f"```wiki-pages\n{lines}\n```")) == 8


def test_parses_written_pages() -> None:
    output = f"```wiki-written\n{WIKI_ROOT}/concepts/x.md\n{WIKI_ROOT}/summaries/y.md\n```"
    assert parse_written_pages(output) == [
        f"{WIKI_ROOT}/concepts/x.md",
        f"{WIKI_ROOT}/summaries/y.md",
    ]


def test_parses_lint_summary() -> None:
    output = """```wiki-lint
{"orphans": 2, "missing_frontmatter": 0, "issues": ["orphan: a.md", "orphan: b.md"]}
```"""
    result = parse_lint_output(output)
    assert result.ok is False
    assert result.counts["orphans"] == 2
    assert result.total_problems == 2
    assert len(result.issues) == 2


def test_clean_lint_is_ok() -> None:
    result = parse_lint_output('```wiki-lint\n{"orphans": 0, "issues": []}\n```')
    assert result.ok is True
    assert result.total_problems == 0


def test_lint_without_a_fence_is_not_an_error() -> None:
    assert parse_lint_output("Everything looks fine.").ok is True
