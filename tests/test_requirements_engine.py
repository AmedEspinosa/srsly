"""Requirements engine loop — FR-10, FR-11, FR-12, FR-13.

Bedrock is replaced with a scripted client so the loop's control flow — the 5x5
cap, the ready flag, early termination, restart durability — is asserted without
network calls or model nondeterminism.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from workflow_orchestrator.app import create_app
from workflow_orchestrator.config import Settings
from workflow_orchestrator.db import dispose_engine
from workflow_orchestrator.services.bedrock import ConverseResult, Message
from workflow_orchestrator.services.prompts import MAX_ROUNDS
from workflow_orchestrator.services.requirements import RequirementsEngine

SRS_DOCUMENT = "# Software Requirements Specification\n\n## 1. Overview\n\nAuth0 rollout.\n"


def questions_block(*texts: str, ready: bool = False) -> str:
    payload = {
        "v": 1,
        "ready": ready,
        "questions": [
            {
                "id": f"q{i + 1}",
                "text": text,
                "type": "text",
                "options": [],
                "allow_custom": True,
                "placeholder": "",
            }
            for i, text in enumerate(texts)
        ],
    }
    return f"Some prose.\n\n```clarify-json\n{json.dumps(payload)}\n```"


class ScriptedBedrock:
    """Returns queued replies in order; records every request for assertions."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, object]] = []

    async def converse(
        self,
        messages: list[Message],
        *,
        model_id: str | None = None,
        system: str | None = None,
        max_tokens: int = 8000,
        temperature: float = 0.1,
    ) -> ConverseResult:
        self.calls.append(
            {
                "messages": [(m.role, m.text) for m in messages],
                "system": system,
                "max_tokens": max_tokens,
            }
        )
        text = self.replies.pop(0) if self.replies else SRS_DOCUMENT
        return ConverseResult(text=text, stop_reason="end_turn", total_tokens=100)


@pytest.fixture
def make_client(settings: Settings):
    """Build an app whose requirements engine uses a scripted Bedrock client."""

    async def _factory(replies: list[str]):
        app = create_app(settings, create_schema=True)
        fake = ScriptedBedrock(replies)
        app.state.requirements_engine = RequirementsEngine(settings, client=fake)  # type: ignore[arg-type]
        transport = ASGITransport(app=app)
        client = AsyncClient(transport=transport, base_url="http://test")
        context = app.router.lifespan_context(app)
        await context.__aenter__()
        return client, fake, context

    return _factory


async def _bootstrap(client: AsyncClient, repo: Path, wiki_repo: Path) -> str:
    project = (
        await client.post(
            "/projects",
            json={
                "name": "qa",
                "repo_path": str(repo),
                "wiki_repo_path": str(wiki_repo),
                "wiki_super_summary_path": "llm-wiki/super-summaries/qa.md",
            },
        )
    ).json()
    session = (
        await client.post(
            f"/projects/{project['id']}/sessions",
            json={"feature_prompt": "Rebuild auth to use Auth0"},
        )
    ).json()
    return session["id"]


async def test_first_round_uses_the_srs_system_prompt(
    make_client, repo: Path, wiki_repo: Path
) -> None:
    client, fake, ctx = await make_client([questions_block("Which provider?")])
    try:
        session_id = await _bootstrap(client, repo, wiki_repo)
        state = (await client.post(f"/sessions/{session_id}/qa/start")).json()

        assert state["round"] == 1
        assert [q["text"] for q in state["questions"]] == ["Which provider?"]

        system = fake.calls[0]["system"]
        assert "clarify-json" in system
        assert "Ask, do not assume" in system  # FR-11
        assert "5 rounds" in system  # FR-10
        # The opening turn carries the feature prompt.
        assert "Auth0" in fake.calls[0]["messages"][0][1]
    finally:
        await client.aclose()
        await ctx.__aexit__(None, None, None)
        await dispose_engine()


async def test_ready_flag_ends_the_loop_and_writes_srs(
    make_client, repo: Path, wiki_repo: Path
) -> None:
    """FR-12 — .workflow/srs.md is produced when the model signals ready."""
    client, fake, ctx = await make_client(
        [
            questions_block("Which provider?"),
            questions_block(ready=True),  # round 2: done asking
            SRS_DOCUMENT,  # finalize call
        ]
    )
    try:
        session_id = await _bootstrap(client, repo, wiki_repo)
        await client.post(f"/sessions/{session_id}/qa/start")

        state = (
            await client.post(
                f"/sessions/{session_id}/qa/answer",
                json={"answers": [{"question_id": "q1", "answer": "Auth0"}]},
            )
        ).json()
        assert state["srs_written"] is True

        srs = repo / "worktrees" / session_id / ".workflow" / "srs.md"
        assert srs.exists()
        assert "Software Requirements Specification" in srs.read_text()

        # FR-12 completing means the session leaves QA.
        detail = (await client.get(f"/sessions/{session_id}")).json()
        assert detail["current_phase"] == "srs"
    finally:
        await client.aclose()
        await ctx.__aexit__(None, None, None)
        await dispose_engine()


async def test_round_cap_is_enforced(make_client, repo: Path, wiki_repo: Path) -> None:
    """FR-10 — the loop stops after 5 rounds even if the model keeps asking."""
    client, fake, ctx = await make_client(
        [questions_block(f"Question round {i}?") for i in range(1, MAX_ROUNDS + 1)]
        + [SRS_DOCUMENT]
    )
    try:
        session_id = await _bootstrap(client, repo, wiki_repo)
        state = (await client.post(f"/sessions/{session_id}/qa/start")).json()

        for _ in range(MAX_ROUNDS):
            if state["srs_written"]:
                break
            state = (
                await client.post(
                    f"/sessions/{session_id}/qa/answer",
                    json={"answers": [{"question_id": "q1", "answer": "yes"}]},
                )
            ).json()

        assert state["round"] <= MAX_ROUNDS
        assert state["srs_written"] is True
        srs = repo / "worktrees" / session_id / ".workflow" / "srs.md"
        assert srs.exists()
    finally:
        await client.aclose()
        await ctx.__aexit__(None, None, None)
        await dispose_engine()


async def test_more_than_five_questions_are_truncated(
    make_client, repo: Path, wiki_repo: Path
) -> None:
    """FR-10 — at most 5 questions per round reach the user."""
    client, fake, ctx = await make_client(
        [questions_block(*[f"Q{i}?" for i in range(1, 9)])]
    )
    try:
        session_id = await _bootstrap(client, repo, wiki_repo)
        state = (await client.post(f"/sessions/{session_id}/qa/start")).json()
        assert len(state["questions"]) == 5
    finally:
        await client.aclose()
        await ctx.__aexit__(None, None, None)
        await dispose_engine()


async def test_user_can_end_the_loop_early(
    make_client, repo: Path, wiki_repo: Path
) -> None:
    """FR-11 — the user may end the loop before the model says ready."""
    client, fake, ctx = await make_client(
        [questions_block("Which provider?"), SRS_DOCUMENT]
    )
    try:
        session_id = await _bootstrap(client, repo, wiki_repo)
        await client.post(f"/sessions/{session_id}/qa/start")

        state = (
            await client.post(
                f"/sessions/{session_id}/qa/answer",
                json={
                    "answers": [{"question_id": "q1", "answer": "Auth0"}],
                    "end_early": True,
                },
            )
        ).json()
        assert state["srs_written"] is True

        # The finalize call must forbid further questions.
        assert "do NOT emit a clarify-json block" in fake.calls[-1]["messages"][-1][1]
    finally:
        await client.aclose()
        await ctx.__aexit__(None, None, None)
        await dispose_engine()


async def test_no_question_block_ends_the_loop(
    make_client, repo: Path, wiki_repo: Path
) -> None:
    """A reply with no clarify block means the model is done asking."""
    client, fake, ctx = await make_client(["I have everything I need.", SRS_DOCUMENT])
    try:
        session_id = await _bootstrap(client, repo, wiki_repo)
        state = (await client.post(f"/sessions/{session_id}/qa/start")).json()
        assert state["srs_written"] is True
    finally:
        await client.aclose()
        await ctx.__aexit__(None, None, None)
        await dispose_engine()


async def test_answers_are_rendered_back_to_the_model(
    make_client, repo: Path, wiki_repo: Path
) -> None:
    client, fake, ctx = await make_client(
        [questions_block("Which provider?"), questions_block("Follow up?")]
    )
    try:
        session_id = await _bootstrap(client, repo, wiki_repo)
        await client.post(f"/sessions/{session_id}/qa/start")
        await client.post(
            f"/sessions/{session_id}/qa/answer",
            json={"answers": [{"question_id": "q1", "answer": "Auth0"}]},
        )
        second_call_messages = fake.calls[1]["messages"]
        rendered = second_call_messages[-1][1]
        assert "1. Which provider? → Auth0" in rendered
    finally:
        await client.aclose()
        await ctx.__aexit__(None, None, None)
        await dispose_engine()


async def test_transcript_survives_restart(
    make_client, settings: Settings, repo: Path, wiki_repo: Path
) -> None:
    """FR-6 — a session mid-loop is resumable after the app restarts."""
    client, fake, ctx = await make_client([questions_block("Which provider?")])
    try:
        session_id = await _bootstrap(client, repo, wiki_repo)
        await client.post(f"/sessions/{session_id}/qa/start")
    finally:
        await client.aclose()
        await ctx.__aexit__(None, None, None)
        await dispose_engine()

    # Fresh app against the same database — no replies queued, so any state the
    # engine reports must have come from the persisted transcript.
    client2, fake2, ctx2 = await make_client([])
    try:
        state = (await client2.get(f"/sessions/{session_id}/qa")).json()
        assert state["round"] == 1
        assert [q["text"] for q in state["questions"]] == ["Which provider?"]
        assert fake2.calls == []  # nothing was re-asked
    finally:
        await client2.aclose()
        await ctx2.__aexit__(None, None, None)
        await dispose_engine()


async def test_qa_endpoints_reject_non_qa_phase(
    make_client, repo: Path, wiki_repo: Path
) -> None:
    client, fake, ctx = await make_client([questions_block(ready=True), SRS_DOCUMENT])
    try:
        session_id = await _bootstrap(client, repo, wiki_repo)
        await client.post(f"/sessions/{session_id}/qa/start")  # writes srs.md, -> srs

        response = await client.post(f"/sessions/{session_id}/qa/start")
        assert response.status_code == 422
        assert response.json()["detail"]["error"] == "not_in_qa_phase"
    finally:
        await client.aclose()
        await ctx.__aexit__(None, None, None)
        await dispose_engine()
