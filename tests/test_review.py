"""Review findings parsing and triage — FR-24, FR-25."""

from __future__ import annotations

import json
from pathlib import Path

from workflow_orchestrator.services.review import (
    STATUS_DISMISSED,
    STATUS_FIXING,
    STATUS_OPEN,
    findings_with_triage,
    outstanding,
    parse_review,
    set_finding_status,
)

REVIEW_DOC = """\
# Review

The implementation matches the plan, with three exceptions.

```review-findings
{
  "findings": [
    {"id": "f1", "title": "Rate limit key is not namespaced", "severity": "high",
     "confidence": "high", "file": "app/limiter.py", "line": 42,
     "detail": "Two tenants share a bucket."},
    {"id": "f2", "title": "Missing test for the 429 path", "severity": "low",
     "confidence": "medium", "file": "tests/test_limiter.py"},
    {"id": "f3", "title": "Redis failure is not handled", "severity": "medium",
     "confidence": "high", "detail": "Fails closed instead of open."}
  ]
}
```
"""


def test_parses_findings_and_prose() -> None:
    document = parse_review(REVIEW_DOC)
    assert "three exceptions" in document.prose
    assert "review-findings" not in document.prose
    assert len(document.findings) == 3


def test_findings_are_sorted_by_severity() -> None:
    document = parse_review(REVIEW_DOC)
    assert [f.severity for f in document.findings] == ["high", "medium", "low"]
    assert document.findings[0].id == "f1"
    assert document.findings[0].line == 42


def test_missing_fence_yields_no_findings_but_keeps_prose() -> None:
    document = parse_review("# Review\n\nLooks fine to me.\n")
    assert document.findings == []
    assert "Looks fine" in document.prose


def test_unparseable_fence_is_tolerated() -> None:
    document = parse_review("# Review\n\n```review-findings\n{oops\n```\n")
    assert document.findings == []


def test_findings_without_a_title_are_dropped() -> None:
    payload = json.dumps({"findings": [{"id": "x"}, {"title": "Real one"}]})
    document = parse_review(f"```review-findings\n{payload}\n```")
    assert [f.title for f in document.findings] == ["Real one"]


def test_invalid_severity_falls_back_to_medium() -> None:
    payload = json.dumps({"findings": [{"title": "t", "severity": "catastrophic"}]})
    document = parse_review(f"```review-findings\n{payload}\n```")
    assert document.findings[0].severity == "medium"


def test_duplicate_ids_are_disambiguated() -> None:
    """Two findings sharing an id would make triage act on the wrong row."""
    payload = json.dumps(
        {"findings": [{"id": "f1", "title": "A"}, {"id": "f1", "title": "B"}]}
    )
    document = parse_review(f"```review-findings\n{payload}\n```")
    assert len({f.id for f in document.findings}) == 2


def test_empty_findings_array() -> None:
    document = parse_review('```review-findings\n{"findings": []}\n```')
    assert document.findings == []


def test_triage_state_round_trips(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    (worktree / ".workflow").mkdir(parents=True)

    findings = findings_with_triage(worktree, REVIEW_DOC)
    assert all(f.status == STATUS_OPEN for f in findings)

    set_finding_status(worktree, "f2", STATUS_DISMISSED)
    set_finding_status(worktree, "f1", STATUS_FIXING, run_id="run-123")

    reloaded = {f.id: f for f in findings_with_triage(worktree, REVIEW_DOC)}
    assert reloaded["f2"].status == STATUS_DISMISSED
    assert reloaded["f1"].status == STATUS_FIXING
    assert reloaded["f1"].run_id == "run-123"
    assert reloaded["f3"].status == STATUS_OPEN


def test_outstanding_excludes_dismissed(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    (worktree / ".workflow").mkdir(parents=True)
    set_finding_status(worktree, "f1", STATUS_DISMISSED)
    set_finding_status(worktree, "f2", STATUS_DISMISSED)

    findings = findings_with_triage(worktree, REVIEW_DOC)
    assert [f.id for f in outstanding(findings)] == ["f3"]


def test_finding_renders_a_scoped_follow_up_prompt() -> None:
    finding = parse_review(REVIEW_DOC).findings[0]
    prompt = finding.as_prompt()
    assert "Rate limit key is not namespaced" in prompt
    assert "app/limiter.py:42" in prompt
    assert "Two tenants share a bucket." in prompt


def test_corrupt_triage_file_is_ignored(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    (worktree / ".workflow").mkdir(parents=True)
    (worktree / ".workflow" / "review-triage.json").write_text("{not json", encoding="utf-8")

    findings = findings_with_triage(worktree, REVIEW_DOC)
    assert all(f.status == STATUS_OPEN for f in findings)
