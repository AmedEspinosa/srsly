"""``wiki-query`` invocation — SRS FR-36, OQ-4.

OQ-4 asks whether ``wiki-query`` needs the wiki repo as the working directory.
Resolved to the SRS's proposed default: the Librarian ``chdir``s into the wiki
repo before invoking Codex, because ``AGENTS.md`` — which defines the ``wiki-*``
command vocabulary — is discovered relative to the working directory.
"""

from __future__ import annotations

import re

from ..config import Settings
from ..logging import get_logger
from ..models import Project
from ..services.process import run_command
from .layout import WIKI_ROOT, layout_for

log = get_logger(__name__)

QUERY_TIMEOUT_SECONDS = 300.0

#: The agent is asked to end its reply with this fence so page selection is
#: parseable rather than scraped out of prose.
PAGES_FENCE = re.compile(r"```wiki-pages\s*\n(.*?)\n```", re.DOTALL)

QUERY_INSTRUCTION = """\
wiki-query: {question}

Follow the query procedure in llm-wiki/schema.md. Do not create or modify any \
files — this is a read-only lookup.

End your reply with a fenced block listing only the repo-relative paths of the \
pages a developer should read for this task, most relevant first, at most 8:

```wiki-pages
{root}/concepts/example.md
{root}/summaries/example.md
```

If nothing in the wiki is relevant, emit the fence with no lines inside it."""


def parse_selected_pages(output: str, *, root: str = WIKI_ROOT) -> list[str]:
    """Pull repo-relative page paths out of the agent's reply."""
    match = PAGES_FENCE.search(output or "")
    candidates: list[str] = []

    if match:
        candidates = [line.strip() for line in match.group(1).splitlines()]
    else:
        # Fall back to scanning for anything that looks like a wiki page path,
        # so a reply that forgets the fence still yields usable context.
        candidates = re.findall(rf"{re.escape(root)}/[\w./-]+\.md", output or "")

    seen: set[str] = set()
    pages: list[str] = []
    for raw in candidates:
        path = raw.strip().strip("`").lstrip("-").strip()
        if not path or not path.endswith(".md"):
            continue
        if not path.startswith(f"{root}/"):
            continue
        if ".." in path:  # never let a reply walk out of the repo
            continue
        if path in seen:
            continue
        seen.add(path)
        pages.append(path)
    return pages[:8]


async def query_pages(
    settings: Settings, project: Project, question: str
) -> list[str]:
    """Run ``wiki-query`` in the wiki repo and return the selected page paths."""
    layout = layout_for(project.wiki_repo_path)
    if not layout.is_initialized():
        return []

    # Show the example paths at the prefix this vault actually uses, so the
    # agent emits paths that resolve instead of ones shaped like the SRS's
    # canonical layout.
    prompt = QUERY_INSTRUCTION.format(
        question=question.strip(), root=layout.pages_prefix
    )
    argv = [
        settings.WORKFLOW_CODEX_BIN,
        "exec",
        "--json",
        "--cd",
        str(layout.repo_root),
        "-s",
        "read-only",  # a query must never mutate the wiki
        prompt,
    ]

    try:
        result = await run_command(
            argv, cwd=layout.repo_root, timeout=QUERY_TIMEOUT_SECONDS
        )
    except (FileNotFoundError, TimeoutError) as exc:
        log.warning("wiki.query_unavailable", project_id=project.id, error=str(exc))
        return []

    if not result.ok:
        log.warning(
            "wiki.query_failed",
            project_id=project.id,
            returncode=result.returncode,
            stderr=result.stderr[-500:],
        )
        return []

    pages = parse_selected_pages(result.stdout)
    log.info("wiki.query_selected", project_id=project.id, pages=pages)
    return pages
