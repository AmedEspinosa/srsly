"""Regression: the super summary must reach the model, and its absence must be
visible.

The first live sessions ran with the feature prompt alone. The wiki path was
configured one level too deep, ``is_initialized()`` failed, and
``build_session_context`` returned "". Nothing surfaced that — the only symptom
was that the requirements engine asked generic questions ("What language is
this?") where the same prompt with the super summary produced specific ones
("is axios-retry already installed?").

So there are two things to hold: that the context is genuinely in the first
message sent to Bedrock, and that when it is not, the API says why.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from workflow_orchestrator.app import create_app
from workflow_orchestrator.config import Settings
from workflow_orchestrator.librarian.layout import WIKI_ROOT
from workflow_orchestrator.librarian.scaffold import init_wiki_repo
from workflow_orchestrator.services.requirements import RequirementsEngine

from .test_requirements_engine import ScriptedBedrock, questions_block

SUMMARY_REL = f"{WIKI_ROOT}/super-summaries/qa.md"
SUMMARY_TEXT = (
    "# Trucking backend\n\nFrontend calls the API through an Axios client. "
    "Backend outbound calls use httpx. Sentry is wired up.\n"
)


@pytest.fixture
def no_wiki_query(monkeypatch):
    """FR-36's page selection shells out to Codex; the super summary is FR-35
    and must work on its own."""

    async def _none(settings, project, question):
        return []

    monkeypatch.setattr(
        "workflow_orchestrator.librarian.query.query_pages", _none
    )


async def _app(settings: Settings, replies: list[str]):
    app = create_app(settings, create_schema=True)
    fake = ScriptedBedrock(replies)
    app.state.requirements_engine = RequirementsEngine(settings, client=fake)  # type: ignore[arg-type]
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    context = app.router.lifespan_context(app)
    await context.__aenter__()
    return client, fake, context


async def _session(client: AsyncClient, repo: Path, wiki_repo: Path) -> str:
    project = (
        await client.post(
            "/projects",
            json={
                "name": "qa",
                "repo_path": str(repo),
                "wiki_repo_path": str(wiki_repo),
                "wiki_super_summary_path": SUMMARY_REL,
            },
        )
    ).json()
    session = (
        await client.post(
            f"/projects/{project['id']}/sessions",
            json={"feature_prompt": "Add retries to the HTTP client"},
        )
    ).json()
    return session["id"]


@pytest.fixture
def seeded_wiki(wiki_repo: Path) -> Path:
    """A vault in the *nested* shape — the one the live project uses."""
    init_wiki_repo(wiki_repo)
    nested = wiki_repo / WIKI_ROOT / "wiki" / "super-summaries"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "qa.md").write_text(SUMMARY_TEXT, encoding="utf-8")
    return wiki_repo


async def test_super_summary_reaches_the_model(
    settings: Settings, repo: Path, seeded_wiki: Path, no_wiki_query
) -> None:
    client, fake, context = await _app(settings, [questions_block("Which layer?")])
    try:
        session_id = await _session(client, repo, seeded_wiki)
        response = await client.post(f"/sessions/{session_id}/qa/start")
        assert response.status_code == 200, response.text

        # The very first user turn — what the model actually sees.
        first_role, first_text = fake.calls[0]["messages"][0]  # type: ignore[index]
        assert first_role == "user"
        assert "Axios" in first_text, "super summary missing from the opening turn"
        assert "Add retries to the HTTP client" in first_text
    finally:
        await context.__aexit__(None, None, None)
        await client.aclose()


async def test_context_endpoint_reports_injection(
    settings: Settings, repo: Path, seeded_wiki: Path, no_wiki_query
) -> None:
    client, _fake, context = await _app(settings, [questions_block("Which layer?")])
    try:
        session_id = await _session(client, repo, seeded_wiki)

        before = (await client.get(f"/sessions/{session_id}/qa/context")).json()
        assert before["available"] is True
        assert before["injected"] is False

        await client.post(f"/sessions/{session_id}/qa/start")
        after = (await client.get(f"/sessions/{session_id}/qa/context")).json()
        assert after["injected"] is True
        assert after["super_summary_found"] is True
        assert after["super_summary_chars"] == len(SUMMARY_TEXT)
    finally:
        await context.__aexit__(None, None, None)
        await client.aclose()


async def test_missing_wiki_is_reported_not_silent(
    settings: Settings, repo: Path, wiki_repo: Path, no_wiki_query
) -> None:
    """An unscaffolded wiki must still run QA — but must not look healthy."""
    client, _fake, context = await _app(settings, [questions_block("Which layer?")])
    try:
        session_id = await _session(client, repo, wiki_repo)
        assert (await client.post(f"/sessions/{session_id}/qa/start")).status_code == 200

        status = (await client.get(f"/sessions/{session_id}/qa/context")).json()
        assert status["initialized"] is False
        assert status["injected"] is False
        assert status["hint"] and WIKI_ROOT in status["hint"]
    finally:
        await context.__aexit__(None, None, None)
        await client.aclose()


async def test_restart_reopens_the_loop_with_context(
    settings: Settings, repo: Path, seeded_wiki: Path, no_wiki_query
) -> None:
    """A session that opened without context can be recovered.

    Context is only injectable at round 0, so without this a session started
    against a misconfigured wiki is stuck with generic questions for good.
    This walks the live sequence: start with the summary unreachable, fix the
    wiki, restart.
    """
    client, fake, context = await _app(
        settings, [questions_block("Generic?"), questions_block("Specific?")]
    )
    summary = seeded_wiki / WIKI_ROOT / "wiki" / "super-summaries" / "qa.md"
    try:
        session_id = await _session(client, repo, seeded_wiki)

        summary.rename(summary.with_suffix(".md.hidden"))
        await client.post(f"/sessions/{session_id}/qa/start")
        assert "Axios" not in fake.calls[0]["messages"][0][1]  # type: ignore[index]
        assert (await client.get(f"/sessions/{session_id}/qa/context")).json()[
            "injected"
        ] is False

        summary.with_suffix(".md.hidden").rename(summary)
        restarted = await client.post(f"/sessions/{session_id}/qa/restart")
        assert restarted.status_code == 200, restarted.text

        # The transcript was replaced, not appended to.
        messages = fake.calls[-1]["messages"]  # type: ignore[index]
        assert len(messages) == 1
        assert "Axios" in messages[0][1]
        assert restarted.json()["round"] == 1

        state = (await client.get(f"/sessions/{session_id}/qa")).json()
        assert [q["text"] for q in state["questions"]] == ["Specific?"]
    finally:
        await context.__aexit__(None, None, None)
        await client.aclose()


def test_super_summary_is_not_truncated_at_the_page_limit() -> None:
    """The live super summary is 28 KB; the per-page cap would drop a third."""
    from workflow_orchestrator.librarian import retrieval

    assert retrieval.MAX_SUPER_SUMMARY_CHARS > 28_000
    assert retrieval.MAX_SUPER_SUMMARY_CHARS > retrieval.MAX_PAGE_CHARS


def test_clarify_block_still_parses_from_the_scripted_shape() -> None:
    """Guard the fixture itself — a malformed block would make the assertions
    above pass for the wrong reason."""
    from workflow_orchestrator.services.clarify import parse_response

    payload = parse_response(questions_block("a", "b"))
    assert payload is not None
    assert [q.text for q in payload.questions] == ["a", "b"]
    assert json.loads('{"ok": true}')["ok"] is True
