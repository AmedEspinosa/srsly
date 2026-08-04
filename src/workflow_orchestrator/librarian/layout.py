"""Consolidated wiki repository layout — SRS §4.5.

Page directories sit directly under ``llm-wiki/``:

    <wiki_repo_root>/
      llm-wiki/
        raw/  concepts/  summaries/  super-summaries/  analyses/  topics/  entities/
        index.md  log.md  schema.md
      AGENTS.md

``wiki init`` scaffolds exactly that. But an existing vault may nest the page
directories one level deeper, under ``llm-wiki/wiki/``, keeping ``index.md`` and
``schema.md`` at ``llm-wiki/``. Reading such a vault is not migrating it, so
SRS §1.2 is untouched: the layout *detects* which of the two shapes is on disk
and resolves page directories accordingly, rather than assuming the canonical
one and silently finding nothing.

The configured path is also normalised. Pointing a project at ``…/llm-wiki`` or
``…/llm-wiki/wiki`` is the obvious mistake to make — both are "the wiki" in
conversation — and the repo root is the enclosing directory in each case.
Getting this wrong used to degrade silently: retrieval returned empty context
and the requirements engine asked generic questions with no indication that the
wiki had been skipped.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

WIKI_ROOT = "llm-wiki"

#: Some vaults keep the page directories one level below ``llm-wiki/``.
NESTED_PAGES_DIR = "wiki"

PAGE_DIRS = (
    "raw",
    "concepts",
    "summaries",
    "super-summaries",
    "analyses",
    "topics",
    "entities",
)

ROOT_FILES = ("index.md", "log.md", "schema.md")
AGENTS_FILE = "AGENTS.md"


def discover_repo_root(configured: Path | str) -> Path:
    """Normalise a configured wiki path to the repo root that holds ``llm-wiki/``.

    Accepts the repo root itself, the ``llm-wiki/`` directory, or a page
    directory nested directly inside it. Anything else is returned unchanged so
    that ``wiki init`` can still scaffold into a fresh, empty path.
    """
    path = Path(configured).expanduser()
    if (path / WIKI_ROOT).is_dir():
        return path
    # Walk up at most two levels — llm-wiki/ and llm-wiki/<nested>/ — so a
    # mis-levelled config resolves instead of failing shut.
    for candidate in (path, *list(path.parents)[:2]):
        if candidate.name == WIKI_ROOT and candidate.parent != candidate:
            return candidate.parent
    return path


@dataclass(frozen=True)
class WikiLayout:
    repo_root: Path

    @property
    def wiki_dir(self) -> Path:
        return self.repo_root / WIKI_ROOT

    @property
    def pages_root(self) -> Path:
        """Where the page directories actually live.

        Canonically ``llm-wiki/`` itself; a vault that nests them reports
        ``llm-wiki/wiki/``. Detected from disk so both shapes read alike.
        """
        nested = self.wiki_dir / NESTED_PAGES_DIR
        if nested.is_dir() and any((nested / name).is_dir() for name in PAGE_DIRS):
            return nested
        return self.wiki_dir

    @property
    def pages_prefix(self) -> str:
        """``pages_root`` as a repo-relative POSIX path, for prompts and paths."""
        return self.pages_root.relative_to(self.repo_root).as_posix()

    @property
    def index_md(self) -> Path:
        return self.wiki_dir / "index.md"

    @property
    def log_md(self) -> Path:
        return self.wiki_dir / "log.md"

    @property
    def schema_md(self) -> Path:
        return self.wiki_dir / "schema.md"

    @property
    def agents_md(self) -> Path:
        return self.repo_root / AGENTS_FILE

    def page_dir(self, name: str) -> Path:
        """Locate one page directory, tolerating a partially nested vault.

        The vault this was first pointed at keeps ``raw/`` at ``llm-wiki/`` but
        every other page directory under ``llm-wiki/wiki/``, so probe both.
        A directory that exists in neither place resolves to ``pages_root`` —
        new pages are written where the bulk of them already live.
        """
        nested = self.pages_root / name
        if nested.is_dir():
            return nested
        alongside = self.wiki_dir / name
        if alongside.is_dir():
            return alongside
        return nested

    def _alternate_placement(self, path: Path) -> Path | None:
        """The same page under the other of the two page-directory shapes."""
        wiki = self.wiki_dir.resolve()
        pages = self.pages_root.resolve()
        if pages == wiki:
            return None
        try:
            parts = path.relative_to(wiki).parts
        except ValueError:
            return None
        if parts and parts[0] == NESTED_PAGES_DIR:
            return wiki.joinpath(*parts[1:])
        return pages.joinpath(*parts)

    def resolve(self, relative: str) -> Path:
        """Resolve a repo-relative page path, refusing to escape the repo.

        A path written in the canonical shape (``llm-wiki/concepts/x.md``) also
        resolves against a nested vault that stores it at
        ``llm-wiki/wiki/concepts/x.md``, and vice versa — the SRS's paths are
        what people copy, and silently not finding the file is the failure mode
        this whole module exists to prevent. Only an *existing* file redirects;
        new pages are still created at the literal path.
        """
        candidate = (self.repo_root / relative).resolve()
        root = self.repo_root.resolve()
        if not candidate.is_relative_to(root):
            raise ValueError(f"path escapes the wiki repo: {relative}")
        if candidate.exists():
            return candidate
        alternate = self._alternate_placement(candidate)
        if alternate is not None and alternate.exists():
            return alternate
        return candidate

    def is_initialized(self) -> bool:
        return self.wiki_dir.is_dir() and self.index_md.exists()

    def missing_entries(self) -> list[str]:
        missing: list[str] = []
        if not self.wiki_dir.is_dir():
            missing.append(f"{WIKI_ROOT}/")
        prefix = self.pages_prefix
        for name in PAGE_DIRS:
            if not self.page_dir(name).is_dir():
                missing.append(f"{prefix}/{name}/")
        for name in ROOT_FILES:
            if not (self.wiki_dir / name).exists():
                missing.append(f"{WIKI_ROOT}/{name}")
        return missing


def layout_for(repo_path: Path | str) -> WikiLayout:
    return WikiLayout(repo_root=discover_repo_root(repo_path))
