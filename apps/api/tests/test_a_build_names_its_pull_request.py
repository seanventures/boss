"""A build's pull request is credited to the agent who built it (agents-home spec §6.5).

`close_code_task` closed a build's stages and attached nothing, so the only `pr` artifact in
the ledger came from a workflow step that echoed a PR URL — and "PR #842 opened" on Agents ›
Live had no agent to put a face on. The build's engineering stage now carries `{kind: pr,
ref: '#n', url}` from code_task_runs, once, and the Live board's Done lately shows it.
"""
from __future__ import annotations

import uuid

from sqlalchemy import text

PROJECT = "boss-dogfood"


def test_a_finished_build_signs_its_pull_request_on_the_engineering_stage(client, engine):
    from boss_engine.services import agent_acts

    run_id = f"ctr_{uuid.uuid4().hex[:16]}"
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec) "
            "VALUES (:i, :p, 'running', 'coding', 'Ship it', '{}'::jsonb)"),
            {"i": run_id, "p": PROJECT})
        for stage in ("plan", "implement", "review_code"):
            assert agent_acts.stage_act(c, project_id=PROJECT, code_task_run_id=run_id,
                                        stage=stage)
        c.execute(text(
            "UPDATE code_task_runs SET status = 'completed', phase = 'done', "
            "pull_request_url = 'https://github.example/acme/app/pull/842', "
            "pull_request_number = 842 WHERE id = :i"), {"i": run_id})
        agent_acts.close_code_task(c, run_id)
        agent_acts.close_code_task(c, run_id)  # a second settle adds nothing
        stages = {r["step_id"]: r for r in c.execute(text(
            "SELECT step_id, role_key, artifacts FROM agent_acts WHERE code_task_run_id = :i "
            "AND kind = 'stage'"), {"i": run_id}).mappings()}
    prs = {k: [a for a in v["artifacts"] if a["kind"] == "pr"] for k, v in stages.items()}
    assert prs["implement"] == [{"kind": "pr", "ref": "#842",
                                 "url": "https://github.example/acme/app/pull/842"}]
    assert prs["plan"] == [] and prs["review_code"] == []

    board = client.get("/v1/agents/live-board", params={"project_id": PROJECT}).json()
    chip = next(o for o in board["outcomes"] if o.get("url", "").endswith("/pull/842"))
    assert chip["text"] == "PR #842 opened"
    assert chip["kind"] == "pr"
    assert chip["role_key"] == stages["implement"]["role_key"]
