"""Post-merge wiki write-back — SRS FR-38..FR-41, NFR-6.

The §2.11 selection table is the single authority for what gets ingested. It is
a module-level constant so the policy is auditable in one place rather than
spread through the ingest logic:

| Artifact                                    | Action                    |
|---------------------------------------------|---------------------------|
| ``.workflow/srs.md``                         | Ingest via ``wiki-ingest``|
| Architectural decisions extracted from srs   | Ingest via ``wiki-ingest``|
| ``.workflow/plan.md``                        | Do not ingest — transient |
| ``.workflow/review.md``                      | Do not ingest             |
| Session transcripts / run logs               | Never ingest              |

Every write goes through the serialised queue (FR-42), every page it creates or
modifies is flagged ``needs_review: true`` (FR-39), and a super-summary
regeneration is only ever *proposed* (FR-41).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from ..config import Settings
from ..db import session_scope
from ..logging import get_logger
from ..models import Project, Session, SuperSummaryProposal, WikiOperation, WikiWrite, utcnow
from ..services.process import run_command
from .layout import WIKI_ROOT, layout_for
from .queue import get_queue

log = get_logger(__name__)

INGEST_TIMEOUT_SECONDS = 900.0


@dataclass(frozen=True)
class ArtifactPolicy:
    relative: str
    ingest: bool
    reason: str


#: SRS §2.11 selection table. Order is the order artifacts are considered.
SELECTION_TABLE: tuple[ArtifactPolicy, ...] = (
    ArtifactPolicy(".workflow/srs.md", True, "specification is durable context"),
    ArtifactPolicy(".workflow/plan.md", False, "transient"),
    ArtifactPolicy(".workflow/review.md", False, "not ingested"),
    ArtifactPolicy(".workflow/diff.patch", False, "implementation detail"),
    ArtifactPolicy(".workflow/pr.json", False, "not ingested"),
)

#: Run logs and transcripts are never ingested, whatever their name.
NEVER_INGEST_PATTERNS = (
    re.compile(r"^\.workflow/run-.*\.log$"),
    re.compile(r"^\.workflow/.*transcript.*$", re.IGNORECASE),
    re.compile(r"^\.workflow/.*\.prompt\.txt$"),
    re.compile(r"^\.workflow/.*\.exit$"),
)


def should_ingest(relative_path: str) -> bool:
    """The §2.11 decision for one artifact."""
    # removeprefix, not lstrip: lstrip strips *characters*, so lstrip("./") turns
    # ".workflow/srs.md" into "workflow/srs.md" and nothing ever matches.
    normalized = relative_path.replace("\\", "/").removeprefix("./")
    for pattern in NEVER_INGEST_PATTERNS:
        if pattern.match(normalized):
            return False
    for policy in SELECTION_TABLE:
        if normalized == policy.relative:
            return policy.ingest
    # Anything not named in the table is not ingested. Defaulting to "no" keeps
    # transcripts and future scratch files out without needing a rule each.
    return False


def ingestible_artifacts(worktree: Path) -> list[Path]:
    return [
        worktree / policy.relative
        for policy in SELECTION_TABLE
        if policy.ingest and (worktree / policy.relative).exists()
    ]


# --- frontmatter ---------------------------------------------------------------

FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def ensure_needs_review(text: str) -> str:
    """FR-39 — force ``needs_review: true`` into a page's frontmatter."""
    match = FRONTMATTER.match(text)
    if not match:
        return f"---\nneeds_review: true\n---\n\n{text.lstrip()}"

    block = match.group(1)
    if re.search(r"^needs_review\s*:", block, re.MULTILINE):
        block = re.sub(
            r"^needs_review\s*:.*$", "needs_review: true", block, flags=re.MULTILINE
        )
    else:
        block = block.rstrip() + "\nneeds_review: true"
    return f"---\n{block}\n---\n" + text[match.end() :]


def has_needs_review(text: str) -> bool:
    match = FRONTMATTER.match(text)
    if not match:
        return False
    return bool(
        re.search(r"^needs_review\s*:\s*true\s*$", match.group(1), re.MULTILINE)
    )


# --- token accounting (NFR-6) ---------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Rough token count for ``index.md`` growth tracking.

    A local heuristic rather than a tokenizer call: NFR-6 wants a trend line to
    decide when semantic search becomes necessary, and paying for an API call on
    every wiki write to sharpen that trend would be the wrong trade.
    """
    if not text:
        return 0
    return max(len(text) // 4, len(text.split()))


def count_pages(repo_root: Path) -> int:
    wiki_dir = layout_for(repo_root).wiki_dir
    if not wiki_dir.is_dir():
        return 0
    return sum(1 for _ in wiki_dir.rglob("*.md"))


async def log_index_metrics(
    repo_root: Path, *, session_id: str | None, project_id: str | None = None
) -> int:
    """NFR-6 / AC-7 — structured log line after every wiki write."""
    layout = layout_for(repo_root)
    text = (
        layout.index_md.read_text(encoding="utf-8", errors="replace")
        if layout.index_md.exists()
        else ""
    )
    token_count = estimate_tokens(text)
    pages = count_pages(repo_root)

    log.info(
        "wiki.index_metrics",
        timestamp=utcnow(),
        token_count=token_count,
        page_count=pages,
        triggering_session_id=session_id,
        project_id=project_id,
    )
    return token_count


# --- ingest --------------------------------------------------------------------

INGEST_INSTRUCTION = """\
wiki-ingest: {path}

Follow the ingest procedure in llm-wiki/schema.md.

This source is a software requirements specification produced by an automated \
development workflow. Extract the durable knowledge from it — architectural \
decisions, interfaces, constraints and their rationale — rather than transcribing \
the document.

Every page you create or modify MUST carry `needs_review: true` in its \
frontmatter, because this write was automated and a human has not yet checked it.

When you are done, list the repo-relative paths of every page you created or \
modified in a fenced block:

```wiki-written
{root}/concepts/example.md
```
"""

WRITTEN_FENCE = re.compile(r"```wiki-written\s*\n(.*?)\n```", re.DOTALL)


def parse_written_pages(output: str, *, root: str = WIKI_ROOT) -> list[str]:
    match = WRITTEN_FENCE.search(output or "")
    candidates = (
        [line.strip() for line in match.group(1).splitlines()]
        if match
        else re.findall(rf"{re.escape(root)}/[\w./-]+\.md", output or "")
    )
    pages: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        path = raw.strip().strip("`").lstrip("-").strip()
        if not path.endswith(".md") or not path.startswith(f"{root}/") or ".." in path:
            continue
        if path in seen:
            continue
        seen.add(path)
        pages.append(path)
    return pages


async def _ingest_one(settings: Settings, project: Project, source: Path) -> list[str]:
    layout = layout_for(project.wiki_repo_path)
    prompt = INGEST_INSTRUCTION.format(path=source, root=WIKI_ROOT)

    argv = [
        settings.WORKFLOW_CODEX_BIN,
        "exec",
        "--json",
        "--cd",
        str(layout.repo_root),
        "-s",
        "workspace-write",
        "--dangerously-bypass-approvals-and-sandbox",
        "-",
    ]
    try:
        result = await run_command(
            argv, cwd=layout.repo_root, stdin=prompt, timeout=INGEST_TIMEOUT_SECONDS
        )
    except (FileNotFoundError, TimeoutError) as exc:
        log.warning("wiki.ingest_unavailable", error=str(exc))
        return []

    if not result.ok:
        log.warning("wiki.ingest_failed", stderr=result.stderr[-500:])
        return []

    return parse_written_pages(result.stdout)


def enforce_needs_review(repo_root: Path, pages: list[str]) -> list[str]:
    """FR-39 — belt and braces: rewrite frontmatter the agent may have missed."""
    layout = layout_for(repo_root)
    corrected: list[str] = []
    for relative in pages:
        try:
            path = layout.resolve(relative)
        except ValueError:
            continue
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if has_needs_review(text):
            continue
        path.write_text(ensure_needs_review(text), encoding="utf-8")
        corrected.append(relative)
        log.info("wiki.needs_review_enforced", page=relative)
    return corrected


async def _record_writes(
    session_id: str | None, pages: list[str], index_tokens: int
) -> None:
    if not pages:
        return
    async with session_scope() as db:
        for relative in pages:
            existing = (
                await db.execute(
                    select(WikiWrite).where(WikiWrite.page_path == relative)
                )
            ).scalars().first()
            db.add(
                WikiWrite(
                    session_id=session_id,
                    page_path=relative,
                    operation=(
                        WikiOperation.UPDATE.value
                        if existing
                        else WikiOperation.CREATE.value
                    ),
                    needs_review=1,
                    index_token_count=index_tokens,
                    written_at=utcnow(),
                )
            )


async def _propose_super_summary(
    project: Project, session_id: str | None, pages: list[str]
) -> None:
    """FR-41 / AC-8 — propose regeneration; never execute it."""
    if not pages:
        return
    summary_id = Path(project.wiki_super_summary_path).stem
    async with session_scope() as db:
        db.add(
            SuperSummaryProposal(
                session_id=session_id,
                project_id=project.id,
                super_summary_id=summary_id,
                rationale=(
                    f"{len(pages)} page(s) were written by automated post-merge "
                    "ingest; the super summary may now be stale."
                ),
                status="pending",
                created_at=utcnow(),
            )
        )
    log.info(
        "wiki.super_summary_proposed",
        project_id=project.id,
        super_summary_id=summary_id,
        pages=len(pages),
    )


async def run_post_merge(settings: Settings, project_id: str, session_id: str) -> list[str]:
    """The actual write-back. Always invoked from inside the queue (FR-42)."""
    async with session_scope() as db:
        project = await db.get(Project, project_id)
        session = await db.get(Session, session_id)
    if project is None or session is None:  # pragma: no cover
        return []

    layout = layout_for(project.wiki_repo_path)
    if not layout.is_initialized():
        log.warning(
            "wiki.not_initialized_skipping_writeback",
            project_id=project_id,
            hint="run: workflow-orchestrator wiki init <path>",
        )
        return []

    from ..services.workflow import session_worktree

    worktree = session_worktree(project, session)
    written: list[str] = []

    for source in ingestible_artifacts(worktree):
        pages = await _ingest_one(settings, project, source)
        written.extend(pages)
        log.info(
            "wiki.ingested", source=str(source), pages=len(pages), session_id=session_id
        )

    enforce_needs_review(layout.repo_root, written)

    index_tokens = await log_index_metrics(
        layout.repo_root, session_id=session_id, project_id=project_id
    )
    await _record_writes(session_id, written, index_tokens)
    await _propose_super_summary(project, session_id, written)

    # FR-43 — lint after every automated write-back.
    from .lint import run_lint

    if written:
        await run_lint(settings, project, session_id=session_id)

    return written


async def enqueue_post_merge(
    settings: Settings, project_id: str, session_id: str
) -> None:
    """FR-29 — queue the post-merge write-back (FR-42 serialises it)."""
    queue = get_queue()
    queue.submit(
        f"post-merge:{session_id}",
        lambda: run_post_merge(settings, project_id, session_id),
    )
