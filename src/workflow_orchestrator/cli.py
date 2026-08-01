"""CLI entrypoint — SRS §4.2.

    workflow-orchestrator serve          # start the FastAPI server
    workflow-orchestrator db migrate     # run pending Alembic migrations
    workflow-orchestrator db reset       # drop and recreate schema (dev only)
    workflow-orchestrator run reattach   # manually trigger supervisor reattach
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from .config import get_settings
from .logging import configure_logging, get_logger

app = typer.Typer(
    name="workflow-orchestrator",
    help="AI-driven development workflow orchestrator",
    no_args_is_help=True,
    add_completion=False,
)
db_app = typer.Typer(help="Database maintenance", no_args_is_help=True)
run_app = typer.Typer(help="Run supervisor controls", no_args_is_help=True)
wiki_app = typer.Typer(help="Wiki repository helpers", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(run_app, name="run")
app.add_typer(wiki_app, name="wiki")

log = get_logger(__name__)

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def _alembic_config():
    from alembic.config import Config

    settings = get_settings()
    settings.ensure_dirs()
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_INI.parent / "migrations"))
    # Alembic runs synchronously; strip the async driver from the URL.
    config.set_main_option("sqlalchemy.url", f"sqlite:///{settings.WORKFLOW_DB_PATH}")
    return config


@app.command()
def serve(
    host: str | None = typer.Option(None, help="Override WORKFLOW_HOST (loopback only)"),
    port: int | None = typer.Option(None, help="Override WORKFLOW_PORT"),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes"),
) -> None:
    """Start the FastAPI server (binds to loopback only — NFR-3)."""
    import uvicorn

    settings = get_settings()
    settings.ensure_dirs()
    configure_logging(settings.log_file)

    bind_host = host or settings.WORKFLOW_HOST
    if bind_host not in {"127.0.0.1", "localhost", "::1"}:
        typer.secho(
            f"refusing to bind to {bind_host!r}: loopback only (NFR-3)",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=2)

    typer.secho(
        f"→ http://{bind_host}:{port or settings.WORKFLOW_PORT}", fg=typer.colors.GREEN
    )
    uvicorn.run(
        "workflow_orchestrator.asgi:app",
        host=bind_host,
        port=port or settings.WORKFLOW_PORT,
        reload=reload,
        log_config=None,
    )


@db_app.command("migrate")
def db_migrate() -> None:
    """Run pending Alembic migrations."""
    from alembic import command

    settings = get_settings()
    configure_logging(settings.log_file, console=True)
    command.upgrade(_alembic_config(), "head")
    typer.secho(f"schema up to date: {settings.WORKFLOW_DB_PATH}", fg=typer.colors.GREEN)


@db_app.command("revision")
def db_revision(
    message: str = typer.Option(..., "-m", "--message"),
    autogenerate: bool = typer.Option(True, "--autogenerate/--empty"),
) -> None:
    """Create a new migration revision (development helper)."""
    from alembic import command

    command.revision(_alembic_config(), message=message, autogenerate=autogenerate)


@db_app.command("reset")
def db_reset(
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt"),
) -> None:
    """Drop and recreate the schema. Development only — destroys all state."""
    settings = get_settings()
    if not yes:
        typer.secho(
            f"This will DELETE every project, session, approval and run in\n"
            f"  {settings.WORKFLOW_DB_PATH}",
            fg=typer.colors.YELLOW,
        )
        typer.confirm("Continue?", abort=True)

    configure_logging(settings.log_file)

    async def _reset() -> None:
        from .db import build_engine, create_all, drop_all

        engine = build_engine(settings)
        try:
            await drop_all(engine)
            await create_all(engine)
        finally:
            await engine.dispose()

    asyncio.run(_reset())
    # Keep Alembic's bookkeeping consistent with the freshly built schema.
    from alembic import command

    command.stamp(_alembic_config(), "head")
    typer.secho("schema reset", fg=typer.colors.GREEN)


@run_app.command("reattach")
def run_reattach() -> None:
    """Reattach the supervisor to any runs still recorded as running (FR-32)."""
    settings = get_settings()
    configure_logging(settings.log_file)

    async def _reattach() -> None:
        from .db import dispose_engine, init_engine
        from .runs.supervisor import get_supervisor

        init_engine(settings)
        try:
            supervisor = get_supervisor(settings)
            reattached = await supervisor.reattach_all()
            for run_id, state in reattached.items():
                typer.echo(f"{run_id}: {state}")
            if not reattached:
                typer.echo("no runs to reattach")
        finally:
            await dispose_engine()

    asyncio.run(_reattach())


@wiki_app.command("init")
def wiki_init(
    path: Path = typer.Argument(..., help="Path to the consolidated wiki repo"),
    seed_from: Path | None = typer.Option(
        None, "--seed-from", help="Existing vault to copy schema.md / AGENTS.md from"
    ),
) -> None:
    """Scaffold a consolidated wiki repo with the SRS §4.5 layout."""
    from .librarian.scaffold import init_wiki_repo

    created = init_wiki_repo(path.expanduser(), seed_from=seed_from)
    for entry in created:
        typer.echo(f"created {entry}")
    typer.secho(f"wiki repo ready: {path}", fg=typer.colors.GREEN)


@app.command()
def version() -> None:
    """Print the installed version."""
    from importlib.metadata import version as pkg_version

    try:
        typer.echo(pkg_version("workflow-orchestrator"))
    except Exception:  # pragma: no cover - editable installs before build
        typer.echo("0.1.0")


if __name__ == "__main__":  # pragma: no cover
    app()
