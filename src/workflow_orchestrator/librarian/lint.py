"""``wiki-lint`` — SRS FR-43, OQ-1.

OQ-1 asked whether lint should run on every automated write, on a timer, or
manually. Resolved to the SRS's proposed default: after every automated
write-back **and** exposed as a manual trigger in the UI. There is no timer —
a scheduled lint on a local single-user app would burn agent runs on an
unchanged wiki.

Lint results are surfaced in the wiki review queue alongside the pages they
concern.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from ..logging import get_logger
from ..models import Project
from ..services.process import run_command
from .layout import layout_for

log = get_logger(__name__)

LINT_TIMEOUT_SECONDS = 900.0

LINT_INSTRUCTION = """\
wiki-lint

Run the lint procedure in llm-wiki/schema.md section 9 and report the findings.

Do not fix anything and do not modify any file — this is a read-only check.

End your reply with a fenced summary block:

```wiki-lint
{
  "orphans": 0,
  "missing_frontmatter": 0,
  "stale": 0,
  "unresolved_flags": 0,
  "missing_index": 0,
  "issues": ["one line per problem found"]
}
```
"""

LINT_FENCE = re.compile(r"```wiki-lint\s*\n(.*?)\n```", re.DOTALL)


@dataclass
class LintResult:
    ok: bool
    counts: dict[str, int] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    raw: str = ""
    error: str | None = None

    @property
    def total_problems(self) -> int:
        return sum(self.counts.values())

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "counts": self.counts,
            "issues": self.issues,
            "total_problems": self.total_problems,
            "error": self.error,
        }


def parse_lint_output(output: str) -> LintResult:
    import json

    match = LINT_FENCE.search(output or "")
    if not match:
        return LintResult(ok=True, raw=output or "", issues=[])

    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return LintResult(ok=True, raw=output or "", error="unparseable lint block")

    if not isinstance(payload, dict):
        return LintResult(ok=True, raw=output or "")

    issues = payload.get("issues")
    counts = {
        key: int(value)
        for key, value in payload.items()
        if key != "issues" and isinstance(value, int | float)
    }
    return LintResult(
        ok=sum(counts.values()) == 0,
        counts=counts,
        issues=[str(i) for i in issues] if isinstance(issues, list) else [],
        raw=output or "",
    )


async def run_lint(
    settings: Settings, project: Project, *, session_id: str | None = None
) -> LintResult:
    layout = layout_for(project.wiki_repo_path)
    if not layout.is_initialized():
        return LintResult(ok=False, error="wiki repo is not initialized")

    argv = [
        settings.WORKFLOW_CODEX_BIN,
        "exec",
        "--json",
        "--cd",
        str(layout.repo_root),
        "-s",
        "read-only",  # lint must never mutate the wiki
        "-",
    ]
    try:
        result = await run_command(
            argv,
            cwd=layout.repo_root,
            stdin=LINT_INSTRUCTION,
            timeout=LINT_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, TimeoutError) as exc:
        log.warning("wiki.lint_unavailable", error=str(exc))
        return LintResult(ok=False, error=str(exc))

    if not result.ok:
        log.warning("wiki.lint_failed", stderr=result.stderr[-500:])
        return LintResult(ok=False, error=result.stderr[-500:] or "lint failed")

    parsed = parse_lint_output(result.stdout)
    log.info(
        "wiki.lint_complete",
        project_id=project.id,
        session_id=session_id,
        ok=parsed.ok,
        problems=parsed.total_problems,
    )
    return parsed


def latest_lint_path(repo_root: Path) -> Path:
    return layout_for(repo_root).wiki_dir / ".last-lint.json"
