"""Regression: the wiki must be found in both on-disk shapes, and a wiki that
is *not* found must say so.

The first live project was configured with ``wiki_repo_path`` pointing at
``…/llm-wiki/wiki`` — the directory the page folders live in, which reads as
"the wiki" but is two levels below the repo root. ``is_initialized()`` looked
for ``<configured>/llm-wiki/index.md``, found nothing, and ``build_session_context``
returned "". The QA phase then ran with the feature prompt alone and asked
generic questions, with no signal anywhere that the wiki had been skipped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from workflow_orchestrator.librarian.layout import (
    PAGE_DIRS,
    WIKI_ROOT,
    discover_repo_root,
    layout_for,
)


def _make_vault(root: Path, *, nested: bool) -> Path:
    """Build either the canonical §4.5 layout or the nested-vault variant."""
    wiki = root / WIKI_ROOT
    pages = wiki / "wiki" if nested else wiki
    for name in PAGE_DIRS:
        (pages / name).mkdir(parents=True, exist_ok=True)
    for name in ("index.md", "log.md", "schema.md"):
        (wiki / name).write_text(f"# {name}\n", encoding="utf-8")
    return root


@pytest.mark.parametrize("nested", [False, True])
def test_layout_resolves_from_any_configured_level(tmp_path: Path, nested: bool) -> None:
    root = _make_vault(tmp_path / "repo", nested=nested)
    configured = [root, root / WIKI_ROOT]
    if nested:
        configured.append(root / WIKI_ROOT / "wiki")

    for path in configured:
        layout = layout_for(path)
        assert layout.repo_root == root, path
        assert layout.wiki_dir == root / WIKI_ROOT, path
        assert layout.is_initialized(), path
        assert layout.missing_entries() == [], path
        assert layout.page_dir("concepts").is_dir(), path


def test_nested_vault_reports_the_nested_page_prefix(tmp_path: Path) -> None:
    """Prompts and page paths must use the prefix that actually resolves."""
    root = _make_vault(tmp_path / "repo", nested=True)
    assert layout_for(root).pages_prefix == f"{WIKI_ROOT}/wiki"
    assert layout_for(tmp_path / "flat").pages_prefix == WIKI_ROOT


def test_partially_nested_vault_finds_both_placements(tmp_path: Path) -> None:
    """The live vault keeps raw/ beside llm-wiki/ and the rest under wiki/."""
    root = _make_vault(tmp_path / "repo", nested=True)
    stray = root / WIKI_ROOT / "raw"
    (root / WIKI_ROOT / "wiki" / "raw").rmdir()
    stray.mkdir(parents=True, exist_ok=True)

    layout = layout_for(root)
    assert layout.page_dir("raw") == stray
    assert layout.page_dir("concepts") == root / WIKI_ROOT / "wiki" / "concepts"
    assert layout.missing_entries() == []


def test_unscaffolded_path_is_returned_unchanged(tmp_path: Path) -> None:
    """``wiki init`` must still be able to scaffold into a fresh directory."""
    fresh = tmp_path / "brand-new"
    assert discover_repo_root(fresh) == fresh
    assert not layout_for(fresh).is_initialized()


def test_a_page_directory_name_does_not_hijack_discovery(tmp_path: Path) -> None:
    """Only ``llm-wiki`` anchors the walk up — an unrelated path stays put."""
    unrelated = tmp_path / "concepts"
    unrelated.mkdir()
    assert discover_repo_root(unrelated) == unrelated
