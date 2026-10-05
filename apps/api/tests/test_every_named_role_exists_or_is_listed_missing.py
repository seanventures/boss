"""A role a workflow step names that the project does not have is an empty seat on its team
(org-view spec §3 graft, §6.2): listed with the steps that name it, until the agent exists.
`Auto` steps name nobody, so they are never missing."""
from __future__ import annotations

from apps.api.tests import org_board_support as ob


def test_a_named_role_nobody_fills_is_missing_until_it_exists(client, engine):
    missing_key = ob.uid("data_engineer")
    key = ob.workflow(engine, [ob.step("load", captain=missing_key), ob.step("auto_one")])
    body = ob.board(client).json()
    entry = [m for m in body["missing_roles"] if m["role_key"] == missing_key]
    assert entry, body["missing_roles"]
    assert entry[0]["steps"] == [{"workflow_key": key, "workflow_name": key, "step_id": "load",
                                  "step_title": "Load"}]
    assert entry[0]["team"] == "yours"  # no pack YAML knows this key
    assert ob.team_row(body, "yours")["gaps"]["missing_roles"] >= 1
    # Auto names nobody: never an empty seat.
    assert all(s["step_id"] != "auto_one" for m in body["missing_roles"] for s in m["steps"])

    ob.role(engine, missing_key, "yours")
    after = ob.board(client).json()
    assert not [m for m in after["missing_roles"] if m["role_key"] == missing_key]
