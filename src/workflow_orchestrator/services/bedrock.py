"""AWS Bedrock Converse client — SRS FR-10, NFR-2.

Mirrors the request/response shapes proven by the Neovim provider at
``~/.config/nvim/lua/custom/bedrock_provider.lua``: system blocks are separated
from messages, ``inferenceConfig`` carries the sampling knobs, and the reply text
is concatenated from ``output.message.content[].text``.

Credentials come from the environment only, never from the database (NFR-2).
Two shapes are supported:

* ``AWS_BEARER_TOKEN_BEDROCK`` — a Bedrock API key. boto3 reads this env var
  natively for ``bedrock-runtime``; it is what the Neovim provider already uses,
  so it is preferred when present.
* The standard boto3 credential chain — static keys, ``AWS_PROFILE``, instance
  metadata, and so on.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..logging import get_logger

log = get_logger(__name__)

DEFAULT_MAX_TOKENS = 8000
DEFAULT_TEMPERATURE = 0.1


class BedrockError(RuntimeError):
    pass


@dataclass
class Message:
    role: str  # "user" | "assistant"
    text: str


@dataclass
class ConverseResult:
    text: str
    stop_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


def _content_block(text: str) -> dict[str, Any]:
    return {"text": text}


def build_request(
    messages: list[Message],
    *,
    system: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
) -> dict[str, Any]:
    """Build a Converse request body.

    Kept pure so the shape can be asserted in tests without touching AWS.
    """
    body: dict[str, Any] = {
        "messages": [
            {"role": m.role, "content": [_content_block(m.text)]}
            for m in messages
            if m.text.strip()
        ],
        "inferenceConfig": {
            "maxTokens": max_tokens,
            "temperature": temperature,
        },
    }
    if system:
        body["system"] = [_content_block(system)]
    return body


def parse_response(payload: dict[str, Any]) -> ConverseResult:
    """Extract text and usage from a Converse response."""
    error = _normalize_error(payload)
    if error:
        raise BedrockError(error)

    blocks = (payload.get("output") or {}).get("message", {}).get("content") or []
    text = "".join(
        block["text"] for block in blocks if isinstance(block, dict) and "text" in block
    )
    usage = payload.get("usage") or {}
    return ConverseResult(
        text=text,
        stop_reason=payload.get("stopReason"),
        input_tokens=int(usage.get("inputTokens") or 0),
        output_tokens=int(usage.get("outputTokens") or 0),
        total_tokens=int(usage.get("totalTokens") or 0),
        raw=payload,
    )


def _normalize_error(payload: dict[str, Any]) -> str | None:
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if error:
        if isinstance(error, str):
            return error
        if isinstance(error, dict):
            return str(error.get("message") or error.get("Message") or error)
    # A bare message with no output is an error envelope, not a reply.
    if "output" not in payload and (payload.get("message") or payload.get("Message")):
        return str(payload.get("message") or payload.get("Message"))
    return None


class BedrockClient:
    """Thin async wrapper over the synchronous boto3 ``converse`` call."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Any | None = None

    @property
    def credential_mode(self) -> str:
        return self._settings.bedrock_credential_mode()

    def _build_client(self) -> Any:
        import boto3
        from botocore.config import Config as BotoConfig

        settings = self._settings
        boto_config = BotoConfig(
            region_name=settings.AWS_REGION,
            retries={"max_attempts": 3, "mode": "standard"},
            read_timeout=300,
            connect_timeout=15,
        )

        session_kwargs: dict[str, Any] = {}
        if settings.AWS_PROFILE and not settings.AWS_BEARER_TOKEN_BEDROCK:
            session_kwargs["profile_name"] = settings.AWS_PROFILE

        boto_session = boto3.Session(**session_kwargs)

        client_kwargs: dict[str, Any] = {"config": boto_config}
        if not settings.AWS_BEARER_TOKEN_BEDROCK:
            # Only pass static keys explicitly; otherwise let the chain resolve.
            if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
                client_kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
                client_kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
                if settings.AWS_SESSION_TOKEN:
                    client_kwargs["aws_session_token"] = settings.AWS_SESSION_TOKEN

        return boto_session.client("bedrock-runtime", **client_kwargs)

    def client(self) -> Any:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    async def converse(
        self,
        messages: list[Message],
        *,
        model_id: str | None = None,
        system: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> ConverseResult:
        model = model_id or self._settings.WORKFLOW_BEDROCK_MODEL_REASONING
        request = build_request(
            messages, system=system, max_tokens=max_tokens, temperature=temperature
        )

        def _call() -> dict[str, Any]:
            client = self.client()
            return client.converse(modelId=model, **request)

        try:
            payload = await asyncio.to_thread(_call)
        except Exception as exc:  # boto3 raises a wide variety of ClientErrors
            log.error("bedrock.converse_failed", model_id=model, error=str(exc))
            raise BedrockError(f"Bedrock converse failed for {model}: {exc}") from exc

        result = parse_response(payload)
        log.info(
            "bedrock.converse",
            model_id=model,
            stop_reason=result.stop_reason,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )
        return result

    async def check_model_available(self, model_id: str | None = None) -> bool:
        """Best-effort startup check. Never fatal — logs a warning instead."""
        model = model_id or self._settings.WORKFLOW_BEDROCK_MODEL_REASONING
        try:
            await self.converse(
                [Message(role="user", text="ping")], model_id=model, max_tokens=1
            )
        except BedrockError as exc:
            log.warning("bedrock.model_unavailable", model_id=model, error=str(exc))
            return False
        return True
