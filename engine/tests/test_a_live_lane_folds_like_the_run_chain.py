"""A Live work row draws a run's chain the way the run view does, only shorter (spec §4.3).

`agents_live_board.lane` is chain-model.ts's folding, ported so the server sends the lane
ready to draw: the person who started it first, then the work that stands on its own in causal
order, a step's inner work counted under it, a back-and-forth said once (`Forge ↔ Rook ×3`),
and a lane longer than 7 folded to its first 2 hops, `+n`, and its last 4.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from boss_engine.services.agents_live_board import lane

T0 = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)


def act(i, kind, role, parent, *, status="complete", step=None, label=None, minutes=None):
    return {"act_id": f"act_{i:03d}", "kind": kind, "role_key": role, "parent_act_id": parent,
            "status": status, "step_id": step, "label": label or f"{step or kind} · Ship it",
            "how": "named", "handed_by_kind": "person" if kind == "run" else "boss",
            "handed_by_id": "ceo" if kind == "run" else None, "accountable": "ceo",
            "scope": {}, "considered": [],
            "started_at": T0 + timedelta(minutes=i if minutes is None else minutes)}


def test_the_root_then_steps_with_their_inner_work_counted():
    acts = [act(0, "run", None, None, status="active"),
            act(1, "step", "pm", "act_000", step="prd", label="PRD · Ship it"),
            act(2, "step", "software_engineer", "act_001", status="active", step="build",
                label="Build · Ship it"),
            *[act(3 + n, "stage", "software_engineer", "act_002", step="implement")
              for n in range(5)]]
    hops = lane(acts)
    assert [h["label"] for h in hops] == ["Started", "PRD", "Build"]
    assert hops[0]["role_key"] is None and hops[0]["person"] == "ceo"
    assert hops[0]["state"] == "idle"
    assert hops[2]["state"] == "working" and hops[2]["inside"] == 5


def test_a_gate_follows_the_work_it_holds_whatever_its_clock_says():
    acts = [act(0, "run", None, None, status="active"),
            act(5, "step", "media_publisher", "act_000", step="draft"),
            # Never claimed: its start is when its row was made, before the draft ran.
            act(1, "step", None, "act_005", status="awaiting_input", step="publish")]
    hops = lane(acts)
    assert [h["role_key"] for h in hops] == [None, "media_publisher", None]
    assert hops[-1]["state"] == "needs_you" and hops[-1]["person"] == "ceo"


def test_a_back_and_forth_is_said_once():
    acts = [act(0, "run", None, None, status="active")]
    parent = "act_000"
    for n in range(6):
        role, step = (("software_engineer", "implement") if n % 2 == 0
                      else ("adversarial_reviewer", "review"))
        acts.append(act(n + 1, "step", role, parent, step=step))
        parent = f"act_{n + 1:03d}"
    hops = lane(acts)
    assert hops[1] == {"loop": {"a": "software_engineer", "b": "adversarial_reviewer",
                                "rounds": 3}}


def test_a_long_lane_folds_to_its_first_two_and_last_four():
    acts = [act(0, "run", None, None, status="active")]
    parent = "act_000"
    for n in range(9):
        acts.append(act(n + 1, "step", f"agent_{n}", parent, step=f"s{n}"))
        parent = f"act_{n + 1:03d}"
    hops = lane(acts)
    assert len(hops) == 7
    assert hops[2] == {"fold": {"hidden": 4}}
    assert [h.get("role_key") for h in hops[3:]] == ["agent_5", "agent_6", "agent_7", "agent_8"]
