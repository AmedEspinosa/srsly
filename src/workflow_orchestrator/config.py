"""Application configuration — SRS §4.1 environment variable table.

Credentials are read from the environment only. Nothing in this module is ever
persisted to the database (NFR-2).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_HOME = Path.home() / ".workflow-orchestrator"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=True)

    # --- Bedrock auth (NFR-2) -------------------------------------------------
    # Two credential shapes are supported. The bearer token is checked first
    # because it is what the existing Neovim provider uses; boto3's standard
    # chain (keys or profile) is the fallback.
    AWS_BEARER_TOKEN_BEDROCK: str | None = None
    AWS_ACCESS_KEY_ID: str | None = None
    AWS_SECRET_ACCESS_KEY: str | None = None
    AWS_SESSION_TOKEN: str | None = None
    AWS_PROFILE: str | None = None
    AWS_REGION: str = "us-east-1"

    # --- Bedrock models -------------------------------------------------------
    # SRS §2.3 names Sonnet 5 for the reasoning path, but that model is not
    # enabled on this account (Converse returns AccessDeniedException:
    # "anthropic.claude-sonnet-5 is not available for this account"). The default
    # is therefore the model the existing Neovim provider already runs on, which
    # is verified working. Override once Sonnet 5 access is granted:
    #     WORKFLOW_BEDROCK_MODEL_REASONING=us.anthropic.claude-sonnet-5
    WORKFLOW_BEDROCK_MODEL_REASONING: str = "us.anthropic.claude-sonnet-4-6"
    WORKFLOW_BEDROCK_MODEL_BUDGET: str = "amazon.nova-lite-v1:0"

    # --- Paths ----------------------------------------------------------------
    WORKFLOW_DB_PATH: Path = DEFAULT_HOME / "db.sqlite3"
    WORKFLOW_LOG_DIR: Path = DEFAULT_HOME / "logs"

    # --- Server ---------------------------------------------------------------
    WORKFLOW_HOST: str = "127.0.0.1"
    WORKFLOW_PORT: int = 8000

    # --- Run limits (FR-21) ---------------------------------------------------
    WORKFLOW_RUN_TIMEOUT_MINUTES: int = 90
    WORKFLOW_RUN_COST_CEILING_USD: float = 5.00
    WORKFLOW_RUN_POLL_SECONDS: int = 5  # FR-34 requires <= 10

    # --- Sandbox --------------------------------------------------------------
    DOCKER_IMAGE_AGENT: str | None = None
    # When Docker is unavailable the supervisor falls back to detached host
    # subprocesses. This drops NFR-1 container isolation, so it is opt-in.
    WORKFLOW_ALLOW_HOST_RUNNER: bool = True

    # --- Harness / wiki CLIs --------------------------------------------------
    WORKFLOW_CLAUDE_BIN: str = "claude"
    WORKFLOW_CODEX_BIN: str = "codex"
    WORKFLOW_GH_BIN: str = "gh"
    WORKFLOW_GIT_BIN: str = "git"

    # --- Merge polling (OQ-3) -------------------------------------------------
    WORKFLOW_MERGE_POLL_SECONDS: int = 60

    @field_validator("WORKFLOW_HOST")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        """NFR-3: the server must not be reachable off the loopback interface."""
        if v not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(
                f"WORKFLOW_HOST must be a loopback address (NFR-3), got {v!r}"
            )
        return v

    @field_validator("WORKFLOW_RUN_POLL_SECONDS")
    @classmethod
    def _poll_ceiling(cls, v: int) -> int:
        if not 1 <= v <= 10:
            raise ValueError("WORKFLOW_RUN_POLL_SECONDS must be between 1 and 10 (FR-34)")
        return v

    @field_validator("WORKFLOW_DB_PATH", "WORKFLOW_LOG_DIR", mode="before")
    @classmethod
    def _expand(cls, v: object) -> object:
        if isinstance(v, str):
            return Path(v).expanduser()
        return v

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.WORKFLOW_DB_PATH}"

    @property
    def log_file(self) -> Path:
        return self.WORKFLOW_LOG_DIR / "app.log"

    def ensure_dirs(self) -> None:
        self.WORKFLOW_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.WORKFLOW_LOG_DIR.mkdir(parents=True, exist_ok=True)

    def bedrock_credential_mode(self) -> str:
        """Which credential shape will be used. Purely for logging/diagnostics."""
        if self.AWS_BEARER_TOKEN_BEDROCK:
            return "bearer_token"
        if self.AWS_ACCESS_KEY_ID and self.AWS_SECRET_ACCESS_KEY:
            return "static_keys"
        if self.AWS_PROFILE:
            return "profile"
        return "default_chain"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Test hook — settings are cached for the process lifetime otherwise."""
    get_settings.cache_clear()


def settings_from_env(**overrides: object) -> Settings:
    """Build a Settings instance without touching the process-wide cache."""
    env = {k: v for k, v in os.environ.items()}
    env.update({k: str(v) for k, v in overrides.items()})
    return Settings(**{k: v for k, v in env.items() if k in Settings.model_fields})
