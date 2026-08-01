"""FastAPI application factory.

NFR-3: the server binds to loopback only; that is enforced in
:mod:`workflow_orchestrator.config` and honoured by the CLI's ``serve`` command.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import Settings, get_settings
from .db import apply_sqlite_pragmas, create_all, dispose_engine, init_engine
from .logging import configure_logging, get_logger

log = get_logger(__name__)

WEB_DIR = Path(__file__).parent / "web"
STATIC_DIR = WEB_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    engine = init_engine(settings)
    if getattr(app.state, "create_schema", False):
        await create_all(engine)
    journal_mode = await apply_sqlite_pragmas(engine)

    log.info(
        "app.started",
        host=settings.WORKFLOW_HOST,
        port=settings.WORKFLOW_PORT,
        db_path=str(settings.WORKFLOW_DB_PATH),
        journal_mode=journal_mode,
        bedrock_credential_mode=settings.bedrock_credential_mode(),
    )

    # FR-32 / NFR-4 — resume supervision of any run still marked `running`.
    # A failure here must not stop the server from booting: the operator needs
    # the UI precisely when something has gone wrong with a run.
    from .runs.supervisor import get_supervisor

    supervisor = get_supervisor(settings)
    app.state.supervisor = supervisor
    try:
        reattached = await supervisor.reattach_all()
        if reattached:
            log.info("app.runs_reattached", runs=reattached)
    except Exception as exc:  # pragma: no cover - defensive
        log.error("app.reattach_failed", error=str(exc))

    # FR-42 — the single consumer that serialises every wiki write.
    from .librarian.queue import get_queue

    wiki_queue = get_queue()
    wiki_queue.start()
    app.state.wiki_queue = wiki_queue

    # FR-29 / OQ-3 — poll open PRs for merge at 60-second intervals.
    from .api.merge import MergePoller

    poller = MergePoller(settings)
    poller.start()
    app.state.merge_poller = poller

    try:
        yield
    finally:
        log.info("app.stopping")
        await poller.stop()
        await wiki_queue.stop()
        # Stop supervising, but leave the runs themselves alive (FR-31).
        await supervisor.shutdown()
        await dispose_engine()


def create_app(settings: Settings | None = None, *, create_schema: bool = False) -> FastAPI:
    settings = settings or get_settings()
    settings.ensure_dirs()
    configure_logging(settings.log_file)

    app = FastAPI(
        title="AI-Driven Development Workflow Orchestrator",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.create_schema = create_schema

    from .api import merge, plan, projects, qa, review, runs, sessions, wiki

    app.include_router(projects.router)
    app.include_router(sessions.router)
    app.include_router(qa.router)
    app.include_router(plan.router)
    app.include_router(runs.router)
    app.include_router(review.router)
    app.include_router(merge.router)
    app.include_router(wiki.router)

    from .web import routes as web_routes

    app.include_router(web_routes.router)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
