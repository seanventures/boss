"""The re-check's API door (BOSSAPP-1326): preview with the estimate, start, refuse in words."""
from __future__ import annotations

import json
import uuid

from sqlalchemy import text

from apps.api.tests import command_center_view_seed as seed

PID = seed.PROJECT


def _run(c, *, status="completed", pr="http://boss-local/acme/shop/pull/7"):
    rid = f"crun_{uuid.uuid4().hex[:16]}"
    c.execute(text(
        "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, finalize_ctx, "
        "pull_request_url) VALUES (:i, :p, :s, 'done', 'Shop', CAST(:sp AS JSONB), "
        "'{}'::jsonb, :u)"),
        {"i": rid, "p": PID, "s": status, "u": pr,
         "sp": json.dumps({"instructions": "Build the shop.", "owner": "acme", "repo": "shop"})})
    return rid


def test_the_preview_says_what_it_runs_and_costs(engine, client):
    with engine.begin() as c:
        rid = _run(c)
    got = client.get(f"/v1/code-tasks/{rid}/recheck")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["can"] is True
    assert body["gates"] == ["tests", "security", "review", "consistency", "acceptance"]
    assert body["estimate"]["usd"] > 0


def test_a_change_that_cannot_be_rechecked_is_refused_in_words(engine, client):
    with engine.begin() as c:
        running = _run(c, status="running")
        no_pr = _run(c, pr=None)
    for rid, why in ((running, "still running"), (no_pr, "no pull request")):
        got = client.post(f"/v1/code-tasks/{rid}/recheck", json={"actor": "ceo"})
        assert got.status_code == 409, got.text
        assert why in got.json()["detail"]
    assert client.get(f"/v1/code-tasks/{no_pr}/recheck").json()["can"] is False


def test_a_press_that_withdrew_its_card_answers_409_and_keeps_the_withdrawal(
        engine, client, monkeypatch):
    """PR #170 review: a stale Fix card's press withdraws the card (decisions.transition returns
    it withdrawn). The route answered 200 and recorded a governed `decision.approve`; now it
    answers 409 with the note, records no approval, and the withdrawal is COMMITTED."""
    from apps.api.app import main

    did = f"dec_{uuid.uuid4().hex[:10]}"
    note = "A newer build of this change (crun_x) exists; its own checks decide. Nothing was started."
    with engine.begin() as c:
        c.execute(text("INSERT INTO decisions (id, project_id, question, recommendation, status) "
                       "VALUES (:i, :p, 'Fix these (about $1.00)', CAST(:r AS JSONB), 'pending')"),
                  {"i": did, "p": PID, "r": json.dumps({"action": "start_recheck_followup"})})

    def withdraw(connection, decision_id, **_kw):
        connection.execute(text("UPDATE decisions SET status = 'withdrawn', resolution_note = :n "
                                "WHERE id = :i"), {"i": decision_id, "n": note})
        return dict(connection.execute(text("SELECT * FROM decisions WHERE id = :i"),
                                       {"i": decision_id}).mappings().one())

    governed: list = []
    monkeypatch.setattr(main.decisions, "transition", withdraw)
    monkeypatch.setattr(main, "_record_governed", lambda *a, **k: governed.append(k))
    got = client.post(f"/v1/decisions/{did}/approve", json={"actor": "ceo", "note": "go"})
    assert got.status_code == 409, got.text
    assert got.json() == {"detail": note, "status": "withdrawn", "decision_id": did}
    assert not [g for g in governed if g.get("effect_kind") == "decision.approve"]
    with engine.begin() as c:
        assert c.execute(text("SELECT status FROM decisions WHERE id = :i"),
                         {"i": did}).scalar() == "withdrawn"
