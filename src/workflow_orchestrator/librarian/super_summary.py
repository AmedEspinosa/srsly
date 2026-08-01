"""Super summary regeneration — SRS FR-41, AC-8.

Regeneration is **never** automatic. The write-back records a
:class:`~workflow_orchestrator.models.SuperSummaryProposal`; this module runs
only when the user approves that proposal through the review queue, and only
from inside the serialised write queue (FR-42).
"""

from __future__ import annotations

from ..config import Settings
from ..logging import get_logger
from ..models import Project
from ..services.process import run_command
from .layout import layout_for
from .writeback import enforce_needs_review, parse_written_pages

log = get_logger(__name__)

TIMEOUT_SECONDS = 1200.0

INSTRUCTION = """\
wiki-super-summary: {summary_id}

Follow the super summary playbook in AGENTS.md and llm-wiki/schema.md.

This regeneration was approved by a human, but the output is still automated, so \
the resulting page MUST carry `needs_review: true` in its frontmatter.

List the repo-relative paths of every page you created or modified in a fenced \
block:

```wiki-written
llm-wiki/super-summaries/{summary_id}.md
```
"""


async def regenerate_super_summary(
    settings: Settings,
    project: Project,
    summary_id: str,
    *,
    session_id: str | None = None,
) -> list[str]:
    layout = layout_for(project.wiki_repo_path)
    if not layout.is_initialized():
        log.warning("wiki.super_summary_skipped", reason="wiki repo not initialized")
        return []

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
            argv,
            cwd=layout.repo_root,
            stdin=INSTRUCTION.format(summary_id=summary_id),
            timeout=TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, TimeoutError) as exc:
        log.warning("wiki.super_summary_unavailable", error=str(exc))
        return []

    if not result.ok:
        log.warning("wiki.super_summary_failed", stderr=result.stderr[-500:])
        return []

    pages = parse_written_pages(result.stdout)
    enforce_needs_review(layout.repo_root, pages)

    log.info(
        "wiki.super_summary_regenerated",
        project_id=project.id,
        summary_id=summary_id,
        session_id=session_id,
        pages=pages,
    )
    return pages
