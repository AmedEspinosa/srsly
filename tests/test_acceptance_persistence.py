"""AC-1 — session persistence across a SIGKILL + restart.

This deliberately spawns a real uvicorn subprocess and ``SIGKILL``s it. An
in-process fake cannot prove durability: the whole point of FR-6 is that state
survives a process that never got to run shutdown code.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.acceptance

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Server:
    def __init__(self, port: int, env: dict[str, str]) -> None:
        self.port = port
        self.env = env
        self.process: subprocess.Popen[bytes] | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "workflow_orchestrator.asgi:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--log-level",
                "warning",
            ],
            cwd=PROJECT_ROOT,
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.wait_ready()

    def wait_ready(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                stderr = (self.process.stderr.read() if self.process.stderr else b"").decode()
                raise RuntimeError(f"server exited early:\n{stderr}")
            try:
                response = httpx.get(f"{self.base_url}/healthz", timeout=1.0)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.15)
        raise TimeoutError("server did not become ready")

    def sigkill(self) -> None:
        """SIGKILL — no graceful shutdown, no chance to flush anything."""
        assert self.process is not None
        os.kill(self.process.pid, signal.SIGKILL)
        self.process.wait(timeout=10)

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                self.process.kill()


@pytest.fixture
def server(tmp_path: Path) -> Iterator[Server]:
    db_path = tmp_path / "db.sqlite3"
    env = {
        **os.environ,
        "WORKFLOW_DB_PATH": str(db_path),
        "WORKFLOW_LOG_DIR": str(tmp_path / "logs"),
        "PYTHONPATH": str(PROJECT_ROOT / "src"),
    }

    subprocess.run(
        [sys.executable, "-m", "workflow_orchestrator.cli", "db", "migrate"],
        cwd=PROJECT_ROOT,
        env=env,
        check=True,
        capture_output=True,
    )

    instance = Server(free_port(), env)
    try:
        instance.start()
        yield instance
    finally:
        instance.stop()


def test_ac1_session_survives_sigkill_and_restart(
    server: Server, repo: Path, wiki_repo: Path
) -> None:
    base = server.base_url

    # 1. Create session S1 in project P1.
    project = httpx.post(
        f"{base}/projects",
        json={
            "name": "ac1",
            "repo_path": str(repo),
            "wiki_repo_path": str(wiki_repo),
            "wiki_super_summary_path": "llm-wiki/super-summaries/ac1.md",
        },
        timeout=30.0,
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]

    created = httpx.post(
        f"{base}/projects/{project_id}/sessions",
        json={"feature_prompt": "AC-1 durability check"},
        timeout=30.0,
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    # 2. Advance S1 through qa -> srs and record the approval.
    srs = repo / "worktrees" / session_id / ".workflow" / "srs.md"
    srs.parent.mkdir(parents=True, exist_ok=True)
    srs.write_text("# SRS\n\nAC-1 fixture.\n", encoding="utf-8")

    approved = httpx.post(
        f"{base}/sessions/{session_id}/approve",
        json={"phase": "srs", "notes": "ac1"},
        timeout=30.0,
    )
    assert approved.status_code == 200, approved.text
    original_timestamp = approved.json()["approved_at"]
    original_id = approved.json()["id"]

    # 3. SIGKILL the server process.
    server.sigkill()
    with pytest.raises(httpx.HTTPError):
        httpx.get(f"{base}/healthz", timeout=1.0)

    # 4. Restart the server.
    server.start()

    # 5. GET /sessions/S1
    detail = httpx.get(f"{base}/sessions/{session_id}", timeout=30.0).json()

    # Expected: current_phase = "plan", approval for "srs" present with the
    # original timestamp.
    assert detail["current_phase"] == "plan"
    srs_approvals = [a for a in detail["approvals"] if a["phase"] == "srs"]
    assert len(srs_approvals) == 1
    assert srs_approvals[0]["approved_at"] == original_timestamp
    assert srs_approvals[0]["id"] == original_id
    assert srs_approvals[0]["notes"] == "ac1"


def test_ac1_wal_mode_is_enabled(server: Server, tmp_path: Path) -> None:
    """NFR-5 — WAL prevents reader/writer contention during log streaming."""
    import sqlite3

    db_path = Path(server.env["WORKFLOW_DB_PATH"])
    connection = sqlite3.connect(db_path)
    try:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()
    assert mode.lower() == "wal"
