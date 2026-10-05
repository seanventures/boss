"""The meeting room serves every turn's gist, and one meeting can be read on its own.

The room shows each seat's turn as a card (headline, three points, "Read all"), so each record
carries `round1_gists` / `round2_gists` / `synthesis_gist`: the seat's own gist when it wrote
one, else one cut from its markdown — which is what every meeting held before gists existed
gets. A workflow's panel step opens its meeting by id, and the list is only the newest 25, so
`GET /v1/meeting-rooms/{id}` returns that one record in the same shape.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from apps.api.app.db import begin

PROJECT_ID = "boss-dogfood"
OLD_TURN = "**PM – the real problem**\n\n| a | b |\n|---|---|\n\n- Small teams drown in inbound noise.\n"


def _run(connection, run_id, state):
    connection.execute(
        text(
            """
            INSERT INTO meeting_runs (id, project_id, status, phase, directive, url, role_keys,
                                      constraints, state, usage, finalize_ctx, title)
            VALUES (:id, :p, 'completed', 'done', 'Build a lead triage inbox', '', '[]'::jsonb,
                    '{}'::jsonb, CAST(:s AS JSONB), '{}'::jsonb, '{}'::jsonb, 'Lead triage inbox')
            """
        ),
        {"id": run_id, "p": PROJECT_ID, "s": json.dumps(state)},
    )


def test_stored_gists_are_served_and_old_turns_get_one_cut(client, engine):
    with begin(engine) as c:
        _run(c, "mrun_gist_new", {
            "round1": {"Product Manager": "anything"},
            "round1_gists": {"Product Manager": {"headline": "Score it", "points": ["one", "two"]}},
            "round2": {"Product Manager": OLD_TURN},
            "synthesis": "## Agreed\n\nShip the narrow slice first.",
        })
        _run(c, "mrun_gist_old", {"round1": {"Product Manager": OLD_TURN}, "round2": {}})

    items = {i["id"]: i for i in client.get(
        "/v1/meeting-rooms", params={"project_id": PROJECT_ID}).json()["items"]}
    new, old = items["mrun_gist_new"], items["mrun_gist_old"]
    assert new["round1_gists"]["Product Manager"] == {"headline": "Score it", "points": ["one", "two"]}
    assert new["round2_gists"]["Product Manager"] == {
        "headline": "PM – the real problem", "points": ["Small teams drown in inbound noise."]}
    assert new["synthesis_gist"] == {"headline": "Agreed", "points": ["Ship the narrow slice first."]}
    assert old["round1_gists"]["Product Manager"]["headline"] == "PM – the real problem"
    assert old["synthesis_gist"] is None


def test_one_meeting_is_read_by_id_in_the_list_shape(client, engine):
    with begin(engine) as c:
        _run(c, "mrun_gist_one", {"round1": {"QA / Test": OLD_TURN}})
    listed = {i["id"]: i for i in client.get(
        "/v1/meeting-rooms", params={"project_id": PROJECT_ID}).json()["items"]}["mrun_gist_one"]
    one = client.get("/v1/meeting-rooms/mrun_gist_one", params={"project_id": PROJECT_ID})
    assert one.status_code == 200, one.text
    assert one.json() == listed

    missing = client.get("/v1/meeting-rooms/mrun_nope", params={"project_id": PROJECT_ID})
    assert missing.status_code == 404
    other = client.get("/v1/meeting-rooms/mrun_gist_one", params={"project_id": "another-project"})
    assert other.status_code == 404
