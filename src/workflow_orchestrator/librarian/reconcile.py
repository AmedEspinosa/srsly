"""Reconcile ``needs_review`` frontmatter against the review queue.

The queue reads only the database (``wiki_writes.needs_review``); the flag in a
page's frontmatter is a label for whoever opens the file. Nothing kept the two
in step, and rejecting a page pulled them apart wholesale: ``git checkout``
restores a version committed *with* ``needs_review: true`` while the database row
is zeroed, so the file says "unreviewed" and the queue says "handled".

Direction of repair is file-follows-database, for four reasons:

  * the database is the only thing the queue consults; the frontmatter is derived;
  * a review is an event at a timestamp, while the file demonstrably can be
    rolled back — that is how these diverged in the first place;
  * the reverse would re-open pages a human already dispositioned; and
  * where the database has *no* opinion, neither does this module. A flagged
    page with no row is reported, never cleared — clearing it would invent a
    review that never happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..logging import get_logger
from ..models import Project, WikiWrite, utcnow
from .layout import WikiLayout, layout_for
from .writeback import clear_needs_review, has_needs_review

log = get_logger(__name__)


@dataclass(frozen=True)
class Divergence:
    """One page whose frontmatter and queue state disagree."""

    page_path: str
    path: Path


@dataclass
class ReconcileReport:
    layout: WikiLayout
    #: Flagged in the file, dispositioned in the database. Repairable.
    stale_labels: list[Divergence] = field(default_factory=list)
    #: Flagged in the file, unknown to the database. Reported only.
    unknown_provenance: list[Divergence] = field(default_factory=list)
    #: Queued in the database, unflagged in the file. Reported only.
    stale_queue: list[Divergence] = field(default_factory=list)
    scanned: int = 0

    @property
    def clean(self) -> bool:
        return not (self.stale_labels or self.unknown_provenance or self.stale_queue)


def _relative(layout: WikiLayout, path: Path) -> str:
    return path.resolve().relative_to(layout.repo_root.resolve()).as_posix()


async def survey(db: AsyncSession, project: Project) -> ReconcileReport:
    """Compare every page under the wiki directory against its queue rows.

    Only ``layout.wiki_dir`` is scanned. A vault usually lives inside a larger
    personal notes repository, and markdown elsewhere in that repository is
    nothing to do with this tool.
    """
    layout = layout_for(project.wiki_repo_path)
    report = ReconcileReport(layout=layout)
    if not layout.wiki_dir.is_dir():
        return report

    rows = (await db.execute(select(WikiWrite))).scalars().all()

    # Key rows by the file they actually resolve to, not by the stored string:
    # a canonical path and a nested vault name the same page differently, and
    # ``resolve`` is the only thing that knows which shape is on disk.
    queued: dict[Path, list[WikiWrite]] = {}
    for row in rows:
        try:
            resolved = layout.resolve(row.page_path)
        except ValueError:
            continue
        queued.setdefault(resolved.resolve(), []).append(row)

    seen: set[Path] = set()
    for path in sorted(layout.wiki_dir.rglob("*.md")):
        resolved = path.resolve()
        seen.add(resolved)
        report.scanned += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        flagged = has_needs_review(text)
        matching = queued.get(resolved, [])

        if flagged and not matching:
            report.unknown_provenance.append(Divergence(_relative(layout, path), path))
        elif flagged and all(row.needs_review == 0 for row in matching):
            report.stale_labels.append(Divergence(_relative(layout, path), path))
        elif not flagged and any(row.needs_review == 1 for row in matching):
            report.stale_queue.append(Divergence(_relative(layout, path), path))

    # Rows whose page has vanished are queue entries pointing at nothing.
    for resolved, matching in queued.items():
        if resolved not in seen and any(row.needs_review == 1 for row in matching):
            report.stale_queue.append(
                Divergence(matching[0].page_path, resolved)
            )

    return report


def apply_stale_labels(report: ReconcileReport) -> list[str]:
    """Write ``needs_review: false`` onto every stale-label page. Returns the paths."""
    written: list[str] = []
    for item in report.stale_labels:
        text = item.path.read_text(encoding="utf-8", errors="replace")
        cleared = clear_needs_review(text)
        if cleared is None or cleared == text:
            continue
        item.path.write_text(cleared, encoding="utf-8")
        written.append(item.page_path)
        log.info("wiki.stale_label_cleared", page_path=item.page_path)
    return written


async def adopt_unknown(db: AsyncSession, report: ReconcileReport) -> list[str]:
    """Enqueue flagged pages the database has never seen.

    The honest outcome for a page of unknown provenance: this does not pretend
    the page was reviewed, it puts it in front of a human.
    """
    adopted: list[str] = []
    for item in report.unknown_provenance:
        db.add(
            WikiWrite(
                session_id=None,
                page_path=item.page_path,
                operation="create",
                needs_review=1,
                written_at=utcnow(),
            )
        )
        adopted.append(item.page_path)
        log.info("wiki.unknown_page_adopted", page_path=item.page_path)
    return adopted
