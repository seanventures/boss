"""The Meetings screen can tell its meetings apart, and says where each PRD stands.

The incident (boss-1, 2026-10-02): every workflow-convened meeting was titled with the framing
the workflow wraps around the ask — "PRD meeting: --- WHAT THIS PANEL IS FOR --- You are the
panel inside BOSS'…" — so all four cards and all four consensus options read the same. The cards
said "awaiting approval" whenever no PR URL was recorded, including meetings whose PRD shipped.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from apps.api.app.db import begin
from boss_engine.services import meeting_service as ms

PROJECT_ID = "boss-dogfood"


def _framed(workflow: str, ask: str) -> str:
    return (
        f"{ms.PANEL_PURPOSE_MARKER}\nYou are the panel inside BOSS's {workflow} workflow. "
        f"That workflow's objective:\n\nShip it.\n{ms.PANEL_ASK_MARKER}\n\n{ask}"
    )


def _run(connection, run_id, directive, *, title=None, decision_id=None, status="completed"):
    connection.execute(
        text(
            """
            INSERT INTO meeting_runs (id, project_id, status, phase, directive, url, role_keys,
                                      constraints, state, usage, finalize_ctx, decision_id, title)
            VALUES (:id, :p, :st, 'done', :d, '', '[]'::jsonb, '{}'::jsonb, '{}'::jsonb,
                    CAST(:u AS JSONB), '{}'::jsonb, :dec, :t)
            """
        ),
        {"id": run_id, "p": PROJECT_ID, "st": status, "d": directive, "dec": decision_id,
         "t": title, "u": json.dumps({"cost_usd_micros": 870000})},
    )


def _decision(connection, decision_id, status):
    connection.execute(
        text(
            "INSERT INTO decisions (id, project_id, question, status) VALUES (:id, :p, :q, :s)"
        ),
        {"id": decision_id, "p": PROJECT_ID, "q": "Approve publishing the PRD the team drafted",
         "s": status},
    )


def test_workflow_convened_meetings_are_named_by_their_ask(client, engine):
    with begin(engine) as c:
        _decision(c, "dec_cards_shipped", "executed")
        _decision(c, "dec_cards_waiting", "pending")
        # Written before meeting_runs.title existed: only the framed directive.
        _run(c, "mrun_cards_legacy", _framed("ship-a-web-app", "Build a recipe planner"),
             decision_id="dec_cards_shipped")
        # Written now: the run carries its own title.
        _run(c, "mrun_cards_titled", _framed("weekly-newsletter", "Draft the October issue"),
             title="Draft the October issue", decision_id="dec_cards_waiting")

    response = client.get("/v1/meeting-rooms", params={"project_id": PROJECT_ID})
    assert response.status_code == 200, response.text
    items = {item["id"]: item for item in response.json()["items"]}

    assert items["mrun_cards_legacy"]["title"] == "PRD meeting: Build a recipe planner"
    assert items["mrun_cards_titled"]["title"] == "PRD meeting: Draft the October issue"
    titles = [item["title"] for item in items.values()]
    assert all("---" not in t for t in titles)
    assert len(set(titles)) == len(titles)

    # The PRD's state comes from its decision, not from a PR URL that was never recorded.
    assert items["mrun_cards_legacy"]["prd_label"] == "PRD published"
    assert items["mrun_cards_titled"]["prd_label"] == "awaiting approval"


def test_a_legacy_meeting_row_cut_inside_the_framing_names_its_workflow(client, engine):
    with begin(engine) as c:
        c.execute(
            text(
                """
                INSERT INTO meetings (id, project_id, title, purpose, status, participants,
                                      created_by)
                VALUES ('mtg_cards_cut', :p, :t, 'Cross-functional PRD review and drafting',
                        'completed', '[]'::jsonb, 'boss')
                """
            ),
            {"p": PROJECT_ID,
             "t": "PRD meeting: " + _framed("ship-a-web-app", "x")[:80]},
        )
    items = {i["id"]: i for i in client.get(
        "/v1/meeting-rooms", params={"project_id": PROJECT_ID}).json()["items"]}
    assert items["mtg_cards_cut"]["title"] == "PRD meeting: panel for the ship-a-web-app workflow"
    assert items["mtg_cards_cut"]["prd_label"] == "no PRD drafted"


def test_convening_through_the_api_stores_the_subject_as_the_title(client, engine, monkeypatch):
    response = client.post(
        "/v1/meetings/prd",
        json={"project_id": PROJECT_ID, "actor": "ceo",
              "directive": _framed("ship-a-web-app", "Plan the  onboarding\nflow"),
              "url": ""},
    )
    assert response.status_code in (200, 201), response.text
    with begin(engine) as c:
        row = c.execute(
            text("SELECT title FROM meeting_runs WHERE project_id = :p ORDER BY created_at DESC LIMIT 1"),
            {"p": PROJECT_ID},
        ).mappings().one()
        task = c.execute(
            text("SELECT title FROM tasks WHERE id = :t"), {"t": response.json()["task_id"]},
        ).mappings().one()
    assert row["title"] == "Plan the onboarding flow"
    assert task["title"] == "PRD meeting: Plan the onboarding flow"
