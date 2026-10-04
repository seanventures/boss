"""A panel a workflow step convenes is signed under that step (Agent World §5.1 hook 5).

The panel is convened by the `request_meeting` tool in the tool's OWN transaction, while the
tick that called it — and the step's act — are still uncommitted. So the seats cannot find the
step by looking. Before this was fixed they did not: each such panel got a stray `Panel · …`
root accountable to a made-up person, "operator", every seat hung off it for ever, the root was
never closed, and the workflow's chain and Credits had no seats at all.

Driven through the HTTP API the console and the worker use, on the real database; the panel is
convened by the real tool. Only the panel's own model work is not run: the test ends it.
"""
from __future__ import annotations

from sqlalchemy import text

from apps.api.tests import workflow_management_support as wf


def _acts(engine, where: str, **params) -> list[dict]:
    with engine.begin() as c:
        return [dict(r) for r in c.execute(
            text(f"SELECT * FROM agent_acts WHERE {where} ORDER BY started_at, act_id"),
            params).mappings()]


def test_a_panel_a_step_convenes_sits_under_that_step_and_nobody_is_made_up(client, engine,
                                                                            monkeypatch):
    wf.install_model(monkeypatch)
    spec = {
        "entry_step": "panel",
        "input_schema": wf.START_FORM,
        "steps": [
            {"id": "panel", "kind": "capability", "next": [], "max_attempts": 1,
             "captain": "pm",
             "binding": {"capability": "meeting",
                         "inputs": {"directive": {"from_input": "instructions"}}}},
        ],
    }
    made = wf.published(client, spec)
    started = wf.start(client, made["definition"]["workflow_key"],
                       {"instructions": "Should we ship dark mode for the console?"},
                       start_anyway=["model", "repository", "github"])
    assert started.status_code == 200, started.text
    run_id = started.json()["run"]["id"]

    for _ in range(2):
        wf.tick(client)
    view = wf.run_view(client, run_id)
    assert wf.statuses(view)["panel"] == "waiting", wf.statuses(view)
    meeting_run_id = wf.steps_of(view)["panel"]["output"]["meeting_run_id"]

    chain = client.get(f"/v1/workflow-runs/{run_id}/chain",
                       params={"project_id": wf.PROJECT, "viewer": "ceo"}).json()
    step = next(a for a in chain["acts"] if a["kind"] == "step")
    seats = [a for a in chain["acts"] if a["kind"] == "seat"]

    # THE SEATS ARE IN THE RUN'S CHAIN, under the step that convened them.
    assert seats, "the panel's seats are not in the run's chain"
    assert {a["parent_act_id"] for a in seats} == {step["act_id"]}
    assert all(a["links"]["meeting_run_id"] == meeting_run_id for a in seats)
    assert all(a["accountable"] == "ceo" for a in seats)
    credited = {c["role_key"] for c in chain["credits"]}
    assert {a["role_key"] for a in seats} <= credited

    # NO STRAY ROOT, NO MADE-UP PERSON — anywhere in the project.
    assert _acts(engine, "subject_kind = 'meeting_run' AND kind = 'run'") == []
    assert _acts(engine, "accountable = 'operator' OR handed_by_id = 'operator'") == []
    panel_acts = _acts(engine, "meeting_run_id = :m", m=meeting_run_id)
    assert {a["kind"] for a in panel_acts} == {"step", "seat"}

    # The panel ends: its seats close, and nothing of it is left open.
    with engine.begin() as c:
        c.execute(text("UPDATE meeting_runs SET status = 'completed', phase = 'done' "
                       "WHERE id = :m"), {"m": meeting_run_id})
    wf.tick(client)
    still_open = [a for a in _acts(engine, "meeting_run_id = :m AND ended_at IS NULL",
                                   m=meeting_run_id) if a["kind"] != "step"]
    assert still_open == []


def test_a_panel_nobody_recorded_says_boss_and_closes(client, engine):
    """A panel convened with no person recorded (Plane, an old caller) is BOSS's, said so."""
    from boss_engine.services import agent_acts, meeting_service

    with engine.begin() as c:
        meeting_id = meeting_service.start_meeting_run(
            c, project_id=wf.PROJECT, task_id=None, directive="Is the importer safe?",
            url="", constraints={}, role_keys=["qa"], finalize_ctx={}, title="Importer")
    root = _acts(engine, "kind = 'run' AND subject_kind = 'meeting_run' AND subject_id = :m",
                 m=meeting_id)
    assert [(r["kind"], r["accountable"], r["handed_by_kind"]) for r in root] == [
        ("run", agent_acts.BOSS, "boss")]
    got = client.get(f"/v1/acts/{root[0]['act_id']}",
                     params={"project_id": wf.PROJECT, "viewer": "ceo"}).json()
    assert got["edge_label"] == "Started by BOSS"

    with engine.begin() as c:
        c.execute(text("UPDATE meeting_runs SET status = 'completed' WHERE id = :m"),
                  {"m": meeting_id})
        agent_acts.reconcile(c, wf.PROJECT)
    assert _acts(engine, "meeting_run_id = :m AND ended_at IS NULL", m=meeting_id) == []
