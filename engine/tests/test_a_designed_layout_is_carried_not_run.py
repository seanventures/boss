"""Where the designer's operator put a step is carried through, and never changes a run (#551).

The console keeps a step dragged off the lane's grid as `layout.positions[step_id] = {dx, dy}` at
the top of the spec (apps/web/lib/workflow-layout.ts). It is drawing only: the validator Check and
Save use must keep it exactly as given — so a save and a reload bring it back — and must judge a
spec with a layout exactly as it judges the same spec without one.
"""
from __future__ import annotations

import copy

from boss_engine.services.workflows import normalise_workflow_spec, validate_workflow_spec

SPEC = {
    "entry_step": "draft",
    "steps": [
        {"id": "draft", "kind": "capability", "next": ["review"],
         "binding": {"agent_work": True, "inputs": {"instructions": {"from_input": "instructions"}}}},
        {"id": "review", "kind": "capability", "next": [],
         "binding": {"agent_work": True, "inputs": {"instructions": {"from_input": "instructions"}}}},
    ],
}
LAYOUT = {"positions": {"review": {"dx": 120, "dy": -64}}}


def test_a_layout_survives_normalising_untouched():
    spec = {**copy.deepcopy(SPEC), "layout": copy.deepcopy(LAYOUT)}
    normalised, problems = normalise_workflow_spec(spec, check_agents=False)
    assert problems == []
    assert normalised["layout"] == LAYOUT
    assert validate_workflow_spec(spec, check_agents=False)["layout"] == LAYOUT


def test_a_layout_changes_nothing_the_validator_says():
    plain, plain_problems = normalise_workflow_spec(copy.deepcopy(SPEC), check_agents=False)
    laid, laid_problems = normalise_workflow_spec({**copy.deepcopy(SPEC), "layout": LAYOUT},
                                                  check_agents=False)
    assert laid_problems == plain_problems
    assert {k: v for k, v in laid.items() if k != "layout"} == plain
