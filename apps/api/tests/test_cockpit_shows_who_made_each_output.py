"""Every output a build made names the agent who made it (factory spec §6.3, §8.1).

The cockpit is one read: header state, the Line, the Story, the Outputs. Each output carries
the role of the act that produced it — the tests their tester, the code its builder, a comment
its reviewer, the PR its builder — from acts, never from a hard-coded stage map. `now` names a
tool and a path, never what the agent wrote. A steer is acknowledged by the stage that read it.
And the state word is one: the cockpit, the Floor and MCP get_code_task agree.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from apps.api.tests import _factory_support as fx


def _cockpit(client, run_id, **params):
    r = client.get(f"/v1/code-tasks/{run_id}/cockpit",
                   params={"project_id": fx.PROJECT, **params})
    assert r.status_code == 200, r.text
    return r.json()


def _finished_build(engine):
    run_id = fx.new_run(
        engine, status="completed", phase="done", pr=842, title="Invoice export",
        state={"test_files": ["tests/export.test.ts"], "impl_files": ["src/export.ts"],
               "tests": [{"command": "npm test", "exit_code": 0, "ok": True,
                          "timed_out": False}],
               "last_verify_output": "ok", "qa_summary": "Wrote two tests.",
               "summary": "Added the CSV writer."},
        fctx={"review_comments": [{"id": "rc_1", "round": 0, "severity": "minor",
                                   "body": "Name this better", "path": "src/export.ts",
                                   "line": 3, "author": "adversarial_reviewer",
                                   "author_kind": "agent", "status": "open",
                                   "stage": "review_code"}],
              "waived": {"gates": ["code_review_passed"], "by": "ceo", "reason": "known",
                         "at": "2026-10-04T09:00:00+00:00"},
              "parent_run_id": "crun_earlier", "root_run_id": "crun_earlier", "attempt": 2})
    fx.stage(engine, run_id, "write_tests")
    fx.stage(engine, run_id, "implement")
    fx.stage(engine, run_id, "review_code")
    fx.finish(engine, run_id)
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO quality_gates (id, project_id, code_task_run_id, status, gates_passed, "
            "gates_failed) VALUES (:i, :p, :r, 'waived', '[\"code_written\"]'::jsonb, "
            "'[\"code_review_passed\"]'::jsonb)"),
            {"i": f"qg_{run_id}", "p": fx.PROJECT, "r": run_id})
    return run_id


def test_each_output_names_its_maker(client, engine):
    run_id = _finished_build(engine)
    body = _cockpit(client, run_id)
    out = body["outputs"]
    assert out["changes"]["tests_role_key"] == "qa"
    assert out["changes"]["code_role_key"] == "software_engineer"
    assert out["changes"]["word"] == "2 files"
    assert out["tests"]["role_key"] == "qa"
    assert out["tests"]["word"] == "1 of 1 commands passed"
    assert out["pr"]["role_key"] == "software_engineer" and out["pr"]["number"] == 842
    comment = out["comments"]["items"][0]
    assert comment["author_role_key"] == "adversarial_reviewer"
    gates = {g["name"]: g for g in out["checks"]["gates"]}
    assert gates["code_review_passed"]["role_key"] == "adversarial_reviewer"
    assert gates["code_review_passed"]["waived_by"] == "ceo"
    assert gates["code_written"]["role_key"] == "software_engineer"
    # The Story: the people who shaped it.
    you = {r["what"] for r in body["story"] if r["type"] == "you"}
    assert {"waived", "retried"} <= you
    said = {r["title"]: r["said"] for r in body["story"] if r["type"] == "act"}
    assert said["Write tests"] == "Wrote two tests."
    assert said["Implement"] == "Added the CSV writer."


def test_now_is_a_tool_and_a_path_never_content(client, engine):
    run_id = fx.new_run(engine, title="Live app")
    act = fx.stage(engine, run_id, "implement")
    fx.turn(engine, run_id, act["act_id"], tool="Write",
            content={"file_path": "src/invoices/export.ts", "content": "SECRET_BODY_TEXT"})
    body = _cockpit(client, run_id)
    assert body["now"]["tool"] == "Write"
    assert body["now"]["arg"] == "src/invoices/export.ts"
    assert "SECRET_BODY_TEXT" not in json.dumps(body)
    fx.turn(engine, run_id, act["act_id"], tool="Bash",
            content={"command": "npm test -- --runInBand --coverage --verbose --silent=false"})
    body = _cockpit(client, run_id)
    assert body["now"]["arg"] == "npm test -- --runInBand --coverage --ver"
    assert len(body["now"]["arg"]) <= 40


def test_a_steer_is_read_by_the_next_stage(client, engine):
    from boss_engine.services.governed_code_task_service import take_pending_guidance

    run_id = fx.new_run(engine, title="Steer app")
    fx.stage(engine, run_id, "write_tests")
    sent = client.post(f"/v1/code-tasks/{run_id}/steer",
                       json={"actor": fx.ACTOR, "guidance": "Use the shared CSV writer"})
    assert sent.status_code == 200, sent.text
    queued = [r for r in _cockpit(client, run_id)["story"] if r.get("what") == "steered"]
    assert queued and queued[0]["applied_act_id"] is None
    taken = take_pending_guidance(engine, run_id, stage="implement")
    assert taken and taken[0]["applied_act_id"]
    steered = [r for r in _cockpit(client, run_id)["story"] if r.get("what") == "steered"][0]
    with engine.begin() as c:
        implement = c.execute(text(
            "SELECT act_id FROM agent_acts WHERE code_task_run_id = :r AND step_id = 'implement'"),
            {"r": run_id}).scalar()
    assert steered["applied_act_id"] == implement
    assert steered["applied_role"] == "software_engineer"


def test_every_reader_says_the_same_state(client, engine):
    from boss_engine.services import factory_view

    run_id = fx.new_run(engine, title="One state app")
    fx.stage(engine, run_id, "implement")
    cockpit = _cockpit(client, run_id)
    floor = client.get("/v1/factory/floor", params={"project_id": fx.PROJECT}).json()
    row = next(b for b in floor["builds"] if b["run_id"] == run_id)
    with engine.begin() as c:
        mcp = factory_view.state_of(c, fx.PROJECT, run_id)
    assert cockpit["state"]["state"] == row["state"] == mcp["state"] == "building"
    assert cockpit["state"]["word"] == row["word"] == mcp["word"]
    assert (cockpit["state"]["k"], cockpit["state"]["n"]) == (row["k"], row["n"])


def test_the_cockpit_is_etagged(client, engine):
    run_id = fx.new_run(engine, title="Etag cockpit")
    fx.stage(engine, run_id, "implement")
    first = client.get(f"/v1/code-tasks/{run_id}/cockpit", params={"project_id": fx.PROJECT})
    etag = first.headers["etag"]
    again = client.get(f"/v1/code-tasks/{run_id}/cockpit", params={"project_id": fx.PROJECT},
                       headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert first.json()["live"] is True


def test_activity_says_which_calls_were_included(client, engine):
    run_id = fx.new_run(engine, title="Activity app")
    act = fx.stage(engine, run_id, "implement")
    fx.call(engine, run_id, act["act_id"], micros=500, included=True)
    calls = client.get(f"/v1/code-tasks/{run_id}/activity").json()["calls"]
    assert calls and calls[0]["included_in_subscription"] is True
    cost = _cockpit(client, run_id)["outputs"]["cost"]
    assert cost["word"] == "Included in plan" and cost["metered_usd"] == 0
