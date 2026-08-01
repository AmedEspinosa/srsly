"""AC-9 — no backend-specific logic outside the harness adapter modules.

    "No backend-specific logic exists outside adapter/claude_code.py and
     adapter/codex.py (verified by grep/AST check in CI)."

The check is AST-based rather than a plain grep so that a mention inside a
docstring or comment does not count as logic, while a real string literal or
attribute reference does.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "workflow_orchestrator"

BACKEND_TOKENS = ("claude_code", "codex", "claude-code")

#: Packages permitted to name a backend, with the reason each is legitimate.
ALLOWED_PREFIXES: dict[str, str] = {
    # The harness package *is* the adapter layer AC-9 carves out.
    "harness/": "the adapter layer itself",
    # SRS §1.4 wires the Librarian directly to the Codex CLI: the `wiki-*`
    # command vocabulary is defined in the wiki repo's AGENTS.md, which is a
    # Codex instruction file. This is a separate concern from the plan/
    # implement/review adapter abstraction AC-9 governs.
    "librarian/": "wiki-* is a Codex command vocabulary (SRS §1.4, AGENTS.md)",
}

#: Individual files permitted to name a backend.
ALLOWED: dict[str, str] = {
    "models.py": "the Harness enum defines the persisted values",
    "config.py": "CLI binary paths are configuration, not logic",
}


def is_allowed(rel: str) -> bool:
    return rel in ALLOWED or any(rel.startswith(p) for p in ALLOWED_PREFIXES)


def python_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def relative(path: Path) -> str:
    return path.relative_to(SRC).as_posix()


def backend_references(path: Path) -> list[str]:
    """Backend names appearing as code, not as prose."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    # Docstrings are prose; drop them before walking.
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))

    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            lowered = node.value.lower()
            for token in BACKEND_TOKENS:
                if token in lowered:
                    hits.append(f"string {node.value[:60]!r} (line {node.lineno})")
                    break
        elif isinstance(node, ast.Name | ast.Attribute):
            name = node.id if isinstance(node, ast.Name) else node.attr
            lowered = name.lower()
            for token in BACKEND_TOKENS:
                if token in lowered:
                    hits.append(f"name {name!r} (line {node.lineno})")
                    break
    return hits


def test_no_backend_specific_logic_outside_adapters() -> None:
    offenders: dict[str, list[str]] = {}
    for path in python_files():
        rel = relative(path)
        if is_allowed(rel):
            continue
        hits = backend_references(path)
        if hits:
            offenders[rel] = hits

    assert offenders == {}, (
        "Backend-specific logic leaked outside the harness adapters (AC-9):\n"
        + "\n".join(f"  {file}: {hits}" for file, hits in offenders.items())
    )


def test_allowlist_entries_all_exist() -> None:
    """A stale allow-list would silently widen the check."""
    missing = [name for name in ALLOWED if not (SRC / name).exists()]
    assert missing == [], f"allow-listed files that no longer exist: {missing}"


def test_adapters_are_reached_only_through_the_registry() -> None:
    """Nothing outside the harness package may import an adapter class directly."""
    offenders: list[str] = []
    for path in python_files():
        rel = relative(path)
        if rel.startswith("harness/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.endswith(("harness.claude_code", "harness.codex")):
                    offenders.append(f"{rel} imports {node.module}")
    assert offenders == [], "\n".join(offenders)


@pytest.mark.parametrize("operation", ["plan", "implement", "review"])
def test_both_adapters_implement_the_protocol(operation: str) -> None:
    """§4.4 — both backends expose the same operation surface."""
    from workflow_orchestrator.harness.claude_code import ClaudeCodeAdapter
    from workflow_orchestrator.harness.codex import CodexAdapter

    for adapter_cls in (ClaudeCodeAdapter, CodexAdapter):
        assert callable(getattr(adapter_cls, operation)), adapter_cls.__name__
