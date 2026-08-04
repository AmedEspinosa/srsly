"""The as-built record — what shipped, versus what the SRS proposed.

``as-built.md`` is a Markdown document ending in a fenced ``as-built`` block, the
same technique that makes review findings addressable
(:mod:`workflow_orchestrator.services.review`). Parsing it turns the record from
prose into something the UI can rank and a reader can scan: the dropped and
deviated requirements are the point, and they should not have to be found by
reading a table.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from ..logging import get_logger

log = get_logger(__name__)

AS_BUILT_FENCE = re.compile(r"```as-built\s*\n(.*?)\n```", re.DOTALL)

ARTIFACT_RELATIVE = ".workflow/as-built.md"

STATUS_IMPLEMENTED = "implemented"
STATUS_DEVIATED = "deviated"
STATUS_DROPPED = "dropped"
STATUS_UNVERIFIED = "unverified"

STATUSES = (STATUS_IMPLEMENTED, STATUS_DEVIATED, STATUS_DROPPED, STATUS_UNVERIFIED)

#: Ordering for display. Dropped first: a requirement that silently does not
#: exist is the one most likely to mislead someone reading the wiki later.
_RANK = {STATUS_DROPPED: 0, STATUS_DEVIATED: 1, STATUS_UNVERIFIED: 2, STATUS_IMPLEMENTED: 3}


@dataclass
class Requirement:
    id: str
    status: str = STATUS_UNVERIFIED
    location: str | None = None
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class AsBuiltDocument:
    prose: str
    requirements: list[Requirement]

    @property
    def gaps(self) -> list[Requirement]:
        """Everything that is not plainly implemented — the reason this exists."""
        return [r for r in self.requirements if r.status != STATUS_IMPLEMENTED]

    def counts(self) -> dict[str, int]:
        return {
            status: sum(1 for r in self.requirements if r.status == status)
            for status in STATUSES
        }


def _normalize(raw: object) -> Requirement | None:
    if not isinstance(raw, dict):
        return None
    identifier = str(raw.get("id") or "").strip()
    if not identifier:
        return None

    status = str(raw.get("status") or STATUS_UNVERIFIED).strip().lower()
    if status not in STATUSES:
        # An unrecognised status must not read as "implemented"; the failure mode
        # this whole feature exists to prevent is over-claiming.
        log.warning("as_built.unknown_status", requirement=identifier, status=status)
        status = STATUS_UNVERIFIED

    location = raw.get("location")
    return Requirement(
        id=identifier,
        status=status,
        location=str(location) if location else None,
        note=str(raw.get("note") or ""),
    )


def parse_as_built(text: str) -> AsBuiltDocument:
    """Split ``as-built.md`` into prose and structured requirement statuses."""
    match = AS_BUILT_FENCE.search(text or "")
    prose = AS_BUILT_FENCE.sub("", text or "").strip()

    if not match:
        return AsBuiltDocument(prose=prose, requirements=[])

    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        log.warning("as_built.block_unparseable")
        return AsBuiltDocument(prose=prose, requirements=[])

    raw = payload.get("requirements") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        return AsBuiltDocument(prose=prose, requirements=[])

    requirements: list[Requirement] = []
    seen: set[str] = set()
    for entry in raw:
        requirement = _normalize(entry)
        if requirement is None or requirement.id in seen:
            continue
        seen.add(requirement.id)
        requirements.append(requirement)

    requirements.sort(key=lambda r: (_RANK.get(r.status, 9), r.id))
    return AsBuiltDocument(prose=prose, requirements=requirements)


def read(worktree: Path) -> AsBuiltDocument | None:
    path = worktree / ARTIFACT_RELATIVE
    if not path.exists():
        return None
    return parse_as_built(path.read_text(encoding="utf-8", errors="replace"))
