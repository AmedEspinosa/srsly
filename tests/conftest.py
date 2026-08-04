from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from workflow_orchestrator.app import create_app
from workflow_orchestrator.config import Settings
from workflow_orchestrator.db import dispose_engine


def git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(cwd),
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )


@pytest.fixture(autouse=True)
def _reset_module_singletons():
    """Isolate process-wide state between tests.

    The event bus, run supervisor and wiki write queue are singletons that hold
    asyncio primitives bound to the loop that created them. pytest-asyncio gives
    each test a fresh loop, so leaking them across tests produces
    "attached to a different loop" errors in teardown.
    """
    from workflow_orchestrator.librarian.queue import reset_queue
    from workflow_orchestrator.runs.bus import reset_bus
    from workflow_orchestrator.runs.supervisor import reset_supervisor

    reset_bus()
    reset_supervisor()
    reset_queue()
    yield
    reset_bus()
    reset_supervisor()
    reset_queue()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repository with one commit — worktrees need a HEAD to branch from."""
    root = tmp_path / "repo"
    root.mkdir()
    git("init", "-b", "main", cwd=root)
    (root / "README.md").write_text("# fixture repo\n", encoding="utf-8")
    git("add", "-A", cwd=root)
    git("commit", "-m", "initial", cwd=root)
    return root


@pytest.fixture
def wiki_repo(tmp_path: Path) -> Path:
    root = tmp_path / "wiki"
    (root / "llm-wiki").mkdir(parents=True)
    return root


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        WORKFLOW_DB_PATH=tmp_path / "db.sqlite3",
        WORKFLOW_LOG_DIR=tmp_path / "logs",
        AWS_BEARER_TOKEN_BEDROCK="test-token",
        AWS_REGION="us-east-1",
    )


@pytest_asyncio.fixture
async def db_engine(settings: Settings):
    """A live database for tests that use ``session_scope()`` without the app."""
    from workflow_orchestrator.db import init_engine
    from workflow_orchestrator.models import Base

    engine = init_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await dispose_engine()


@pytest_asyncio.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(settings, create_schema=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        async with app.router.lifespan_context(app):
            yield ac
    await dispose_engine()


@pytest_asyncio.fixture
async def project(client: AsyncClient, repo: Path, wiki_repo: Path) -> dict:
    response = await client.post(
        "/projects",
        json={
            "name": "fixture",
            "repo_path": str(repo),
            "wiki_repo_path": str(wiki_repo),
            "wiki_super_summary_path": "llm-wiki/super-summaries/fixture.md",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest_asyncio.fixture
async def session(client: AsyncClient, project: dict) -> dict:
    response = await client.post(
        f"/projects/{project['id']}/sessions",
        json={"feature_prompt": "Rebuild auth to use Auth0"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def worktree_for(repo: Path, session_id: str) -> Path:
    return repo / "worktrees" / session_id


def write_artifact(repo: Path, session_id: str, name: str, content: str = "# artifact\n") -> Path:
    path = worktree_for(repo, session_id) / ".workflow" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path
