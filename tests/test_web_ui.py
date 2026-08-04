"""UI routes render, and do not shadow the SRS §4.3 JSON API."""

from __future__ import annotations

import re

from pathlib import Path

from httpx import AsyncClient

from .conftest import write_artifact


async def test_api_paths_return_json_not_html(
    client: AsyncClient, project: dict, session: dict
) -> None:
    """Regression: the UI once registered ``GET /projects/{id}`` and shadowed the
    API. AC-1 does ``GET /sessions/S1`` and expects JSON, so the API owns those
    paths and the UI lives under ``/ui``."""
    for path in (
        "/projects",
        f"/projects/{project['id']}",
        f"/projects/{project['id']}/sessions",
        f"/sessions/{session['id']}",
    ):
        response = await client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("application/json"), path


async def test_ui_pages_render_html(
    client: AsyncClient, project: dict, session: dict
) -> None:
    root = await client.get("/")
    assert root.status_code == 200
    assert root.headers["content-type"].startswith("text/html")
    assert "Projects" in root.text
    assert project["name"] in root.text

    project_page = await client.get(f"/ui/projects/{project['id']}")
    assert project_page.status_code == 200
    assert session["feature_prompt"][:20] in project_page.text

    session_page = await client.get(f"/ui/sessions/{session['id']}")
    assert session_page.status_code == 200
    # Phase stepper and approval controls are present.
    assert 'class="stepper"' in session_page.text
    assert "approve-btn" in session_page.text


async def test_session_page_marks_current_and_approved_phases(
    client: AsyncClient, session: dict, repo: Path
) -> None:
    write_artifact(repo, session["id"], "srs.md")
    await client.post(f"/sessions/{session['id']}/approve", json={"phase": "srs"})

    page = (await client.get(f"/ui/sessions/{session['id']}")).text
    # The approved phase renders as done; the new current phase as current.
    assert "step done" in page
    assert "step current" in page


async def test_ui_404s_for_unknown_ids(client: AsyncClient) -> None:
    assert (await client.get("/ui/projects/nope")).status_code == 404
    assert (await client.get("/ui/sessions/nope")).status_code == 404


async def test_app_js_is_not_deferred(client: AsyncClient) -> None:
    """Regression: page templates call wo.* from inline <script> blocks, which
    execute during parsing. A deferred app.js runs *after* parsing, so every
    such call failed with "wo is not defined" (visible as that string rendered
    into the QA panel). app.js must load synchronously."""
    html = (await client.get("/")).text
    tag = next(line for line in html.splitlines() if "/static/app.js" in line)
    assert "defer" not in tag, tag
    assert "async" not in tag, tag


async def test_wo_helper_is_defined_before_inline_scripts(
    client: AsyncClient, session: dict
) -> None:
    """The script tag must appear before any inline block that uses it."""
    html = (await client.get(f"/ui/sessions/{session['id']}")).text
    script_at = html.index("/static/app.js")
    first_use = html.index("wo.api")
    assert script_at < first_use


async def test_artifact_buttons_are_deduplicated(
    client: AsyncClient, session: dict
) -> None:
    """QA and SRS both map to srs.md; the UI should show one button, not two."""
    html = (await client.get(f"/ui/sessions/{session['id']}")).text
    # Count only the artifact buttons — the approval blurb names the file too.
    buttons = re.findall(r'data-artifact="[^"]+">([^<]+)<', html)
    assert buttons.count(".workflow/srs.md") == 1, buttons
    assert len(buttons) == len(set(buttons)), buttons
