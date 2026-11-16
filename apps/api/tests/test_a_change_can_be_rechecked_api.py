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
