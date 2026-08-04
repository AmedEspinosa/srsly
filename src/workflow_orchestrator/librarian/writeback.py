"""Post-merge wiki write-back — SRS FR-38..FR-41, NFR-6.

The §2.11 selection table is the single authority for what gets ingested. It is
a module-level constant so the policy is auditable in one place rather than
spread through the ingest logic:

| Artifact                                    | Action                    |
|---------------------------------------------|---------------------------|
| ``.workflow/srs.md``                         | Ingest via ``wiki-ingest``|
| Architectural decisions extracted from srs   | Ingest via ``wiki-ingest``|
| ``.workflow/as-built.md``                    | Ingest — generated here   |
| ``.workflow/plan.md``                        | Do not ingest — transient |
| ``.workflow/review.md``                      | Do not ingest             |
| Session transcripts / run logs               | Never ingest              |

``as-built.md`` extends §2.11 rather than following it. The table as written
ingests only the SRS, which is a statement of what was *proposed* — so the wiki
recorded intentions as facts, and a later session asking about a requirement
that was quietly dropped would be told it exists. This module now generates the
as-built record first (:func:`generate_as_built`) and ingests it second, so the
result corrects the proposal instead of the other way round.

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


SPEC_FRAMING = """\
This source is a software requirements specification produced by an automated \
development workflow. It records what was *proposed*. Extract the durable \
knowledge from it — architectural decisions, interfaces, constraints and their \
rationale — rather than transcribing the document."""

AS_BUILT_FRAMING = """\
This source is an as-built record produced after the change merged. It states, \
per requirement, what was actually implemented, what deviated from the \
specification, and what was dropped outright.

Where it contradicts a specification page already in the wiki, **this document \
wins** — the specification described an intention and this describes the result. \
Record dropped and deviated requirements explicitly. A reader who is told a \
feature exists when it does not is worse off than one told nothing."""


@dataclass(frozen=True)
class ArtifactPolicy:
    relative: str
    ingest: bool
    reason: str
    #: How the Librarian is told to read this artifact. Without it every source
    #: would be introduced as a specification, and an as-built record read as a
    #: specification is extracted as though its deviations were requirements.
    framing: str = SPEC_FRAMING


#: SRS §2.11 selection table. Order is the order artifacts are considered — and,
#: for ingest, the order they are written. ``as-built.md`` follows ``srs.md`` so
#: it corrects the record rather than being corrected by it.
SELECTION_TABLE: tuple[ArtifactPolicy, ...] = (
    ArtifactPolicy(".workflow/srs.md", True, "specification is durable context"),
    ArtifactPolicy(
        ".workflow/as-built.md",
        True,
        "what actually shipped — the wiki must not record intentions as facts",
        framing=AS_BUILT_FRAMING,
    ),
    ArtifactPolicy(".workflow/plan.md", False, "transient"),
    ArtifactPolicy(".workflow/review.md", False, "pre-fix defect list; superseded by as-built"),
    ArtifactPolicy(".workflow/diff.patch", False, "implementation detail"),
    ArtifactPolicy(".workflow/merged.diff", False, "input to as-built, not a source"),
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


def ingestible_artifacts(worktree: Path) -> list[tuple[Path, ArtifactPolicy]]:
    """Sources to ingest, each paired with the policy that frames it."""
    return [
        (worktree / policy.relative, policy)
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

{framing}

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


async def _ingest_one(
    settings: Settings, project: Project, source: Path, policy: ArtifactPolicy
) -> list[str]:
    layout = layout_for(project.wiki_repo_path)
    prompt = INGEST_INSTRUCTION.format(
        path=source, root=WIKI_ROOT, framing=policy.framing
    )

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


# --- as-built ------------------------------------------------------------------

AS_BUILT_RELATIVE = ".workflow/as-built.md"
MERGED_DIFF_RELATIVE = ".workflow/merged.diff"


async def generate_as_built(settings: Settings, session: Session, worktree: Path) -> Path | None:
    """Reconcile the SRS against what actually shipped — post-merge only.

    Post-merge is the only point at which this document can be true. Fixes land
    during pull-request review, maintainers amend, and a squash rewrites the
    branch entirely; a record written when the PR opened would describe a state
    that no longer exists.

    The *reviewing* harness runs it, for FR-23's reason: the harness that wrote
    the code is the wrong one to certify what the code does.

    Every failure path returns None and logs. The write-back must still ingest
    the specification if this cannot run — a wiki with the SRS alone is what we
    have today, and losing that too would make the feature a regression.
    """
    from ..harness import get_adapter
    from ..harness.runner import HarnessError
    from ..models import Harness
    from ..services.git import GitService

    srs = worktree / ".workflow" / "srs.md"
    if not srs.exists():
        log.warning("wiki.as_built_skipped", reason="no srs.md", session_id=session.id)
        return None

    # What actually landed, which the local branch may no longer match.
    merged_diff: Path | None = None
    if session.pr_number:
        diff_text = await GitService(settings).pull_request_diff(
            worktree, session.pr_number
        )
        if diff_text:
            merged_diff = worktree / MERGED_DIFF_RELATIVE
            merged_diff.parent.mkdir(parents=True, exist_ok=True)
            merged_diff.write_text(diff_text, encoding="utf-8")

    adapter = get_adapter(Harness(session.harness_review), settings)
    context = [
        srs,
        worktree / ".workflow" / "plan.md",
        worktree / ".workflow" / "review.md",
    ]
    try:
        artifact = await adapter.as_built(worktree, context, merged_diff)
    except (HarnessError, OSError, FileNotFoundError) as exc:
        log.warning("wiki.as_built_failed", session_id=session.id, error=str(exc))
        return None

    log.info(
        "wiki.as_built_written",
        session_id=session.id,
        artifact=str(artifact),
        harness=session.harness_review,
        merged_diff=str(merged_diff) if merged_diff else None,
    )
    await _post_as_built_comment(settings, session, worktree, artifact)
    return artifact


async def _post_as_built_comment(
    settings: Settings, session: Session, worktree: Path, artifact: Path
) -> None:
    """Optionally attach the record to the merged PR. Off by default.

    Publishing to GitHub is outward-facing, so it stays opt-in: the wiki is the
    intended home for this document and posting is a convenience on top.
    """
    if not settings.WORKFLOW_POST_AS_BUILT_COMMENT or not session.pr_number:
        return
    from ..services.git import GitService

    body = artifact.read_text(encoding="utf-8", errors="replace")
    await GitService(settings).comment_on_pull_request(worktree, session.pr_number, body)


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

    # Produce the as-built record *before* deciding what to ingest, so the run's
    # output is picked up by the selection table below rather than next time.
    await generate_as_built(settings, session, worktree)

    written: list[str] = []

    for source, policy in ingestible_artifacts(worktree):
        pages = await _ingest_one(settings, project, source, policy)
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
