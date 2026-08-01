"""Small async subprocess helper shared by the git, harness and wiki services."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def check(self) -> CommandResult:
        if not self.ok:
            raise CommandError(self)
        return self


class CommandError(RuntimeError):
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        rendered = " ".join(result.argv)
        detail = (result.stderr or result.stdout).strip()
        super().__init__(f"command failed ({result.returncode}): {rendered}\n{detail}")


async def run_command(
    argv: list[str] | tuple[str, ...],
    *,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
    timeout: float | None = 120.0,
) -> CommandResult:
    """Run ``argv`` to completion and capture its output."""
    merged_env = {**os.environ, **(env or {})}
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd) if cwd is not None else None,
        env=merged_env,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        raw_out, raw_err = await asyncio.wait_for(
            process.communicate(stdin.encode() if stdin is not None else None),
            timeout=timeout,
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    return CommandResult(
        argv=tuple(argv),
        returncode=process.returncode or 0,
        stdout=(raw_out or b"").decode("utf-8", errors="replace"),
        stderr=(raw_err or b"").decode("utf-8", errors="replace"),
    )
