"""An edge says how the work actually moved, in one vocabulary (Agent World §2.3, §9).

The web renders the API's `edge_label` and never builds its own, so this is the only place the
words are decided. Every `ran_as.how` the runner can write must land on a ledger `how`, the
ledger refuses an agent "asking" without the envelope that proves it, and acts are written in
the order the work happened even when every clock in one transaction says the same instant.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from boss_engine.services import agent_acts
from boss_engine.services.agent_world_types import EDGE_LABELS


def test_every_how_has_its_words():
    assert set(EDGE_LABELS) == agent_acts.HOWS
    assert agent_acts.edge_label("named") == "Named in workflow"
    assert agent_acts.edge_label("only_holder", skill="video.trim") == "Only one holds video.trim"
    assert agent_acts.edge_label("only_candidate") == "Only one could"
    assert agent_acts.edge_label("model_pick", 3) == "Picked by BOSS over 2"
    assert agent_acts.edge_label("captain", captain="engineering_captain") == (
        "Assigned by engineering_captain")
    assert agent_acts.edge_label("run_actor") == "Ran as you"
    assert agent_acts.edge_label("next_stage") == "Next stage"
    assert agent_acts.edge_label("seated") == "Seated on panel"
    assert agent_acts.edge_label("asked_by_person") == "Asked by you"
    assert agent_acts.edge_label("asked_by_agent", agent="qa") == "Asked by qa"
    assert agent_acts.edge_label("approved") == "Approved by you"
    assert agent_acts.edge_label("started") == "Started by you"
    assert agent_acts.edge_label("signal") == "Raised by a signal"
    assert agent_acts.edge_label("board") == "From the work board"
    with pytest.raises(ValueError):
        agent_acts.edge_label("decided_to_delegate")


def test_no_label_leaks_a_placeholder():
    for how in EDGE_LABELS:
        for n in (0, 1, 2, 5):
            label = agent_acts.edge_label(how, n)
            assert "{" not in label and "}" not in label, (how, label)


def test_every_way_the_runner_says_who_ran_a_step_lands_on_a_ledger_how():
    """The hows step_agents and agent_steps write, read from their source, so a new one that
    nobody mapped fails here rather than signing work as "Named in workflow" by default."""
    root = Path(agent_acts.__file__).parent
    written: set[str] = set()
    for name in ("step_agents.py", "agent_steps.py", "workflow_capability_runner.py"):
        source = (root / name).read_text()
        written |= set(re.findall(r'_record\(\s*[^,]+,\s*"([a-z_]+)"', source))
        written |= set(re.findall(r'"how":\s*"([a-z_]+)"', source))
        written |= set(re.findall(r'single_how="([a-z_]+)"', source))
    assert {"named", "only_holder", "only_candidate", "run_actor", "model"} <= written
    for how in written:
        assert agent_acts._HOW_FROM_RAN_AS.get(how) in agent_acts.HOWS, how


def test_an_agent_is_never_said_to_have_asked_without_the_envelope():
    with pytest.raises(ValueError, match="A2A envelope"):
        agent_acts.open_act(
            None, project_id="p", kind="handoff", role_key="qa", how="asked_by_agent",
            handed_by_kind="agent", handed_by_id="pm", accountable="ceo", label="x")


def test_an_act_needs_the_accountable_human_and_known_words():
    common = dict(project_id="p", role_key="qa", handed_by_kind="boss", label="x")
    with pytest.raises(ValueError):
        agent_acts.open_act(None, kind="step", how="named", accountable="", **common)
    with pytest.raises(ValueError):
        agent_acts.open_act(None, kind="dance", how="named", accountable="ceo", **common)
    with pytest.raises(ValueError):
        agent_acts.open_act(None, kind="step", how="chose_to_ask", accountable="ceo", **common)


def test_acts_are_written_in_causal_order_not_clock_order():
    spec = {"entry_step": "a", "steps": [
        {"id": "a", "kind": "capability", "next": ["gate"]},
        {"id": "gate", "kind": "decision", "next": ["fork"]},
        {"id": "fork", "kind": "parallel", "branches": ["b", "c"], "join": "j"},
        {"id": "b", "kind": "capability", "next": ["j"]},
        {"id": "c", "kind": "capability", "next": ["j"]},
        {"id": "j", "kind": "join", "next": ["a"]},  # a loop back: cut, not followed forever
    ]}
    depth = agent_acts._depths(spec)
    assert depth["a"] < depth["gate"] < depth["fork"] < depth["b"] < depth["j"]
    assert depth["b"] == depth["c"]


def test_a_parent_is_the_step_that_finished_last_walking_through_plumbing():
    from datetime import datetime, timedelta, timezone

    t0 = datetime(2026, 10, 3, tzinfo=timezone.utc)
    preds = {"j": ["b", "c"], "b": ["fork"], "c": ["fork"], "fork": ["a"], "after": ["j"]}
    acts = {
        "a": [{"act_id": "act_a", "status": "complete", "ended_at": t0}],
        "b": [{"act_id": "act_b", "status": "complete", "ended_at": t0 + timedelta(minutes=5)}],
        "c": [{"act_id": "act_c", "status": "complete", "ended_at": t0 + timedelta(minutes=9)}],
    }
    # `j` (a join) has no act of its own; the walk goes through it to the branch that finished
    # last, which is the one that released `after`.
    assert agent_acts._parent_for("after", preds, acts, "root") == "act_c"
    assert agent_acts._parent_for("a", preds, acts, "root") == "root"


def test_a_step_rows_status_maps_to_the_act_it_means():
    state = agent_acts._step_state
    assert state({"status": "pending"}, {}) is None
    assert state({"status": "ready"}, {}) is None
    assert state({"status": "running"}, {}) == "active"
    assert state({"status": "waiting", "decision_id": "dec_1"}, {}) == "awaiting_input"
    assert state({"status": "waiting"}, {"await": {"kind": "code_task", "id": "c"}}) == "active"
    assert state({"status": "waiting"}, {"await": {"kind": "agent_run", "id": "r"}}) == (
        "awaiting_input")
    assert state({"status": "succeeded"}, {}) == "complete"
    assert state({"status": "failed"}, {}) == "error"
    assert state({"status": "skipped"}, {}) == "skipped"


def test_the_assigned_engineer_builds_and_the_reviewers_stay_reviewers():
    named = {"kind": "step", "how": "named", "role_key": "backend_engineer"}
    assert agent_acts.stage_role("implement", named) == "backend_engineer"
    assert agent_acts.stage_role("repair", named) == "backend_engineer"
    assert agent_acts.stage_role("review_code", named) == "adversarial_reviewer"
    assert agent_acts.stage_role("write_tests", named) == "qa"
    picked = {"kind": "step", "how": "run_actor", "role_key": None}
    assert agent_acts.stage_role("implement", picked) == "software_engineer"
    assert agent_acts.stage_role("plan", None) == "chief_architect"


def test_act_ids_sort_the_way_the_work_happened():
    first = agent_acts.new_act_id()
    second = agent_acts.new_act_id()
    assert first.startswith("act_") and len(first) == 30
    assert first[:14] <= second[:14]

