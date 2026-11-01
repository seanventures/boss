"""Three engine shapes the DQ RCA pipeline needs (consultant conformance P0 #4):

* A BOUNDED LOOP WITH A MULTI-STEP BODY. ``"body": ["enrich", "history", "rca"]`` runs the three
  in order each round and decides after the last: RCA says InsufficientEvidence and Context
  Enrichment and Historical Pattern run again, with it. The loop judges the LATEST round, and
  ``on_exhausted: continue`` goes on with what the last round found instead of blocking the run.
* SEPARATION OF DUTIES. A gate whose approval says ``distinct_from: [approve]`` must be answered
  by a different person than whoever approved ``approve``: the engine takes back such an approval
  however it was recorded, and says why; the inbox refuses it first.
* A STAGE THE PIPELINE DRAWS BUT THIS FLOW NEVER STARTS (a shared service, or a side path on its
  own trigger) is not refused as unreachable, and a run completes around it.
"""
from __future__ import annotations

import uuid

from sqlalchemy import text

from boss_engine.services import workflows

from .test_workflow_capability_runner import (  # noqa: F401 - fixtures used by name
    _start,
    eng,
    project,
)

WORKER = "worker-loop"


def _problems(steps, entry="a"):
    _, problems = workflows.normalise_workflow_spec(
        {"entry_step": entry, "steps": steps}, check_agents=False)
    return problems


def _run(c, project_id, steps, entry="a"):
    key = f"lp_{uuid.uuid4().hex[:6]}"
    definition = workflows.create_definition(
        c, project_id=project_id, workflow_key=key, name=key, description=None,
        actor="test", namespace="product")
    version = workflows.publish_version(
        c, project_id=project_id, definition_id=definition["id"],
        spec={"entry_step": entry, "steps": steps}, actor="test")
    return _start(c, project_id, version["id"], {})["id"]


def _finish(c, project_id, run_id, step_id, output=None):
    claimed = workflows.claim_step(c, project_id=project_id, run_id=run_id, worker_id=WORKER,
                                   lease_seconds=600)
    assert claimed is not None and claimed["step_id"] == step_id, claimed
    workflows.complete_step(c, project_id=project_id, step_run_id=claimed["id"],
                            worker_id=WORKER, output=output or {step_id: True}, evidence=[])
    return claimed


def _status(c, run_id):
    return c.execute(text("SELECT status FROM workflow_runs WHERE id=:r"), {"r": run_id}).scalar()


def _loop_spec(**loop):
    return [
        {"id": "a", "kind": "task", "next": ["loop"]},
        {"id": "loop", "kind": "bounded_loop", "body": ["enrich", "history", "rca"],
         "max_iterations": 3,
         "expression": {"path": "steps.rca.output.verdict", "op": "eq",
                        "value": "InsufficientEvidence"},
         "next": ["after"], **loop},
        {"id": "enrich", "kind": "task", "next": []},
        {"id": "history", "kind": "task", "next": []},
        {"id": "rca", "kind": "task", "next": []},
        {"id": "after", "kind": "task", "next": []},
    ]


def test_a_multi_step_body_validates_and_a_member_with_next_steps_is_refused():
    assert _problems(_loop_spec(on_exhausted="continue")) == []
    steps = _loop_spec()
    steps[3]["next"] = ["rca"]
    said = [(p["step_id"], p["message"]) for p in _problems(steps)]
    assert any(sid == "history" and "cannot have next steps" in s for sid, s in said), said
    bad = _loop_spec(on_exhausted="sometimes")
    assert any(p["field"] == "on_exhausted" for p in _problems(bad))
    missing = _loop_spec()
    missing[1]["body"] = ["enrich", "nowhere"]
    assert any("nowhere" in p["message"] for p in _problems(missing))


def test_insufficient_evidence_runs_all_three_again_and_stops_when_the_round_is_conclusive(
        eng, project):
    with eng.begin() as c:
        run_id = _run(c, project, _loop_spec(on_exhausted="continue"))
        _finish(c, project, run_id, "a")
        # Round 1: RCA cannot conclude.
        _finish(c, project, run_id, "enrich")
        _finish(c, project, run_id, "history")
        _finish(c, project, run_id, "rca", {"verdict": "InsufficientEvidence"})
        # Round 2 re-runs Enrichment AND Historical Pattern before RCA, in order.
        _finish(c, project, run_id, "enrich")
        _finish(c, project, run_id, "history")
        _finish(c, project, run_id, "rca", {"verdict": "Candidates"})
        _finish(c, project, run_id, "after")
        assert _status(c, run_id) == "completed"
        rounds = c.execute(text(
            "SELECT step_id, count(*) FROM workflow_step_runs WHERE workflow_run_id=:r "
            "AND status='succeeded' GROUP BY step_id"), {"r": run_id}).all()
        # The loop decides once per round, then once more to stop.
        assert dict(rounds) == {"a": 1, "loop": 3, "enrich": 2, "history": 2, "rca": 2,
                                "after": 1}


def test_exhausted_rounds_go_on_with_the_last_finding_when_the_loop_says_continue(eng, project):
    with eng.begin() as c:
        run_id = _run(c, project, _loop_spec(on_exhausted="continue"))
        _finish(c, project, run_id, "a")
        for _round in range(3):
            _finish(c, project, run_id, "enrich")
            _finish(c, project, run_id, "history")
            _finish(c, project, run_id, "rca", {"verdict": "InsufficientEvidence"})
        _finish(c, project, run_id, "after")
        assert _status(c, run_id) == "completed"
        loop = c.execute(text(
            "SELECT output FROM workflow_step_runs WHERE workflow_run_id=:r AND step_id='loop' "
            "AND status='succeeded' ORDER BY iteration DESC LIMIT 1"), {"r": run_id}).scalar()
        assert loop["exhausted"] is True and "3 round" in loop["note"]


def test_exhausted_rounds_still_block_by_default(eng, project):
    with eng.begin() as c:
        run_id = _run(c, project, _loop_spec())
        _finish(c, project, run_id, "a")
        for _round in range(3):
            _finish(c, project, run_id, "enrich")
            _finish(c, project, run_id, "history")
            _finish(c, project, run_id, "rca", {"verdict": "InsufficientEvidence"})
        assert _status(c, run_id) == "blocked"


def _duties_spec():
    return [
        {"id": "a", "kind": "task", "next": ["approve"]},
        {"id": "approve", "kind": "decision", "title": "Human approval",
         "question": "Approve the fix?", "approval": {"mode": "manual", "approver_role": "human"},
         "next": ["execute"]},
        {"id": "execute", "kind": "decision", "title": "Human execution",
         "question": "Carry it out?",
         "approval": {"mode": "manual", "approver_role": "human",
                      "distinct_from": ["approve"]},
         "next": ["after"]},
        {"id": "after", "kind": "task", "next": []},
        # Drawn, never started by this flow: a shared service and a side path.
        {"id": "curator", "kind": "task", "title": "Context Curator", "next": [],
         "contract": {"design": {"status": "shared_service", "reference": "Chapter to write"}}},
        {"id": "escalation", "kind": "task", "title": "Escalation Agent", "next": [],
         "side_path": "Daily over every open incident"},
    ]


def _decision_of(c, run_id, step_id):
    return c.execute(text("SELECT decision_id FROM workflow_step_runs WHERE workflow_run_id=:r "
                          "AND step_id=:s"), {"r": run_id, "s": step_id}).scalar()


def _approve(c, decision_id, by):
    c.execute(text("UPDATE decisions SET status='approved', approved_by=:b, approved_at=now() "
                   "WHERE id=:i"), {"b": by, "i": decision_id})


def test_the_approver_cannot_also_carry_it_out_and_the_engine_says_why(eng, project):
    assert _problems(_duties_spec()) == [], "a shared service and a side path are not unreachable"
    still = _duties_spec()
    still[4].pop("contract")
    assert any(p["step_id"] == "curator" and "cannot be reached" in p["message"].lower()
               for p in _problems(still))
    with eng.begin() as c:
        run_id = _run(c, project, _duties_spec())
        _finish(c, project, run_id, "a")
        workflows.advance_run(c, project, run_id)
        _approve(c, _decision_of(c, run_id, "approve"), "dana.approver")
        workflows.advance_run(c, project, run_id)
        execute = _decision_of(c, run_id, "execute")
        assert execute, "the execution gate is asked once the approval stands"
        # The inbox's own check: the approver is refused, in words, before anything is recorded.
        said = workflows.separation_of_duties_refusal(c, execute, "dana.approver")
        assert said and said.startswith("A different person must carry this out")
        assert "dana.approver approved it at “Human approval”" in said
        assert workflows.separation_of_duties_refusal(c, execute, "lee.engineer") is None
        # Recorded by another road anyway: the engine takes it back, and the gate waits on.
        _approve(c, execute, "Dana.Approver")
        workflows.advance_run(c, project, run_id)
        row = c.execute(text("SELECT status, approved_by, resolution_note FROM decisions "
                             "WHERE id=:i"), {"i": execute}).mappings().one()
        assert row["status"] == "pending" and row["approved_by"] is None
        assert "separation of duties" in row["resolution_note"]
        step = c.execute(text("SELECT status FROM workflow_step_runs WHERE workflow_run_id=:r "
                              "AND step_id='execute'"), {"r": run_id}).scalar()
        assert step == "waiting"
        # A different person carries it out; the run goes on and completes around the drawn-only
        # stages.
        _approve(c, execute, "lee.engineer")
        workflows.advance_run(c, project, run_id)
        _finish(c, project, run_id, "after")
        assert _status(c, run_id) == "completed"
        refused = c.execute(text("SELECT count(*) FROM events WHERE entity_id=:r AND "
                                 "event_type='workflow.separation_of_duties_refused'"),
                            {"r": run_id}).scalar()
        assert refused == 1
