"""Run supervisor — SRS §2.9 (FR-31..FR-34), FR-20, FR-21, NFR-4.

Responsibilities:

* Launch harness runs **detached**, so an application restart does not kill an
  in-flight run (FR-31).
* Persist ``container_id`` immediately, before any polling, so a crash in the
  gap between launch and first poll is still recoverable (FR-33).
* Poll each run at an interval no greater than 10 seconds (FR-34).
* Tail ``.workflow/run-<id>.log``, translate each line through the run's harness
  adapter, persist meter state, and publish to the SSE bus (FR-20).
* Terminate on timeout or cost ceiling and record the outcome (FR-21).
* On startup, reattach to every run still recorded as ``running`` (FR-32, NFR-4).

Reattach works because the log file is the source of truth: replaying it from
offset 0 rebuilds the event stream and the meter totals, after which following
the file continues live. Nothing depends on the original process's pipes
surviving.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from ..config import Settings
from ..db import session_scope
from ..harness import get_adapter
from ..harness.base import HarnessOperation, RunEvent
from ..logging import get_logger
from ..models import Harness, Phase, Run, RunStatus, Session, utcnow
from .base import LaunchSpec, RunHandle, RunnerBackend, RunState
from .bus import get_bus
from .docker_runner import DockerRunner
from .host_runner import HostRunner
from .meters import RunMeter

log = get_logger(__name__)

TAIL_POLL_SECONDS = 0.25


class SupervisorError(RuntimeError):
    pass


class NoRunnerAvailable(SupervisorError):
    pass


def log_path_for(worktree: Path, run_id: str) -> Path:
    """FR-20 — ``.workflow/run-<run_id>.log``."""
    return worktree / ".workflow" / f"run-{run_id}.log"


@dataclass
class ActiveRun:
    run_id: str
    handle: RunHandle
    meter: RunMeter
    task: asyncio.Task[None] | None = None
    backend: RunnerBackend | None = None
    events: list[RunEvent] = field(default_factory=list)


class RunSupervisor:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._active: dict[str, ActiveRun] = {}
        self._bus = get_bus()

    # --- backend selection ---------------------------------------------------

    async def select_backend(self) -> RunnerBackend:
        """Prefer the sandboxed Docker backend; fall back to host if allowed."""
        docker = DockerRunner(self._settings)
        if await docker.available():
            return docker  # type: ignore[return-value]

        host = HostRunner(self._settings)
        if await host.available():
            log.warning(
                "run.host_backend_selected",
                reason="docker unavailable or DOCKER_IMAGE_AGENT unset",
                note="container isolation (NFR-1) is not in effect",
            )
            return host  # type: ignore[return-value]

        raise NoRunnerAvailable(
            "Docker is unavailable (set DOCKER_IMAGE_AGENT and start the daemon) "
            "and the host runner is disabled by WORKFLOW_ALLOW_HOST_RUNNER=false"
        )

    def backend_for_handle(self, handle: RunHandle) -> RunnerBackend:
        if handle.backend == HostRunner.name:
            return HostRunner(self._settings)  # type: ignore[return-value]
        return DockerRunner(self._settings)  # type: ignore[return-value]

    def _new_meter(self, adapter: object) -> RunMeter:
        """FR-21 meter configured for this backend's cost-reporting semantics."""
        return RunMeter(
            timeout_seconds=self._settings.WORKFLOW_RUN_TIMEOUT_MINUTES * 60,
            cost_ceiling_usd=self._settings.WORKFLOW_RUN_COST_CEILING_USD,
            cost_is_cumulative=getattr(adapter, "cost_is_cumulative", False),
        )

    # --- launching -----------------------------------------------------------

    async def start_run(
        self,
        *,
        session: Session,
        worktree: Path,
        phase: Phase,
        harness: Harness,
        operation: HarnessOperation,
        prompt: str,
    ) -> Run:
        backend = await self.select_backend()
        adapter = get_adapter(harness, self._settings)

        async with session_scope() as db:
            run = Run(
                session_id=session.id,
                phase=phase.value,
                harness=harness.value,
                status=RunStatus.PENDING.value,
            )
            db.add(run)
            await db.flush()
            run_id = run.id

        log_path = log_path_for(worktree, run_id)
        spec = adapter.command(operation, worktree=worktree, prompt=prompt)
        launch = LaunchSpec(
            argv=spec.argv,
            worktree=worktree,
            log_path=log_path,
            stdin=spec.stdin,
            env=spec.env,
            forward_env=getattr(adapter, "credential_env", ()),
        )

        try:
            handle = await backend.launch(launch)
        except Exception as exc:
            await self._finish(run_id, RunStatus.FAILED, note=str(exc))
            raise SupervisorError(f"could not launch run: {exc}") from exc

        # FR-33 — persist the handle before anything can fail, so reattach can
        # find the process even if the server dies on the very next line.
        async with session_scope() as db:
            run = await db.get(Run, run_id)
            if run is not None:
                run.container_id = handle.value
                run.log_path = str(log_path)
                run.status = RunStatus.RUNNING.value
                run.started_at = utcnow()

        meter = self._new_meter(adapter)
        active = ActiveRun(run_id=run_id, handle=handle, meter=meter, backend=backend)
        self._active[run_id] = active
        active.task = asyncio.create_task(
            self._supervise(active, adapter, log_path, from_start=True)
        )

        log.info(
            "run.started",
            run_id=run_id,
            session_id=session.id,
            phase=phase.value,
            harness=harness.value,
            backend=backend.name,
            handle=handle.value[:20],
        )

        async with session_scope() as db:
            return await db.get(Run, run_id)  # type: ignore[return-value]

    # --- supervision loop ----------------------------------------------------

    async def _supervise(
        self,
        active: ActiveRun,
        adapter: object,
        log_path: Path,
        *,
        from_start: bool,
    ) -> None:
        backend = active.backend or self.backend_for_handle(active.handle)
        offset = 0
        pending = b""
        poll_interval = self._settings.WORKFLOW_RUN_POLL_SECONDS  # FR-34, <= 10s
        last_poll = 0.0
        final_status = RunStatus.COMPLETED
        note = ""

        loop = asyncio.get_running_loop()

        try:
            while True:
                # --- drain new log bytes -------------------------------------
                if log_path.exists():
                    size = log_path.stat().st_size
                    if size > offset:
                        with log_path.open("rb") as handle_file:
                            handle_file.seek(offset)
                            chunk = handle_file.read(size - offset)
                            offset = size
                        pending += chunk
                        *complete, pending = pending.split(b"\n")
                        for raw in complete:
                            event = adapter.parse_line(  # type: ignore[attr-defined]
                                raw.decode("utf-8", errors="replace")
                            )
                            if event is None:
                                continue
                            active.events.append(event)
                            active.meter.observe(event)
                            self._bus.publish(active.run_id, event)

                # --- enforce limits (FR-21) ----------------------------------
                breach = active.meter.check()
                if breach is not None:
                    log.warning(
                        "run.limit_exceeded",
                        run_id=active.run_id,
                        status=breach.status.value,
                        reason=breach.reason,
                    )
                    self._bus.publish(
                        active.run_id,
                        RunEvent(event_type="error", text=breach.reason),
                    )
                    await backend.terminate(active.handle)
                    final_status = breach.status
                    note = breach.reason
                    break

                # --- poll liveness (FR-34) -----------------------------------
                now = loop.time()
                if now - last_poll >= poll_interval:
                    last_poll = now
                    info = await backend.poll(active.handle, log_path)
                    if info.state is not RunState.RUNNING:
                        # Drain whatever the process wrote as it exited.
                        await asyncio.sleep(TAIL_POLL_SECONDS)
                        if log_path.exists() and log_path.stat().st_size > offset:
                            continue
                        if info.state is RunState.GONE:
                            final_status = RunStatus.FAILED
                            note = "process handle no longer resolvable"
                        elif info.exit_code:
                            final_status = RunStatus.FAILED
                            note = f"exited with code {info.exit_code}"
                        else:
                            final_status = RunStatus.COMPLETED
                        break

                await asyncio.sleep(TAIL_POLL_SECONDS)

        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception as exc:  # pragma: no cover - defensive
            log.error("run.supervise_failed", run_id=active.run_id, error=str(exc))
            final_status = RunStatus.FAILED
            note = str(exc)

        if any(e.event_type == "error" for e in active.events) and final_status is RunStatus.COMPLETED:
            final_status = RunStatus.FAILED
            note = note or "harness reported an error"

        await self._finish(
            active.run_id,
            final_status,
            note=note,
            tokens_used=active.meter.tokens_used,
            cost_usd=active.meter.cost_usd,
        )
        self._bus.close(active.run_id)
        self._active.pop(active.run_id, None)

    async def _finish(
        self,
        run_id: str,
        status: RunStatus,
        *,
        note: str = "",
        tokens_used: int | None = None,
        cost_usd: float | None = None,
    ) -> None:
        async with session_scope() as db:
            run = await db.get(Run, run_id)
            if run is None:  # pragma: no cover
                return
            run.status = status.value
            run.ended_at = utcnow()
            if tokens_used is not None:
                run.tokens_used = tokens_used
            if cost_usd is not None:
                run.cost_usd = cost_usd
        log.info(
            "run.finished",
            run_id=run_id,
            status=status.value,
            note=note,
            tokens_used=tokens_used,
            cost_usd=cost_usd,
        )

    # --- reattach (FR-32, NFR-4) ---------------------------------------------

    async def reattach_all(self) -> dict[str, str]:
        """Resume supervision of every run still marked ``running``."""
        results: dict[str, str] = {}

        async with session_scope() as db:
            rows = (
                await db.execute(
                    select(Run).where(Run.status == RunStatus.RUNNING.value)
                )
            ).scalars().all()
            candidates = [
                (r.id, r.container_id, r.log_path, r.harness, r.tokens_used, r.cost_usd)
                for r in rows
            ]

        for run_id, container_id, log_path_str, harness, tokens, cost in candidates:
            if run_id in self._active:
                results[run_id] = "already supervised"
                continue
            if not container_id or not log_path_str:
                await self._finish(run_id, RunStatus.FAILED, note="no handle recorded")
                results[run_id] = "failed (no handle)"
                continue

            handle = RunHandle(container_id)
            backend = self.backend_for_handle(handle)
            info = await backend.poll(handle, Path(log_path_str))

            log_path = Path(log_path_str)
            adapter = get_adapter(Harness(harness), self._settings)

            if info.state is RunState.RUNNING:
                meter = self._new_meter(adapter)
                meter.resume_from(tokens_used=tokens or 0, cost_usd=cost or 0.0)
                active = ActiveRun(
                    run_id=run_id, handle=handle, meter=meter, backend=backend
                )
                self._active[run_id] = active
                # Replay the persisted log from the beginning so a reconnecting
                # UI sees the whole run, not just what arrives from now on.
                self._bus.reset(run_id)
                active.task = asyncio.create_task(
                    self._supervise(active, adapter, log_path, from_start=True)
                )
                results[run_id] = "reattached"
                log.info("run.reattached", run_id=run_id, handle=container_id[:20])
            else:
                # Exited while we were down: replay the log to recover events and
                # meter totals, then record the terminal status.
                meter = self._new_meter(adapter)
                meter.resume_from(tokens_used=tokens or 0, cost_usd=cost or 0.0)
                events = self._replay(log_path, adapter, meter, run_id)
                status = RunStatus.COMPLETED
                if info.state is RunState.GONE and not events:
                    status = RunStatus.FAILED
                elif info.exit_code:
                    status = RunStatus.FAILED
                elif any(e.event_type == "error" for e in events):
                    status = RunStatus.FAILED
                await self._finish(
                    run_id,
                    status,
                    note="reconciled on startup",
                    tokens_used=meter.tokens_used,
                    cost_usd=meter.cost_usd,
                )
                self._bus.close(run_id)
                results[run_id] = f"reconciled ({status.value})"
                log.info("run.reconciled", run_id=run_id, status=status.value)

        return results

    def _replay(
        self, log_path: Path, adapter: object, meter: RunMeter, run_id: str
    ) -> list[RunEvent]:
        if not log_path.exists():
            return []
        events: list[RunEvent] = []
        for raw in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            event = adapter.parse_line(raw)  # type: ignore[attr-defined]
            if event is None:
                continue
            events.append(event)
            meter.observe(event)
            self._bus.publish(run_id, event)
        return events

    # --- introspection / control ---------------------------------------------

    def is_active(self, run_id: str) -> bool:
        return run_id in self._active

    def meter_for(self, run_id: str) -> RunMeter | None:
        active = self._active.get(run_id)
        return active.meter if active else None

    async def cancel(self, run_id: str) -> bool:
        active = self._active.get(run_id)
        if active is None:
            return False
        backend = active.backend or self.backend_for_handle(active.handle)
        await backend.terminate(active.handle)
        return True

    async def shutdown(self) -> None:
        """Stop supervising without killing the runs themselves (FR-31)."""
        for active in list(self._active.values()):
            if active.task is not None:
                active.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await active.task
        self._active.clear()


_supervisor: RunSupervisor | None = None


def get_supervisor(settings: Settings) -> RunSupervisor:
    global _supervisor
    if _supervisor is None:
        _supervisor = RunSupervisor(settings)
    return _supervisor


def reset_supervisor() -> None:
    """Test hook."""
    global _supervisor
    _supervisor = None
