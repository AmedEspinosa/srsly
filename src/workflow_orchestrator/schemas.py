"""Pydantic request/response models for the API surface (SRS §4.3)."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .harness.registry import DEFAULT_IMPLEMENT_HARNESS, DEFAULT_REVIEW_HARNESS
from .models import Harness, Phase, RunStatus


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    repo_path: str
    wiki_repo_path: str
    wiki_super_summary_path: str

    @field_validator("repo_path", "wiki_repo_path")
    @classmethod
    def _absolute(cls, v: str) -> str:
        path = Path(v).expanduser()
        if not path.is_absolute():
            raise ValueError("must be an absolute path")
        return str(path)


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    repo_path: str
    wiki_repo_path: str
    wiki_super_summary_path: str
    created_at: str
    archived_at: str | None = None


class SessionCreate(BaseModel):
    feature_prompt: str = Field(min_length=1)
    # FR-8 default pairing. The constants live in the harness registry so this
    # layer never names a backend (AC-9).
    harness_implement: Harness = DEFAULT_IMPLEMENT_HARNESS
    harness_review: Harness = DEFAULT_REVIEW_HARNESS


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    feature_prompt: str
    current_phase: str
    harness_implement: str
    harness_review: str
    wiki_pages_injected: list[str] = Field(default_factory=list)
    created_at: str
    completed_at: str | None = None
    branch_name: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    pr_state: str | None = None

    @field_validator("wiki_pages_injected", mode="before")
    @classmethod
    def _parse_json(cls, v: object) -> object:
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
            except json.JSONDecodeError:
                return []
            return parsed if isinstance(parsed, list) else []
        return v


class ApprovalCreate(BaseModel):
    phase: Phase
    notes: str | None = None


class ApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    session_id: str
    phase: str
    artifact_id: str
    approved_by: str
    approved_at: str
    notes: str | None = None


class RejectionCreate(BaseModel):
    phase: Phase | None = None
    notes: str | None = None


class ArtifactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    session_id: str
    phase: str
    file_path: str
    created_at: str


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    session_id: str
    phase: str
    harness: str
    status: str
    container_id: str | None = None
    log_path: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    tokens_used: int = 0
    cost_usd: float = 0.0


class SessionDetail(SessionOut):
    artifacts: list[ArtifactOut] = Field(default_factory=list)
    approvals: list[ApprovalOut] = Field(default_factory=list)
    runs: list[RunOut] = Field(default_factory=list)
    worktree_path: str | None = None
    available_phases: list[str] = Field(default_factory=list)


class QaAnswer(BaseModel):
    question_id: str
    answer: str | list[str]


class QaSubmit(BaseModel):
    answers: list[QaAnswer] = Field(default_factory=list)
    end_early: bool = False


class QaQuestion(BaseModel):
    id: str
    text: str
    type: str = "text"
    options: list[str] = Field(default_factory=list)
    allow_custom: bool = True
    placeholder: str = ""


class QaState(BaseModel):
    session_id: str
    round: int
    max_rounds: int
    questions: list[QaQuestion] = Field(default_factory=list)
    ready: bool = False
    srs_written: bool = False
    awaiting_first_prompt: bool = False


class WikiReviewItem(BaseModel):
    page_path: str
    operation: str
    written_at: str
    session_id: str | None = None
    index_token_count: int | None = None
    diff: str | None = None


class ErrorPayload(BaseModel):
    error: str


__all__ = [
    "ApprovalCreate",
    "ApprovalOut",
    "ArtifactOut",
    "ErrorPayload",
    "Harness",
    "Phase",
    "ProjectCreate",
    "ProjectOut",
    "QaAnswer",
    "QaQuestion",
    "QaState",
    "QaSubmit",
    "RejectionCreate",
    "RunOut",
    "RunStatus",
    "SessionCreate",
    "SessionDetail",
    "SessionOut",
    "WikiReviewItem",
]
