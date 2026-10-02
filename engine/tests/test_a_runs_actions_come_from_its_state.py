"""A run's buttons come from its actual state, decided once, in the engine.

The incident: a cancelled Standard Product Delivery run whose Research step had FAILED (✗ on the
rail, panel cancelled) still offered "Build on this research →". The product owner: "Does it
make sense to have Build on this research? these small bugs and experience give quality." The
console decided that button — and Stop, Run again, Remove, Re-check, Try again and Skip — with
its own conditions, each a private copy of a rule the engine enforces elsewhere, and they
disagreed with the engine wherever it mattered.

`workflow_run_actions._verdicts` is now the only place those decisions are made, and — since the
review of the first fix found Try again hidden on a cancelled run while the endpoint, the
engine and the agent's tool still accepted it — the only place they are ENFORCED: every
operation on a run calls `require`, which reads the same verdicts. This file is its table: every
action across the run states an operator actually meets, the parity between what is shown and
what is allowed, and an enumeration that holds every operation to asking.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from boss_engine.services import workflow_run_actions as actions_mod
from boss_engine.services import workflows
from boss_engine.services import workflow_recheck
from boss_engine.services.workflow_run_actions import decide, run_actions

DOSSIER_OK = {"drun_ok": {"project_id": "p", "status": "completed", "has_findings": True}}


def step(step_id, status, capability=None, output=None, kind="capability", completed_at=None,
         created_at=None):
    return {"step_id": step_id, "status": status, "capability": capability or step_id,
            "kind": kind, "output": output or {}, "completed_at": completed_at,
            "created_at": created_at}


def researched(status="succeeded", discovery="drun_ok", **extra):
    return step("research", status, "research", {"discovery_id": discovery, **extra})


def summary(run_status, steps, discoveries=None, run_input=None):
    """{key or key:step -> enabled} — the shape the table below is written in."""
    out = {}
    for action in run_actions({"status": run_status, "input": run_input or {}}, steps,
                              discoveries if discoveries is not None else DOSSIER_OK):
        name = action["key"] + (f":{action['step_id']}" if "step_id" in action else "")
        assert name not in out, f"{name} offered twice"
        if not action["enabled"]:
            assert action["reason"], f"{name} is disabled without saying why"
        out[name] = action["enabled"]
    return out


#: (case, run status, steps, discoveries, expected {action: enabled}). Anything not listed must
#: be ABSENT — the table is exhaustive per row, which is what makes it enforcing.
CASES = [
    ("running", "running",
     [researched(), step("panel", "succeeded", "meeting"), step("build", "running", "build")],
     None, {"stop": True, "stop_and_rerun": True}),
    ("waiting on an approval", "waiting",
     [researched(), step("approve", "waiting", "spec_review")],
     None, {"stop": True, "stop_and_rerun": True}),
    ("failed at research — stopped for the operator", "failed",
     [step("research", "failed", "research"), step("panel", "pending", "meeting")],
     None,
     {"recheck": True, "rerun": True, "remove": True,
      "retry_step:research": True, "skip_step:research": True}),
    # A STOPPED RUN OFFERS NO TRY AGAIN — not greyed, absent. It will never become possible
    # (Stop promises "it cannot be resumed"), and a dead button under the red ✗ is the
    # incident's bad experience with less colour. REWRITTEN from `retry_step:panel: False`.
    ("research succeeded, then the run was cancelled", "cancelled",
     [researched(), step("panel", "cancelled", "meeting")],
     None,
     {"rerun": True, "remove": True, "build_on_research": True}),
    ("completed", "completed",
     [researched(), step("panel", "succeeded", "meeting"), step("build", "succeeded", "build")],
     None, {"rerun": True, "remove": True, "build_on_research": True}),
    ("cancelled before research ran", "cancelled",
     [step("research", "cancelled", "research")],
     None, {"rerun": True, "remove": True}),
    # THE INCIDENT, in both shapes it reached the screen in: a pre-2026-10-02 research step the
    # runner recorded `succeeded` with output `failed_continued`, and a research step that failed
    # outright. Neither offers Build on this research — nor, on a cancelled run, a greyed Try
    # again or Skip under the ✗ (REWRITTEN: these rows used to pin both as present-disabled).
    ("INCIDENT: cancelled, research recorded succeeded but failed_continued", "cancelled",
     [researched(discovery="drun_dead", status="succeeded")
      | {"output": {"discovery_id": "drun_dead", "status": "failed_continued"}},
      step("panel", "cancelled", "meeting")],
     {"drun_dead": {"project_id": "p", "status": "failed", "has_findings": True}},
     {"rerun": True, "remove": True}),
    ("INCIDENT: cancelled, research failed outright", "cancelled",
     [researched(status="failed", discovery="drun_dead"), step("panel", "cancelled", "meeting")],
     {"drun_dead": {"project_id": "p", "status": "failed", "has_findings": False}},
     {"rerun": True, "remove": True}),
    # failed_continued ON ITS OWN. Every incident row above also gives the dossier a failed
    # status, so the dossier check alone passed them — removing "failed_continued" from
    # FAILED_OUTPUT_STATUSES left this whole file green. Here the dossier the step names
    # COMPLETED with findings: only the step's own report says the research failed.
    ("research recorded failed_continued; its dossier later completed with findings",
     "cancelled",
     [researched(discovery="drun_ok") | {"output": {"discovery_id": "drun_ok",
                                                    "status": "failed_continued"}}],
     None, {"rerun": True, "remove": True}),
    ("completed: an old failed step offers nothing — the run is over", "completed",
     [step("notify", "failed", "notify")], None, {"rerun": True, "remove": True}),
    ("compensated: undone, nothing in it runs again", "compensated",
     [step("deploy", "failed", "deploy")], None, {"rerun": True, "remove": True}),
    ("a retried step's earlier failed attempt is history, not a button", "running",
     [step("research", "failed", "research", created_at="2026-10-01T10:00:00"),
      step("research", "running", "research", created_at="2026-10-01T10:05:00")],
     None, {"stop": True, "stop_and_rerun": True}),
    ("research step succeeded but its discovery failed", "completed",
     [researched(discovery="drun_dead")],
     {"drun_dead": {"project_id": "p", "status": "failed", "has_findings": True}},
     {"rerun": True, "remove": True}),
    ("research completed with an empty dossier", "completed",
     [researched(discovery="drun_empty")],
     {"drun_empty": {"project_id": "p", "status": "completed", "has_findings": False}},
     {"rerun": True, "remove": True}),
    ("research dossier from another project (not loaded)", "completed",
     [researched(discovery="drun_elsewhere")], {}, {"rerun": True, "remove": True}),
    ("research still being written up", "completed",
     [researched(discovery="drun_busy")],
     {"drun_busy": {"project_id": "p", "status": "running", "has_findings": False}},
     {"rerun": True, "remove": True, "build_on_research": False}),
    ("research skipped by the operator", "completed",
     [researched(status="skipped", discovery="drun_ok")],
     None, {"rerun": True, "remove": True}),
    ("research not applicable (project started directly)", "completed",
     [researched() | {"output": {"discovery_id": "drun_ok", "status": "not_applicable"}}],
     None, {"rerun": True, "remove": True}),
    ("a run still live past its research does not offer a second workflow", "running",
     [researched(), step("build", "running", "build")],
     None, {"stop": True, "stop_and_rerun": True}),
    ("blocked: stoppable, not removable", "blocked",
     [step("deploy", "deadlettered", "deploy")],
     None,
     {"recheck": True, "stop": True, "stop_and_rerun": True,
      "retry_step:deploy": True, "skip_step:deploy": True}),
    ("paused", "paused", [step("build", "ready", "build")],
     None, {"stop": True, "stop_and_rerun": True}),
    ("compensating: neither stopped nor removed until it finishes", "compensating",
     [step("deploy", "failed", "deploy")],
     None,
     {"stop": False, "remove": False,
      "retry_step:deploy": False, "skip_step:deploy": False}),
    ("a refused approval: Try again reopens it, Skip never offered", "failed",
     [step("approve", "failed", "spec_review")],
     None, {"recheck": True, "rerun": True, "remove": True, "retry_step:approve": True}),
    ("a failed decision step is a gate too", "failed",
     [step("gate", "failed", kind="decision", capability="")],
     None, {"recheck": True, "rerun": True, "remove": True, "retry_step:gate": True}),
]


@pytest.mark.parametrize("case,status,steps,discoveries,expected", CASES,
                         ids=[c[0] for c in CASES])
def test_each_run_state_offers_exactly_its_actions(case, status, steps, discoveries, expected):
    assert summary(status, steps, discoveries) == expected, case


def test_a_run_that_adopted_research_at_start_can_still_be_built_on():
    """No research step of its own, but it started from a finished dossier
    (`adopt_discovery_run_id`, set by the server) — the dossier is still there to build on."""
    got = summary("completed", [step("build", "succeeded", "build")],
                  run_input={"adopt_discovery_run_id": "drun_ok"})
    assert got == {"rerun": True, "remove": True, "build_on_research": True}


def test_research_is_found_by_what_it_produced_not_by_its_name():
    """A customer may rename the step; the await handle is the contract."""
    renamed = step("look_around", "succeeded", "custom_thing",
                   {"await": {"kind": "discovery", "id": "drun_ok"}})
    assert summary("completed", [renamed])["build_on_research"] is True


def test_every_disabled_action_says_why_and_every_action_has_a_label():
    for _case, status, steps, discoveries, _ in CASES:
        for action in run_actions({"status": status, "input": {}}, steps,
                                  discoveries if discoveries is not None else DOSSIER_OK):
            assert action["label"].strip()
            assert set(action) >= {"key", "label", "enabled", "reason"}
            assert action["enabled"] or len(action["reason"]) > 10


def test_every_action_key_is_a_declared_one():
    seen = set()
    for _case, status, steps, discoveries, _ in CASES:
        for action in run_actions({"status": status, "input": {}}, steps,
                                  discoveries if discoveries is not None else DOSSIER_OK):
            seen.add(action["key"])
    declared = set(actions_mod.RUN_ACTION_KEYS) | set(actions_mod.STEP_ACTION_KEYS)
    assert seen == declared, "the table above must exercise every action this module can offer"


def test_its_state_lists_are_the_ones_the_engine_enforces():
    """Remove is offered exactly where `delete_run` allows it, Try again where
    `retry_failed_step` does, Skip where `skip_failed_step` does. A drift here is a button that
    can only fail, or work the operator cannot reach."""
    assert actions_mod.FINISHED_RUN_STATES == workflows.TERMINAL_RUN_STATES
    assert actions_mod.RETRYABLE_STEP_STATES == set(workflows.OPERATOR_RECOVERABLE_STEP_STATES)
    assert actions_mod.SKIPPABLE_STEP_STATES == set(workflows.SKIPPABLE_STEP_STATES)


def test_every_run_status_the_database_allows_is_classified():
    """A status the schema allows and this module has never heard of would get no Stop and no
    Remove — a run nobody can get rid of."""
    schema = (Path(__file__).resolve().parents[2] / "db" / "schema.sql").read_text()
    block = schema[schema.index("CREATE TABLE IF NOT EXISTS workflow_runs"):]
    check = block[block.index("CHECK (status IN ("):block.index("))")]
    statuses = set(re.findall(r"'([a-z_]+)'", check))
    classified = (actions_mod.ACTIVE_RUN_STATES | actions_mod.FINISHED_RUN_STATES
                  | {actions_mod.COMPENSATING})
    assert statuses == classified
    assert not actions_mod.ACTIVE_RUN_STATES & actions_mod.FINISHED_RUN_STATES


# ── What is shown is what is allowed ────────────────────────────────────────────────────────

def _every_question(steps):
    yield from ((key, None) for key in actions_mod.RUN_ACTION_KEYS)
    for item in steps:
        for key in actions_mod.STEP_ACTION_KEYS:
            yield key, item["step_id"]


@pytest.mark.parametrize("case,status,steps,discoveries,expected", CASES,
                         ids=[c[0] for c in CASES])
def test_the_list_and_the_gate_are_one_decision(case, status, steps, discoveries, expected):
    """The review of the first fix: Try again was hidden on a cancelled run while the endpoint
    still accepted it — the button and the action disagreeing, the class of bug this module
    exists to end. `require` reads `decide`; this holds `decide` to the list, for every action
    in every row: shown-enabled exactly where it is allowed, shown-disabled or absent exactly
    where it is refused, and every refusal carrying a sentence."""
    shown = summary(status, steps, discoveries)
    run = {"status": status, "input": {}}
    for key, step_id in _every_question(steps):
        verdict = decide(run, steps, discoveries if discoveries is not None else DOSSIER_OK,
                         key, step_id)
        name = key + (f":{step_id}" if step_id else "")
        if verdict["state"] == "absent":
            assert name not in shown, f"{case}: {name} is shown but refused as not applying"
        else:
            assert shown.get(name) == (verdict["state"] == "enabled"), f"{case}: {name}"
        if verdict["state"] != "enabled":
            assert verdict["reason"] and len(verdict["reason"]) > 10, \
                f"{case}: {name} is refused without saying why"


def test_a_finished_run_never_shows_a_step_action_it_will_never_allow():
    """Disabled means "not right now"; on a run that is stopped, completed or undone nothing
    will ever run again, so Try again and Skip are left out there, not greyed out."""
    for status in actions_mod.CLOSED_RUN_STATES:
        steps = [step("research", "failed", "research"), step("deploy", "cancelled", "deploy")]
        keys = {a["key"] for a in run_actions({"status": status, "input": {}}, steps, {})}
        assert not keys & set(actions_mod.STEP_ACTION_KEYS), status
        assert "recheck" not in keys, status


# ── Every operation asks ────────────────────────────────────────────────────────────────────

#: The operation that performs each action, and so must ask `require` for it before changing
#: anything. The console's buttons, the agent's tools and a raw API call all land in these.
ENFORCED_BY = {
    "retry_step": workflows.retry_failed_step,
    "skip_step": workflows.skip_failed_step,
    "stop": workflows.cancel_run,
    "remove": workflows.delete_run,
    "recheck": workflow_recheck.recheck_run,
}

#: Actions with no source run on the server to check: "Run this workflow again" and the
#: second half of "Stop and run again" START a new run (the first half is `stop`, enforced
#: above). Named here so that a new action key has to be classified one way or the other.
STARTS_A_NEW_RUN = {"rerun", "stop_and_rerun"}


def test_every_action_is_enforced_by_the_operation_that_performs_it():
    """An enumeration, so the next action cannot ship as a button its endpoint ignores.
    `build_on_research` is performed by the start endpoint's `from_run` (asserted below)."""
    every = set(actions_mod.RUN_ACTION_KEYS) | set(actions_mod.STEP_ACTION_KEYS)
    assert set(ENFORCED_BY) | STARTS_A_NEW_RUN | {"build_on_research"} == every
    for key, operation in ENFORCED_BY.items():
        source = inspect.getsource(operation)
        assert "require(" in source and f'"{key}"' in source, (
            f"{operation.__name__} performs '{key}' without asking the run's actions")
    main = (Path(__file__).resolve().parents[2] / "apps" / "api" / "app" / "main.py").read_text()
    start = main[main.index('source_run = str(payload.get("from_run")'):]
    assert '_run_actions.require(' in start[:4000]
    assert 'source_run, "build_on_research")' in start[:4000]


def test_the_operations_keep_no_private_copy_of_the_rule():
    """The parallel state sets and the hand-copied gate test are gone from the operations: a
    copy is how the button and the action drifted in the first place."""
    retry = inspect.getsource(workflows.retry_failed_step)
    skip = inspect.getsource(workflows.skip_failed_step)
    assert "OPERATOR_RECOVERABLE_STEP_STATES" not in retry
    assert "SKIPPABLE_STEP_STATES" not in skip
    assert '"cancelled", "completed"' not in skip
    assert "_is_gate_step(" not in skip
    gate = inspect.getsource(workflows._is_gate_step)
    assert "is_gate(" in gate and "CAPABILITY_KEYS" not in gate
    assert "def _is_gate(" not in inspect.getsource(actions_mod)


def test_an_approval_is_an_approval_by_the_same_test_everywhere():
    """`_is_gate_step` (the spec template) and the action list (a loaded step) agree on every
    capability in the catalogue and on a decision step."""
    from boss_engine.services.workflow_capabilities import CAPABILITY_KEYS

    for key in [*CAPABILITY_KEYS, "", "not_a_capability"]:
        template = {"kind": "capability", "binding": {"capability": key}}
        loaded = step("s", "failed", key)
        offered = {a["key"] for a in run_actions({"status": "failed", "input": {}}, [loaded], {})}
        assert workflows._is_gate_step(template) == ("skip_step" not in offered), key
    assert workflows._is_gate_step({"kind": "decision"})
