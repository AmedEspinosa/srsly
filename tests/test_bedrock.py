"""Bedrock Converse shapes and credential resolution — FR-10, NFR-2."""

from __future__ import annotations

import pytest

from workflow_orchestrator.config import Settings
from workflow_orchestrator.services.bedrock import (
    BedrockError,
    Message,
    build_request,
    parse_response,
)


def test_request_separates_system_from_messages() -> None:
    """Matches bedrock_provider.lua: system is its own top-level block list."""
    body = build_request(
        [Message(role="user", text="hello"), Message(role="assistant", text="hi")],
        system="You are helpful.",
        max_tokens=4096,
        temperature=0.2,
    )
    assert body["system"] == [{"text": "You are helpful."}]
    assert body["messages"] == [
        {"role": "user", "content": [{"text": "hello"}]},
        {"role": "assistant", "content": [{"text": "hi"}]},
    ]
    assert body["inferenceConfig"] == {"maxTokens": 4096, "temperature": 0.2}


def test_request_omits_system_when_absent() -> None:
    body = build_request([Message(role="user", text="hi")])
    assert "system" not in body


def test_request_drops_blank_messages() -> None:
    body = build_request(
        [Message(role="user", text="  "), Message(role="user", text="real")]
    )
    assert len(body["messages"]) == 1
    assert body["messages"][0]["content"][0]["text"] == "real"


def test_parse_concatenates_text_blocks() -> None:
    result = parse_response(
        {
            "output": {"message": {"content": [{"text": "part one "}, {"text": "part two"}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 12, "outputTokens": 34, "totalTokens": 46},
        }
    )
    assert result.text == "part one part two"
    assert result.stop_reason == "end_turn"
    assert result.input_tokens == 12
    assert result.output_tokens == 34
    assert result.total_tokens == 46


def test_parse_ignores_non_text_blocks() -> None:
    result = parse_response(
        {"output": {"message": {"content": [{"text": "keep"}, {"image": {}}]}}}
    )
    assert result.text == "keep"


def test_parse_raises_on_error_envelope() -> None:
    with pytest.raises(BedrockError, match="throttled"):
        parse_response({"error": {"message": "throttled"}})

    with pytest.raises(BedrockError, match="bad model"):
        parse_response({"message": "bad model"})


def test_parse_handles_missing_usage() -> None:
    result = parse_response({"output": {"message": {"content": [{"text": "x"}]}}})
    assert result.total_tokens == 0


AWS_ENV_VARS = (
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
)


@pytest.fixture
def clean_aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The developer machine running these tests has real Bedrock credentials
    exported; without clearing them every case would resolve to bearer_token."""
    for name in AWS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"AWS_BEARER_TOKEN_BEDROCK": "tok"}, "bearer_token"),
        ({"AWS_ACCESS_KEY_ID": "k", "AWS_SECRET_ACCESS_KEY": "s"}, "static_keys"),
        ({"AWS_PROFILE": "dev"}, "profile"),
        ({}, "default_chain"),
    ],
)
def test_credential_mode_resolution(
    clean_aws_env: None, env: dict[str, str], expected: str
) -> None:
    """NFR-2 — bearer token preferred, then static keys, then profile."""
    settings = Settings(_env_file=None, **env)  # type: ignore[arg-type]
    assert settings.bedrock_credential_mode() == expected


def test_bearer_token_wins_over_static_keys(clean_aws_env: None) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[arg-type]
        AWS_BEARER_TOKEN_BEDROCK="tok",
        AWS_ACCESS_KEY_ID="k",
        AWS_SECRET_ACCESS_KEY="s",
    )
    assert settings.bedrock_credential_mode() == "bearer_token"


def test_credentials_are_never_persisted_to_the_database() -> None:
    """NFR-2 — no model column may hold a credential."""
    from workflow_orchestrator.models import Base

    forbidden = {
        "secret",
        "password",
        "credential",
        "access_key",
        "api_key",
        "bearer",
        "auth_token",
        "authorization",
    }
    # ``tokens_used`` / ``index_token_count`` are LLM token *counts* (FR-21,
    # NFR-6), not credentials — the substring "token" alone is not a signal.
    offenders = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if any(word in column.name.lower() for word in forbidden)
    ]
    assert offenders == []
