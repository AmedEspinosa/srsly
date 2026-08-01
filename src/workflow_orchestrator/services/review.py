"""Review findings — SRS FR-24, FR-25.

``review.md`` is a Markdown document ending in a fenced ``review-findings``
block (see :mod:`workflow_orchestrator.harness.prompts`). Parsing that block is
what makes FR-25's triage view possible: each finding is addressable, so it can
be dismissed individually or turned into a scoped follow-up run.

Triage state lives in ``.workflow/review-triage.json`` beside the document
rather than in the database. The findings themselves are an artifact of the run,
and keeping their state next to the artifact means a session's review survives
being inspected, copied, or re-read without a database round trip.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..logging import get_logger

log = get_logger(__name__)

FINDINGS_FENCE = re.compile(r"```review-findings\s*\n(.*?)\n```", re.DOTALL)

SEVERITIES = ("high", "medium", "low")
CONFIDENCES = ("high", "medium", "low")

TRIAGE_FILENAME = "review-triage.json"

STATUS_OPEN = "open"
STATUS_DISMISSED = "dismissed"
STATUS_FIXING = "fixing"
STATUS_FIXED = "fixed"


@dataclass
class Finding:
    id: str
    title: str
    severity: str = "medium"
    confidence: str = "medium"
    file: str | None = None
    line: int | None = None
    detail: str = ""
    status: str = STATUS_OPEN
    run_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def as_prompt(self) -> str:
        """Rendered for a scoped follow-up implement run (FR-25)."""
        location = ""
        if self.file:
            location = f"\nLocation: {self.file}" + (f":{self.line}" if self.line else "")
        return (
            f"Finding: {self.title}\n"
            f"Severity: {self.severity} (reviewer confidence: {self.confidence})"
            f"{location}\n\n{self.detail}".strip()
        )


@dataclass
class ReviewDocument:
    prose: str
    findings: list[Finding] = field(default_factory=list)


def _normalize(raw: object, index: int) -> Finding | None:
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or "").strip()
    if not title:
        return None

    severity = str(raw.get("severity") or "medium").lower()
    if severity not in SEVERITIES:
        severity = "medium"
    confidence = str(raw.get("confidence") or "medium").lower()
    if confidence not in CONFIDENCES:
        confidence = "medium"

    line = raw.get("line")
    try:
        line_number = int(line) if line is not None else None
    except (TypeError, ValueError):
        line_number = None

    return Finding(
        id=str(raw.get("id") or f"f{index + 1}"),
        title=title,
        severity=severity,
        confidence=confidence,
        file=str(raw["file"]) if raw.get("file") else None,
        line=line_number,
        detail=str(raw.get("detail") or ""),
    )


def parse_review(text: str) -> ReviewDocument:
    """Split ``review.md`` into prose and structured findings."""
    match = FINDINGS_FENCE.search(text or "")
    prose = FINDINGS_FENCE.sub("", text or "").strip()

    if not match:
        return ReviewDocument(prose=prose, findings=[])

    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        log.warning("review.findings_block_unparseable")
        return ReviewDocument(prose=prose, findings=[])

    raw_findings = payload.get("findings") if isinstance(payload, dict) else payload
    if not isinstance(raw_findings, list):
        return ReviewDocument(prose=prose, findings=[])

    findings: list[Finding] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_findings):
        finding = _normalize(raw, index)
        if finding is None:
            continue
        # Duplicate ids would make triage act on the wrong row.
        if finding.id in seen:
            finding.id = f"{finding.id}-{index}"
        seen.add(finding.id)
        findings.append(finding)

    order = {name: i for i, name in enumerate(SEVERITIES)}
    findings.sort(key=lambda f: (order.get(f.severity, 99), f.id))
    return ReviewDocument(prose=prose, findings=findings)


# --- triage state --------------------------------------------------------------


def triage_path(worktree: Path) -> Path:
    return worktree / ".workflow" / TRIAGE_FILENAME


def load_triage(worktree: Path) -> dict[str, dict[str, object]]:
    path = triage_path(worktree)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_triage(worktree: Path, state: dict[str, dict[str, object]]) -> None:
    path = triage_path(worktree)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def findings_with_triage(worktree: Path, review_text: str) -> list[Finding]:
    document = parse_review(review_text)
    state = load_triage(worktree)
    for finding in document.findings:
        stored = state.get(finding.id)
        if isinstance(stored, dict):
            finding.status = str(stored.get("status") or STATUS_OPEN)
            run_id = stored.get("run_id")
            finding.run_id = str(run_id) if run_id else None
    return document.findings


def set_finding_status(
    worktree: Path, finding_id: str, status: str, *, run_id: str | None = None
) -> None:
    state = load_triage(worktree)
    entry = state.setdefault(finding_id, {})
    entry["status"] = status
    if run_id is not None:
        entry["run_id"] = run_id
    save_triage(worktree, state)
    log.info(
        "review.finding_triaged",
        finding_id=finding_id,
        status=status,
        run_id=run_id,
    )


def outstanding(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.status in (STATUS_OPEN, STATUS_FIXING)]
