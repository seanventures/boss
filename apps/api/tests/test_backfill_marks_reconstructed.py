"""Work done before the ledger existed is rebuilt, and says so (Agent World §5.1, §12.4 test 7).

A run's steps (from `ran_as`), a panel's seats (from `meeting_runs.role_keys`) and a build's
stages (from the order of its `llm_calls`) become acts marked `reconstructed`, which the UI draws
dashed with "Reconstructed". Running it again writes nothing: it is idempotent per source row.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from apps.api.tests import workflow_management_support as wf


def _legacy_rows(engine) -> None:
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO meeting_runs (id, project_id, status, phase, directive, role_keys, "
            "title, created_at, updated_at) VALUES ('mrun_legacy_aw', :p, 'completed', 'done', "
            "'Should we ship dark mode?', CAST(:rk AS JSONB), 'Dark mode', "
            "now() - interval '3 days', now() - interval '3 days')"),
            {"p": wf.PROJECT, "rk": json.dumps(["product_manager", "engineer"])})
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, phase, title, finalize_ctx, "
            "created_at) VALUES ('ctr_legacy_aw', :p, 'completed', 'done', 'Dark mode', "
            "CAST(:f AS JSONB), now() - interval '2 days')"),
            {"p": wf.PROJECT, "f": json.dumps({"actor": "ceo"})})
        for minutes, stage in ((50, "plan"), (40, "implement"), (30, "review_code"),
                               (20, "repair"), (10, "review_code")):
            c.execute(text(
                "INSERT INTO llm_calls (id, project_id, code_task_run_id, stage, engine, model, "
                "role, prompt_tokens, completion_tokens, total_tokens, cost_usd_micros, ok, "
                "created_at) VALUES (:i, :p, 'ctr_legacy_aw', :s, 'stub', 'stub-model', NULL, "
                "10, 5, 15, 1000, true, now() - interval '2 days' + make_interval(mins => :m))"),
            {"i": f"llmc_legacy_{stage}_{minutes}", "p": wf.PROJECT, "s": stage,
             "m": 60 - minutes})


def _backfill(client) -> int:
    done = client.post("/v1/acts/backfill", json={"project_id": wf.PROJECT, "actor": "ceo"})
    assert done.status_code == 200, done.text
    return done.json()["reconstructed"]


def test_backfill_rebuilds_runs_seats_and_stages_once_and_marks_them(client, engine, monkeypatch):
    wf.install_model(monkeypatch)
    skill = wf.skills(client, 1)[0]
    worker = wf.new_agent(client, wf.key("legacy_worker"), holds=(skill,))
    made = wf.published(client, {
        "entry_step": "draft", "input_schema": wf.START_FORM,
        "steps": [wf.agent_step("draft", captain=worker, skills=[skill])]})
    run_id = wf.started(client, made["definition"]["workflow_key"], {"instructions": "Draft."})
    wf.tick(client)
    assert wf.run_view(client, run_id)["status"] == "completed"
    # As if it ran before the ledger: no acts, nothing signed on the calls.
    with engine.begin() as c:
        c.execute(text("DELETE FROM agent_acts"))
    _legacy_rows(engine)

    written = _backfill(client)
    assert written > 0
    assert _backfill(client) == 0, "a second backfill wrote again"

    with engine.begin() as c:
        acts = [dict(r) for r in c.execute(text(
            "SELECT * FROM agent_acts WHERE project_id = :p ORDER BY started_at"),
            {"p": wf.PROJECT}).mappings()]
        unsigned = c.execute(text(
            "SELECT count(*) FROM llm_calls WHERE code_task_run_id = 'ctr_legacy_aw' "
            "AND act_id IS NULL")).scalar()
    assert len(acts) == written
    assert all(a["reconstructed"] for a in acts)

    # The run: its root and the named step, connected.
    run_acts = [a for a in acts if a["workflow_run_id"] == run_id]
    assert sorted(a["kind"] for a in run_acts) == ["run", "step"]
    step = next(a for a in run_acts if a["kind"] == "step")
    assert (step["role_key"], step["how"], step["status"]) == (worker, "named", "complete")

    # The panel: one seat per seat, its titles resolved to role keys.
    seats = [a for a in acts if a["kind"] == "seat"]
    assert sorted(a["role_key"] for a in seats) == ["pm", "software_engineer"]
    assert all(a["status"] == "complete" and a["how"] == "seated" for a in seats)

    # The build: a stage per stage in call order, the repair loop as rounds, every call signed.
    stages = [a for a in acts if a["kind"] == "stage" and a["code_task_run_id"] == "ctr_legacy_aw"]
    assert [a["step_id"] for a in stages] == [
        "plan", "implement", "review_code", "repair", "review_code"]
    assert unsigned == 0
    assert all(a["ended_at"] is not None for a in stages)
    assert float(sum(a["cost_usd"] for a in stages)) == 0.005

    # The UI draws these dashed: the edges say they were reconstructed.
    graph = client.get("/v1/agents/adversarial_reviewer/lineage",
                       params={"project_id": wf.PROJECT, "days": 30}).json()
    assert graph["edges"] and all(e["reconstructed"] for e in graph["edges"])
    chain = client.get(f"/v1/workflow-runs/{run_id}/chain",
                       params={"project_id": wf.PROJECT}).json()
    assert all(a["reconstructed"] for a in chain["acts"])
