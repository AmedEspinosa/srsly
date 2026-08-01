"""Consolidated wiki repository layout — SRS §4.5.

Page directories sit directly under ``llm-wiki/``:

    <wiki_repo_root>/
      llm-wiki/
        raw/  concepts/  summaries/  super-summaries/  analyses/  topics/  entities/
        index.md  log.md  schema.md
      AGENTS.md

Note this is *not* the layout of the existing personal vault, which nests the
page directories one level deeper under ``llm-wiki/wiki/``. SRS §1.2 rules out
migrating that vault, so the orchestrator targets a separate consolidated repo
and ``workflow-orchestrator wiki init`` scaffolds one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

WIKI_ROOT = "llm-wiki"

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


@dataclass(frozen=True)
class WikiLayout:
    repo_root: Path

    @property
    def wiki_dir(self) -> Path:
        return self.repo_root / WIKI_ROOT

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
        return self.wiki_dir / name

    def resolve(self, relative: str) -> Path:
        """Resolve a repo-relative page path, refusing to escape the repo."""
        candidate = (self.repo_root / relative).resolve()
        root = self.repo_root.resolve()
        if not candidate.is_relative_to(root):
            raise ValueError(f"path escapes the wiki repo: {relative}")
        return candidate

    def is_initialized(self) -> bool:
        return self.wiki_dir.is_dir() and self.index_md.exists()

    def missing_entries(self) -> list[str]:
        missing: list[str] = []
        if not self.wiki_dir.is_dir():
            missing.append(f"{WIKI_ROOT}/")
        for name in PAGE_DIRS:
            if not self.page_dir(name).is_dir():
                missing.append(f"{WIKI_ROOT}/{name}/")
        for name in ROOT_FILES:
            if not (self.wiki_dir / name).exists():
                missing.append(f"{WIKI_ROOT}/{name}")
        return missing


def layout_for(repo_path: Path | str) -> WikiLayout:
    return WikiLayout(repo_root=Path(repo_path).expanduser())
