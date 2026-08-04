"""Docker runner construction — NFR-1, OQ-2.

The Docker daemon is not running on this machine, so these assert the launch
command rather than executing it. The live path is covered by the manual
verification steps in the README; everything mechanical about the sandbox
contract is pinned here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from workflow_orchestrator.config import Settings
from workflow_orchestrator.runs.base import LaunchSpec
from workflow_orchestrator.runs.docker_runner import (
    CONTAINER_USER,
    CONTAINER_WORKDIR,
    DockerRunner,
)


@pytest.fixture
def spec(tmp_path: Path) -> LaunchSpec:
    worktree = tmp_path / "wt"
    (worktree / ".workflow").mkdir(parents=True)
    return LaunchSpec(
        argv=("claude", "-p", "--output-format", "stream-json"),
        worktree=worktree,
        log_path=worktree / ".workflow" / "run-abc.log",
        stdin="build it",
        forward_env=("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
    )


@pytest.fixture
def runner(settings: Settings) -> DockerRunner:
    settings.DOCKER_IMAGE_AGENT = "workflow-agent:local"
    return DockerRunner(settings)


def test_runs_detached(runner: DockerRunner, spec: LaunchSpec) -> None:
    """FR-31 — the run must outlive the server process."""
    argv = runner.build_argv(spec)
    assert argv[:3] == ["docker", "run", "--detach"]


def test_runs_as_non_root(runner: DockerRunner, spec: LaunchSpec) -> None:
    """NFR-1 — non-root user."""
    argv = runner.build_argv(spec)
    assert argv[argv.index("--user") + 1] == CONTAINER_USER
    assert CONTAINER_USER != "root"


def test_mounts_only_the_session_worktree(
    runner: DockerRunner, spec: LaunchSpec
) -> None:
    """NFR-1 — "no host path mounts other than the session worktree"."""
    argv = runner.build_argv(spec)
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a in ("--volume", "-v")]
    assert mounts == [f"{spec.worktree}:{CONTAINER_WORKDIR}"]
    assert "--mount" not in argv


def test_drops_capabilities_and_privilege_escalation(
    runner: DockerRunner, spec: LaunchSpec
) -> None:
    argv = runner.build_argv(spec)
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    assert "--privileged" not in argv


def test_forwards_only_declared_credential_names(
    runner: DockerRunner, spec: LaunchSpec
) -> None:
    """NFR-2 — names are forwarded, never values, and only what the adapter asked for."""
    argv = runner.build_argv(spec)
    envs = [argv[i + 1] for i, a in enumerate(argv) if a == "--env"]
    assert envs == ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"]
    # No "NAME=value" form, so no secret can appear in the command line.
    assert all("=" not in e for e in envs)


def test_bedrock_credentials_are_never_forwarded(
    runner: DockerRunner, spec: LaunchSpec
) -> None:
    """The sandbox has no use for Bedrock creds — the QA engine runs on the host."""
    argv = runner.build_argv(spec)
    rendered = " ".join(argv)
    for name in ("AWS_BEARER_TOKEN_BEDROCK", "AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID"):
        assert name not in rendered


def test_log_is_redirected_to_the_mounted_worktree(
    runner: DockerRunner, spec: LaunchSpec
) -> None:
    """FR-20 — the log lands on the host so the supervisor can tail it."""
    argv = runner.build_argv(spec)
    inner = argv[-1]
    assert f"{CONTAINER_WORKDIR}/.workflow/run-abc.log" in inner
    assert "2>&1" in inner
    # Host paths must not leak into the container's command.
    assert str(spec.worktree) not in inner


def test_prompt_is_piped_from_a_file_in_the_worktree(
    runner: DockerRunner, spec: LaunchSpec
) -> None:
    inner = runner.build_argv(spec)[-1]
    assert f"< {CONTAINER_WORKDIR}/.workflow/run-abc.prompt.txt" in inner


def test_image_comes_from_configuration(runner: DockerRunner, spec: LaunchSpec) -> None:
    """OQ-2 — the image is supplied via DOCKER_IMAGE_AGENT."""
    argv = runner.build_argv(spec)
    assert "workflow-agent:local" in argv
    assert argv.index("workflow-agent:local") == len(argv) - 4  # image, sh, -c, inner


async def test_unavailable_without_an_image(settings: Settings) -> None:
    settings.DOCKER_IMAGE_AGENT = None
    assert await DockerRunner(settings).available() is False


def test_dockerfile_declares_a_non_root_user() -> None:
    """The shipped image must satisfy NFR-1."""
    dockerfile = (
        Path(__file__).resolve().parents[1] / "docker" / "Dockerfile.agent"
    ).read_text()
    assert f"USER {CONTAINER_USER}" in dockerfile
    assert "useradd" in dockerfile
    # Both harness CLIs must be present, and their presence verified at build.
    assert "claude-code" in dockerfile
    assert "codex" in dockerfile
    assert "command -v claude" in dockerfile
