"""THE STEP-RECOVERY AGENT READS UNDER THE RUN'S AUTHORITY, READ-ONLY, NEVER AS THE PERSON.

Contributed from an enterprise install (2026-11-19): `_recovery_begin` called
`step_recovery.decide` outside any run authority, so under `BOSS_AUTHZ_MODE=enforce` (the shipped
default) every read the recovery agent made was refused `identity_unavailable`, and it decided
blind. The refusal read "Owner can grant …", so people went looking for a missing grant.

What holds now, measured against the real schema and real Passports (the `staffed` fixture;
only the model is stubbed):
  * a step with an agent: the agent reads with a run token nested under the run
    (`…/step:<step>.recovery`) whose scope is only the delegable keys of the read tools — a
    write is refused under it;
  * a step with no agent: NO tools (evidence only, and why), never the person's full principal;
  * the run's grant has ended: no decision runs, and the awaited step is PARKED on the exact
    sentence until a person renews it (follow_authority_park), never an exception in the poll.
"""
from __future__ import annotations

import json
import types

import pytest
from sqlalchemy import text

from boss_engine.identity import runs
from boss_engine.services import step_recovery
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services.workflow_precheck_agent import read_permissions
from boss_engine.tools.registry import CURRENT_TOOL_AUTH

from .test_identity_agents_workflow_consent import ALICE, env  # noqa: F401  (full-schema fixture)
from .test_no_step_is_blocked_by_consent import (  # noqa: F401
    RUN_INPUT, _publish, _start_consented, staffed)
from .test_pass_two_runs_under_the_runs_authority import _enforce

DEPLOY = {"id": "deploy", "kind": "capability", "next": [],
          "binding": {"capability": "deploy", "inputs": {"run_id": "crun_recovery"}}}


def _run(env, *, captain=None):  # noqa: F811
    step = {**json.loads(json.dumps(DEPLOY)), **({"captain": captain} if captain else {})}
    version = _publish(env, {"entry_step": "deploy", "steps": [step]}, "recovery authority")
    return _start_consented(env, version, RUN_INPUT)


@pytest.fixture()
def model(monkeypatch):
    """The model only: what the recovery agent was handed, and who it ran as."""
    seen: dict = {}

    def ask_model(adapter, *, tools=None, run_tool=None, project_id="", connection=None, **_):
        auth = CURRENT_TOOL_AUTH.get()
        seen["tools"] = tools
        seen["principal"] = getattr(getattr(auth, "principal", None), "id", None)
        rt = getattr(auth, "run_token", None)
        seen["run_token"] = rt
        if rt is not None:
            with connection.engine.begin() as db:
                try:
                    runs.authorize_tool("deployment:create", project_id, db=db)
                    seen["write"] = "allowed"
                except runs.ToolRefused as refused:
                    seen["write"] = refused.reason
        return {"decision": {"action": step_recovery.ASK_PERSON, "cause": "stub",
                             "reasoning": "stub", "question": "Which host?"},
                "cost_usd_micros": 0, "by": {"engine": "stub"}}

    monkeypatch.setattr(step_recovery, "ask_model", ask_model)
    monkeypatch.setattr(step_recovery, "routed_adapter", lambda *a, **k: (object(), object()))
    return seen


def _begin(env, run, *, output=None):  # noqa: F811
    ctx = types.SimpleNamespace(engine=env.engine, settings=None)
    with env.engine.begin() as c:
        return runner._recovery_begin(c, ctx, project_id=env.project, run_id=run["id"],
                                      step_id="deploy", kind="deploy", output=output or {},
                                      evidence={"failure": {"why": "the host refused"}})


def test_the_steps_agent_reads_with_a_read_only_token_nested_under_the_run(
        env, staffed, model, monkeypatch):  # noqa: F811
    _enforce(env, monkeypatch)
    run = _run(env, captain="release_manager")
    entry, _state, agent = _begin(env, run)
    assert agent["role"] == "release_manager"
    assert model["principal"] == staffed["release_manager"], model
    rt = model["run_token"]
    assert rt is not None and any("step:deploy.recovery" in hop for hop in rt.chain.to_boss_chain())
    assert rt.task.run_id.startswith(f"run_wf:{run['id']}"), rt.task.run_id
    assert set(rt.scope) <= read_permissions() and "deployment:create" not in rt.scope
    assert model["write"] != "allowed", model
    assert model["tools"], "the read tools were withheld from an agent holding a read token"
    assert entry["reads"]["tools"] is True and entry["reads"]["as"] == "release_manager"
    assert CURRENT_TOOL_AUTH.get() is None


def test_a_step_with_no_agent_decides_from_evidence_never_as_the_person(
        env, staffed, model, monkeypatch):  # noqa: F811
    _enforce(env, monkeypatch)
    run = _run(env)
    # Even when the step ran as its person (#256: a person-only built-in step).
    entry, _state, _agent = _begin(env, run, output={"ran_as": {"agent": ALICE, "how": "run_actor"}})
    assert model["tools"] is None and model["run_token"] is None
    assert model["principal"] != ALICE, model
    assert entry["reads"]["tools"] is False and entry["reads"]["why"]


def _revoke(env, run):  # noqa: F811
    with env.engine.begin() as c:
        grant = c.execute(text("SELECT grant_id FROM workflow_runs WHERE id = :r"),
                          {"r": run["id"]}).scalar()
        assert grant
        c.execute(text("UPDATE identity_delegation_grants SET status = 'revoked' WHERE id = :g"),
                  {"g": grant})


def test_an_ended_authority_parks_the_step_and_decides_nothing(env, staffed, model, monkeypatch):  # noqa: F811
    _enforce(env, monkeypatch)
    run = _run(env, captain="release_manager")
    _revoke(env, run)
    with pytest.raises(runner.CapabilityExecutionError) as ended:
        _begin(env, run)
    assert step_recovery.authority_message(str(ended.value)) == step_recovery.AUTHORITY_ENDED
    assert model == {}, "a decision ran under an authority that had ended"
    ctx = types.SimpleNamespace(engine=env.engine, settings=None)
    with env.engine.begin() as c:
        row = c.execute(text("SELECT id, step_id FROM workflow_step_runs WHERE workflow_run_id = :r "
                             "AND step_id = 'deploy'"), {"r": run["id"]}).mappings().first()
        assert row is not None
        said = runner._recover_awaited(
            c, ctx, project_id=env.project, run_id=run["id"], step=dict(row), output={},
            handle={}, kind="deploy", failure={"why": "the host refused"},
            evidence={"failure": {"why": "the host refused"}})
        last_error = c.execute(text("SELECT last_error FROM workflow_step_runs WHERE id = :i"),
                               {"i": row["id"]}).scalar()
    assert said["done"]["outcome"] == "parked", said
    assert said["done"]["error"] == step_recovery.AUTHORITY_ENDED
    assert last_error == step_recovery.AUTHORITY_ENDED
    assert model == {}
