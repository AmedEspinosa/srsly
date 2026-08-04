"""Run supervisor — FR-20, FR-21, FR-31..FR-34, NFR-4, AC-4, AC-5.

Uses the host runner with stub harness executables so detachment, log tailing,
limit enforcement and reattach are exercised for real (real processes, real
files, real SIGKILL) without Docker or token spend.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
from pathlib import Path

import pytest

from workflow_orchestrator.config import Settings
from workflow_orchestrator.db import dispose_engine, init_engine, session_scope
from workflow_orchestrator.harness.base import HarnessOperation
from workflow_orchestrator.models import Base, Harness, Phase, Project, Run, RunStatus, Session
from workflow_orchestrator.runs.base import RunHandle, RunState
from workflow_orchestrator.runs.bus import reset_bus
from workflow_orchestrator.runs.host_runner import HostRunner
from workflow_orchestrator.runs.supervisor import (
    RunSupervisor,
    log_path_for,
    reset_supervisor,
)


def make_stub(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def claude_line(**payload: object) -> str:
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, **payload})


@pytest.fixture(autouse=True)
def _isolated_globals():
    reset_bus()
    reset_supervisor()
    yield
    reset_bus()
    reset_supervisor()


@pytest.fixture
async def db_ready(settings: Settings):
    engine = init_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await dispose_engine()


@pytest.fixture
async def fixture_session(db_ready, repo: Path, wiki_repo: Path) -> tuple[Project, Session]:
    async with session_scope() as db:
        project = Project(
            name="sup",
            repo_path=str(repo),
            wiki_repo_path=str(wiki_repo),
            wiki_super_summary_path="llm-wiki/super-summaries/sup.md",
        )
        db.add(project)
        await db.flush()
        session = Session(
            project_id=project.id,
            feature_prompt="do a thing",
            current_phase=Phase.IMPLEMENT.value,
            harness_implement=Harness.CLAUDE_CODE.value,
            harness_review=Harness.CODEX.value,
        )
        db.add(session)
        await db.flush()
        return project, session


def host_only(settings: Settings) -> Settings:
    settings.DOCKER_IMAGE_AGENT = None  # force the host runner
    settings.WORKFLOW_ALLOW_HOST_RUNNER = True
    settings.WORKFLOW_RUN_POLL_SECONDS = 1
    return settings


async def wait_for(predicate, timeout: float = 20.0, interval: float = 0.1) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await predicate() if asyncio.iscoroutinefunction(predicate) else predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def run_status(run_id: str) -> str:
    async with session_scope() as db:
        run = await db.get(Run, run_id)
        return run.status if run else "missing"


async def test_run_completes_and_persists_log_and_meters(
    settings: Settings, fixture_session, tmp_path: Path, repo: Path
) -> None:
    """FR-20 — output is persisted to .workflow/run-<id>.log."""
    project, session = fixture_session
    stub = make_stub(
        tmp_path / "claude-ok",
        "import sys, json\n"
        "sys.stdin.read()\n"
        f"print({json.dumps(claude_line(result='done', total_cost_usd=0.02, usage={'input_tokens': 5, 'output_tokens': 7}))})\n",
    )
    host_only(settings).WORKFLOW_CLAUDE_BIN = str(stub)

    supervisor = RunSupervisor(settings)
    run = await supervisor.start_run(
        session=session,
        worktree=repo,
        phase=Phase.IMPLEMENT,
        harness=Harness.CLAUDE_CODE,
        operation=HarnessOperation.IMPLEMENT,
        prompt="build it",
    )

    assert await wait_for(lambda: not supervisor.is_active(run.id)), "run never finished"
    assert await run_status(run.id) == RunStatus.COMPLETED.value

    log_path = log_path_for(repo, run.id)
    assert log_path.exists()
    assert "done" in log_path.read_text()

    async with session_scope() as db:
        stored = await db.get(Run, run.id)
        assert stored.cost_usd == pytest.approx(0.02)
        assert stored.tokens_used == 12
        assert stored.container_id.startswith("host:")  # FR-33
        assert stored.started_at and stored.ended_at


async def test_failing_harness_is_recorded_as_failed(
    settings: Settings, fixture_session, tmp_path: Path, repo: Path
) -> None:
    project, session = fixture_session
    stub = make_stub(
        tmp_path / "claude-fail",
        "import sys\nsys.stdin.read()\nsys.stderr.write('bad flag\\n')\nsys.exit(2)\n",
    )
    host_only(settings).WORKFLOW_CLAUDE_BIN = str(stub)

    supervisor = RunSupervisor(settings)
    run = await supervisor.start_run(
        session=session,
        worktree=repo,
        phase=Phase.IMPLEMENT,
        harness=Harness.CLAUDE_CODE,
        operation=HarnessOperation.IMPLEMENT,
        prompt="build it",
    )
    assert await wait_for(lambda: not supervisor.is_active(run.id))
    assert await run_status(run.id) == RunStatus.FAILED.value


async def test_ac5_cost_ceiling_terminates_the_run(
    settings: Settings, fixture_session, tmp_path: Path, repo: Path
) -> None:
    """AC-5 — WORKFLOW_RUN_COST_CEILING_USD=0.01 -> status cost_exceeded."""
    project, session = fixture_session
    # Reports an expensive cost, then sleeps so the supervisor must kill it.
    stub = make_stub(
        tmp_path / "claude-expensive",
        "import sys, time, json\n"
        "sys.stdin.read()\n"
        f"print({json.dumps(claude_line(result='partial', total_cost_usd=5.0))}, flush=True)\n"
        "time.sleep(120)\n",
    )
    settings = host_only(settings)
    settings.WORKFLOW_CLAUDE_BIN = str(stub)
    settings.WORKFLOW_RUN_COST_CEILING_USD = 0.01

    supervisor = RunSupervisor(settings)
    run = await supervisor.start_run(
        session=session,
        worktree=repo,
        phase=Phase.IMPLEMENT,
        harness=Harness.CLAUDE_CODE,
        operation=HarnessOperation.IMPLEMENT,
        prompt="build it",
    )

    assert await wait_for(lambda: not supervisor.is_active(run.id), timeout=30)
    assert await run_status(run.id) == RunStatus.COST_EXCEEDED.value

    # The container/process must actually be stopped, not merely marked.
    async with session_scope() as db:
        stored = await db.get(Run, run.id)
    info = await HostRunner(settings).poll(RunHandle(stored.container_id))
    assert info.state is not RunState.RUNNING


async def test_timeout_terminates_the_run(
    settings: Settings, fixture_session, tmp_path: Path, repo: Path
) -> None:
    """FR-21 — the wall-clock timeout is enforced."""
    project, session = fixture_session
    stub = make_stub(
        tmp_path / "claude-slow", "import sys, time\nsys.stdin.read()\ntime.sleep(120)\n"
    )
    settings = host_only(settings)
    settings.WORKFLOW_CLAUDE_BIN = str(stub)
    settings.WORKFLOW_RUN_TIMEOUT_MINUTES = 0  # immediate

    supervisor = RunSupervisor(settings)
    run = await supervisor.start_run(
        session=session,
        worktree=repo,
        phase=Phase.IMPLEMENT,
        harness=Harness.CLAUDE_CODE,
        operation=HarnessOperation.IMPLEMENT,
        prompt="build it",
    )
    assert await wait_for(lambda: not supervisor.is_active(run.id), timeout=30)
    assert await run_status(run.id) == RunStatus.TIMED_OUT.value


async def test_ac4_run_survives_supervisor_shutdown_and_reattaches(
    settings: Settings, fixture_session, tmp_path: Path, repo: Path
) -> None:
    """AC-4 / FR-31 / FR-32 — the harness outlives the supervisor and is reattached.

    ``shutdown()`` models the server going away: it stops supervising but must
    not kill the run. A fresh supervisor then reattaches by ``container_id``.
    """
    project, session = fixture_session
    marker = tmp_path / "still-running.txt"
    stub = make_stub(
        tmp_path / "claude-long",
        "import sys, time, json\n"
        "sys.stdin.read()\n"
        f"print({json.dumps(claude_line(result='first', total_cost_usd=0.01))}, flush=True)\n"
        "time.sleep(3)\n"
        f"open({json.dumps(str(marker))}, 'w').write('alive')\n"
        f"print({json.dumps(claude_line(result='second', total_cost_usd=0.02))}, flush=True)\n",
    )
    host_only(settings).WORKFLOW_CLAUDE_BIN = str(stub)

    first = RunSupervisor(settings)
    run = await first.start_run(
        session=session,
        worktree=repo,
        phase=Phase.IMPLEMENT,
        harness=Harness.CLAUDE_CODE,
        operation=HarnessOperation.IMPLEMENT,
        prompt="build it",
    )
    log_path = log_path_for(repo, run.id)
    assert await wait_for(lambda: log_path.exists() and "first" in log_path.read_text())

    # Server goes away mid-run.
    await first.shutdown()
    assert await run_status(run.id) == RunStatus.RUNNING.value

    # FR-31: the harness process is still alive.
    async with session_scope() as db:
        stored = await db.get(Run, run.id)
    assert (
        await HostRunner(settings).poll(RunHandle(stored.container_id))
    ).state is RunState.RUNNING

    # FR-32: a fresh supervisor picks it up.
    second = RunSupervisor(settings)
    reattached = await second.reattach_all()
    assert reattached[run.id] == "reattached"

    assert await wait_for(lambda: not second.is_active(run.id), timeout=30)
    assert marker.exists(), "the detached process was killed instead of surviving"
    assert await run_status(run.id) == RunStatus.COMPLETED.value

    # The replayed log must have rebuilt the meter totals, not lost them.
    async with session_scope() as db:
        final = await db.get(Run, run.id)
    assert final.cost_usd == pytest.approx(0.02)


async def test_reattach_reconciles_a_run_that_exited_while_down(
    settings: Settings, fixture_session, tmp_path: Path, repo: Path
) -> None:
    """A run that finished while the server was down is reconciled, not left running."""
    project, session = fixture_session
    stub = make_stub(
        tmp_path / "claude-quick",
        "import sys, json\n"
        "sys.stdin.read()\n"
        f"print({json.dumps(claude_line(result='done', total_cost_usd=0.03))})\n",
    )
    host_only(settings).WORKFLOW_CLAUDE_BIN = str(stub)

    first = RunSupervisor(settings)
    run = await first.start_run(
        session=session,
        worktree=repo,
        phase=Phase.IMPLEMENT,
        harness=Harness.CLAUDE_CODE,
        operation=HarnessOperation.IMPLEMENT,
        prompt="build it",
    )
    await first.shutdown()

    # Wait for the harness to actually exit, otherwise reattach would legitimately
    # find it running and this would test the wrong branch.
    from workflow_orchestrator.runs.host_runner import exit_path_for

    log_path = log_path_for(repo, run.id)
    assert await wait_for(lambda: exit_path_for(log_path).exists()), "stub never exited"
    assert "done" in log_path.read_text()

    # Force the row back to `running` to model a crash before the status write.
    async with session_scope() as db:
        stored = await db.get(Run, run.id)
        stored.status = RunStatus.RUNNING.value
        stored.ended_at = None

    second = RunSupervisor(settings)
    results = await second.reattach_all()
    assert "reconciled" in results[run.id]
    assert await run_status(run.id) == RunStatus.COMPLETED.value

    async with session_scope() as db:
        final = await db.get(Run, run.id)
    assert final.cost_usd == pytest.approx(0.03)


async def test_reattach_fails_runs_with_no_handle(
    settings: Settings, fixture_session, repo: Path
) -> None:
    project, session = fixture_session
    async with session_scope() as db:
        orphan = Run(
            session_id=session.id,
            phase=Phase.IMPLEMENT.value,
            harness=Harness.CLAUDE_CODE.value,
            status=RunStatus.RUNNING.value,
        )
        db.add(orphan)
        await db.flush()
        orphan_id = orphan.id

    results = await RunSupervisor(host_only(settings)).reattach_all()
    assert "no handle" in results[orphan_id]
    assert await run_status(orphan_id) == RunStatus.FAILED.value


async def test_no_runner_available_is_reported(
    settings: Settings, fixture_session, repo: Path
) -> None:
    """NFR-1 — refusing to run unsandboxed when the host runner is disabled."""
    from workflow_orchestrator.runs.supervisor import NoRunnerAvailable

    settings.DOCKER_IMAGE_AGENT = None
    settings.WORKFLOW_ALLOW_HOST_RUNNER = False

    project, session = fixture_session
    with pytest.raises(NoRunnerAvailable):
        await RunSupervisor(settings).start_run(
            session=session,
            worktree=repo,
            phase=Phase.IMPLEMENT,
            harness=Harness.CLAUDE_CODE,
            operation=HarnessOperation.IMPLEMENT,
            prompt="build it",
        )


async def test_host_runner_detects_pid_reuse(settings: Settings) -> None:
    """A recycled PID must not make a dead run look alive."""
    runner = HostRunner(settings)
    # A live PID (this process) recorded with a start time that cannot match.
    handle = RunHandle(f"host:{os.getpid()}:definitely_not_the_real_start_time")
    assert (await runner.poll(handle)).state is RunState.GONE


async def test_host_runner_reports_exited_for_dead_pid(settings: Settings) -> None:
    runner = HostRunner(settings)
    assert (await runner.poll(RunHandle("host:999999:x"))).state in (
        RunState.EXITED,
        RunState.GONE,
    )


async def test_malformed_handle_is_gone(settings: Settings) -> None:
    assert (await HostRunner(settings).poll(RunHandle("garbage"))).state is RunState.GONE
