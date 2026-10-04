"""The build actions that were MCP-only have HTTP routes (factory spec §6.5, §8.1).

Stop had no button anywhere — only an MCP tool — so a build heading the wrong way could only
be watched. Review comments could be listed and resolved only over MCP, and the list read a
field nothing writes, so it was always empty. These routes are the console's, behind the same
project authorization as starting work.
"""
from __future__ import annotations

from sqlalchemy import text

from apps.api.tests import _factory_support as fx


def test_stop_then_stop_again_is_a_409(client, engine):
    run_id = fx.new_run(engine)
    act = fx.stage(engine, run_id, "implement")
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO jobs (id, project_id, kind, status, payload) VALUES "
            "(:j, :p, 'code.task', 'queued', CAST(:pl AS JSONB))"),
            {"j": f"job_{run_id}", "p": fx.PROJECT, "pl": f'{{"run_id": "{run_id}"}}'})
    missing = client.post(f"/v1/code-tasks/{run_id}/cancel", json={"actor": ""})
    assert missing.status_code == 422
    stranger = client.post(f"/v1/code-tasks/{run_id}/cancel", json={"actor": "not_a_member"})
    assert stranger.status_code == 403, stranger.text
    ok = client.post(f"/v1/code-tasks/{run_id}/cancel", json={"actor": fx.ACTOR})
    assert ok.status_code == 200, ok.text
    assert ok.json()["jobs_cancelled"] == [f"job_{run_id}"]
    again = client.post(f"/v1/code-tasks/{run_id}/cancel", json={"actor": fx.ACTOR})
    assert again.status_code == 409
    with engine.begin() as c:
        run = c.execute(text("SELECT status, finalize_ctx FROM code_task_runs WHERE id = :r"),
                        {"r": run_id}).mappings().one()
        act_status = c.execute(text("SELECT status FROM agent_acts WHERE act_id = :a"),
                               {"a": act["act_id"]}).scalar()
        job = c.execute(text("SELECT status FROM jobs WHERE id = :j"),
                        {"j": f"job_{run_id}"}).scalar()
    assert run["status"] == "cancelled" and run["finalize_ctx"]["cancelled_by"] == fx.ACTOR
    assert act_status == "skipped" and job == "cancelled"
    cockpit = client.get(f"/v1/code-tasks/{run_id}/cockpit",
                         params={"project_id": fx.PROJECT, "viewer": fx.ACTOR}).json()
    assert cockpit["state"]["word"] == "Stopped by you"
    assert any(r["type"] == "you" and r["what"] == "stopped" for r in cockpit["story"])


def test_stopping_an_unknown_build_is_a_404(client):
    assert client.post("/v1/code-tasks/crun_nope/cancel",
                       json={"actor": fx.ACTOR}).status_code == 404


def test_review_comments_list_and_resolve_round_trip(client, engine):
    run_id = fx.new_run(
        engine, status="completed", phase="done",
        state={"review_summary": "FINDING: MAJOR src/a.py:3 unchecked input"},
        fctx={"review_comments": [{"id": "rc_1", "round": 0, "severity": "blocker",
                                   "body": "CSV injection", "path": "src/x.ts", "line": 42,
                                   "author": "adversarial_reviewer", "author_kind": "agent",
                                   "status": "open", "stage": "review_code"}]})
    listed = client.get(f"/v1/code-tasks/{run_id}/review-comments").json()
    assert listed["count"] == 2  # the stored one and the finding only in the evidence
    resolved = client.post(f"/v1/code-tasks/{run_id}/review-comments/rc_1/resolve",
                           json={"actor": fx.ACTOR, "status": "addressed", "note": "fixed"})
    assert resolved.status_code == 200, resolved.text
    open_now = client.get(f"/v1/code-tasks/{run_id}/review-comments",
                          params={"status": "open"}).json()
    assert [c["id"] for c in open_now["comments"]] == [None]
    nope = client.post(f"/v1/code-tasks/{run_id}/review-comments/rc_missing/resolve",
                       json={"actor": fx.ACTOR, "status": "addressed"})
    assert nope.status_code == 404
    bad = client.post(f"/v1/code-tasks/{run_id}/review-comments/rc_1/resolve",
                      json={"actor": fx.ACTOR, "status": "deleted"})
    assert bad.status_code == 422


def test_gates_return_names_and_labels(client, engine):
    run_id = fx.new_run(engine, status="blocked", phase="awaiting_gate_override")
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO quality_gates (id, project_id, code_task_run_id, status, gates_passed, "
            "gates_failed) VALUES (:i, :p, :r, 'fail', '[\"code_written\"]'::jsonb, "
            "'[\"code_review_passed\", \"app_serves\"]'::jsonb)"),
            {"i": f"qg_{run_id}", "p": fx.PROJECT, "r": run_id})
    got = client.get(f"/v1/code-tasks/{run_id}/gates").json()
    by = {g["name"]: g for g in got["gates"]}
    assert by["code_written"] == {"name": "code_written", "label": "Code written", "ok": True,
                                  "station": "build", "waivable": True}
    assert by["code_review_passed"]["label"] == "Review passed"
    assert by["app_serves"]["waivable"] is False


def test_a_waiver_needs_a_reason_and_never_waives_the_app_starting(client, engine):
    run_id = fx.new_run(engine, status="blocked", phase="awaiting_gate_override",
                        fctx={"failing_gates": ["app_serves"],
                              "pending_diff": [{"path": "a.py", "content": "x"}]})
    no_reason = client.post(f"/v1/code-tasks/{run_id}/override",
                            json={"actor": fx.ACTOR, "waive_gates": ["app_serves"]})
    assert no_reason.status_code == 422
    refused = client.post(f"/v1/code-tasks/{run_id}/override",
                          json={"actor": fx.ACTOR, "waive_gates": ["*"], "reason": "ship it"})
    assert refused.status_code == 400
    assert "the app must start" in refused.json()["detail"]


def test_reads_need_a_known_build(client):
    for path in ("cockpit", "gates", "review-comments", "acts"):
        assert client.get(f"/v1/code-tasks/crun_nope/{path}").status_code == 404, path


def test_stop_is_instant_while_the_worker_holds_the_run(client, engine):
    """D1: the worker holds the run row while it checks. Stop must not wait for it: the request
    is recorded at once, every reader says `Stopped by you`, and the worker finishes the stop at
    its next boundary."""
    import time

    from boss_engine.services.code_task_service import honour_stop_request

    run_id = fx.new_run(engine)
    act = fx.stage(engine, run_id, "implement")
    worker = engine.connect()
    held = worker.begin()
    worker.execute(text("SELECT id FROM code_task_runs WHERE id = :r FOR UPDATE"), {"r": run_id})
    try:
        started = time.monotonic()
        res = client.post(f"/v1/code-tasks/{run_id}/cancel", json={"actor": fx.ACTOR})
        took = time.monotonic() - started
        assert res.status_code == 200, res.text
        assert took < 2.0, f"Stop waited {took:.1f}s on the worker's lock"
        assert res.json()["status"] == "stopping"
        cockpit = client.get(f"/v1/code-tasks/{run_id}/cockpit",
                             params={"project_id": fx.PROJECT, "viewer": fx.ACTOR}).json()
        assert cockpit["state"]["word"] == "Stopped by you"
        floor = client.get("/v1/factory/floor",
                           params={"project_id": fx.PROJECT, "viewer": fx.ACTOR}).json()
        assert any(s["run_id"] == run_id and s["word"] == "Stopped by you" for s in floor["stopped"])
        again = client.post(f"/v1/code-tasks/{run_id}/cancel", json={"actor": fx.ACTOR})
        assert again.status_code == 409
    finally:
        held.rollback()
        worker.close()
    with engine.begin() as c:
        honour_stop_request(c, run_id)
    with engine.begin() as c:
        status = c.execute(text("SELECT status FROM code_task_runs WHERE id = :r"),
                           {"r": run_id}).scalar()
        act_status = c.execute(text("SELECT status FROM agent_acts WHERE act_id = :a"),
                               {"a": act["act_id"]}).scalar()
        honoured = c.execute(text("SELECT honoured_at FROM code_task_stop_requests "
                                  "WHERE run_id = :r"), {"r": run_id}).scalar()
    assert status == "cancelled" and act_status == "skipped" and honoured is not None


def test_a_stop_the_build_outran_is_too_late_not_a_stop(client, engine):
    """Walk D1: Stop pressed while the build was opening its PR (one long transaction, so no
    boundary came). The build finished first: it reads as PR open — never `Stopped by you` —
    and the Story says the Stop came too late. While it was still stopping, `k of n` kept the
    stations the stop skipped (`2 of 8`, never `2 of 2`)."""
    run_id = fx.new_run(engine)
    fx.stage(engine, run_id, "implement")
    worker = engine.connect()
    held = worker.begin()
    worker.execute(text("SELECT id FROM code_task_runs WHERE id = :r FOR UPDATE"), {"r": run_id})
    try:
        assert client.post(f"/v1/code-tasks/{run_id}/cancel",
                           json={"actor": fx.ACTOR}).json()["status"] == "stopping"
        stopping = client.get(f"/v1/code-tasks/{run_id}/cockpit",
                              params={"project_id": fx.PROJECT, "viewer": fx.ACTOR}).json()
        assert stopping["state"]["n"] > stopping["state"]["k"]
    finally:
        held.rollback()
        worker.close()
    with engine.begin() as c:
        c.execute(text("UPDATE code_task_runs SET status = 'completed', phase = 'done', "
                       "pull_request_url = 'https://github.com/acme/app/pull/9', "
                       "pull_request_number = 9 WHERE id = :r"), {"r": run_id})
    body = client.get(f"/v1/code-tasks/{run_id}/cockpit",
                      params={"project_id": fx.PROJECT, "viewer": fx.ACTOR}).json()
    assert body["state"]["word"] != "Stopped by you"
    late = [r for r in body["story"] if r["type"] == "you" and r["what"] == "stopped"]
    assert late and late[0]["word"] == "Too late · it had finished"
