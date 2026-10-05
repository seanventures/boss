"""One meeting, one record, in the person's words.

The independent walk of rp/research-panels (2026-10-05): a go/no-go convened once showed TWO
cards (its meeting_runs row and its meetings row), every record lacked its kind so a stand-up
rendered as a PRD panel, the prompt scaffolding was the only text there was to show, a failed
panel never said why, and a panel opened by task outside the newest 25 could not be found.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from apps.api.app.db import begin

PROJECT_ID = "boss-dogfood"
SCAFFOLDED = ("Build a status page.\n\n--- RESEARCH ALREADY DONE FOR THIS ---\nBOSS researched this "
              "before convening you.\n\n## What was researched\n{\"questions\": [\"Who?\"]}")


def _run(c, run_id, *, status="completed", state=None, constraints=None, meeting_id=None,
         task_id=None, error=None, directive=SCAFFOLDED):
    c.execute(text("""
        INSERT INTO meeting_runs (id, project_id, task_id, status, phase, directive, url, role_keys,
                                  constraints, state, usage, finalize_ctx, meeting_id, error, title)
        VALUES (:id, :p, :t, :s, 'done', :d, '', '[]'::jsonb, CAST(:c AS JSONB), CAST(:st AS JSONB),
                '{}'::jsonb, '{}'::jsonb, :m, :e, NULL)"""),
        {"id": run_id, "p": PROJECT_ID, "t": task_id, "s": status, "d": directive, "m": meeting_id,
         "e": error, "c": json.dumps(constraints or {}), "st": json.dumps(state or {})})


def test_a_typed_fixture_is_one_card_with_its_kind_verdict_and_dissent(client, engine):
    with begin(engine) as c:
        c.execute(text("""INSERT INTO meetings (id, project_id, title, purpose, status, participants, created_by)
                          VALUES ('mtg_one_gng', :p, 'Go/No-Go: ship snooze', 'x', 'completed', '[]'::jsonb, 'boss')"""),
                  {"p": PROJECT_ID})
        _run(c, "mrun_one_gng", status="simulated", meeting_id="mtg_one_gng",
             constraints={"meeting_type": "go_no_go"},
             state={"meeting_type": "go_no_go", "decision_items": [{"recommendation": {"verdict": "go"}}],
                    "dissent_records": [{"role": "QA / Test", "position": "no-go", "reason": "Flake"}]})
    items = client.get("/v1/meeting-rooms", params={"project_id": PROJECT_ID}).json()["items"]
    ids = [i["id"] for i in items]
    assert "mrun_one_gng" in ids and "mtg_one_gng" not in ids
    one = next(i for i in items if i["id"] == "mrun_one_gng")
    assert one["meeting_type"] == "go_no_go"
    assert one["verdict"] == "go"
    assert one["dissent"] == [{"role": "QA / Test", "position": "no-go", "reason": "Flake"}]
    assert one["can_vote"] is True


def test_the_brief_is_the_ask_and_a_failed_panel_says_why(client, engine):
    with begin(engine) as c:
        _run(c, "mrun_words_ok")
        _run(c, "mrun_words_stored", constraints={"brief": "Exactly what I typed"})
        _run(c, "mrun_words_failed", status="failed", error="The budget cap was reached.")
    items = {i["id"]: i for i in client.get("/v1/meeting-rooms", params={"project_id": PROJECT_ID}).json()["items"]}
    ok = items["mrun_words_ok"]
    assert ok["brief"] == "Build a status page."
    assert "RESEARCH ALREADY" not in ok["title"] and "{" not in ok["brief"]
    assert "- **Questions**" in ok["context"] and "{" not in ok["context"]
    assert ok["meeting_type"] == "prd_panel" and ok["can_vote"] is False
    assert items["mrun_words_stored"]["brief"] == "Exactly what I typed"
    assert items["mrun_words_failed"]["error"] == "The budget cap was reached."


def test_a_panel_is_found_by_its_task_and_pages_past_the_first(client, engine):
    with begin(engine) as c:
        c.execute(text("INSERT INTO tasks (id, project_id, title, dedupe_key) VALUES ('tsk_rooms_9', :p, 't', 'rooms-9')"),
                  {"p": PROJECT_ID})
        _run(c, "mrun_rooms_task", task_id="tsk_rooms_9")
        for n in range(3):
            _run(c, f"mrun_rooms_page_{n}")
    by_task = client.get("/v1/meeting-rooms", params={"project_id": PROJECT_ID, "task_id": "tsk_rooms_9"}).json()["items"]
    assert [i["id"] for i in by_task] == ["mrun_rooms_task"]
    first = client.get("/v1/meeting-rooms", params={"project_id": PROJECT_ID, "limit": 2}).json()["items"]
    second = client.get("/v1/meeting-rooms", params={"project_id": PROJECT_ID, "limit": 2, "offset": 2}).json()["items"]
    assert len(first) == 2 and second and not {i["id"] for i in first} & {i["id"] for i in second}
    nothing = client.get("/v1/meeting-rooms", params={"project_id": PROJECT_ID, "task_id": "tsk_none"}).json()["items"]
    assert nothing == []
