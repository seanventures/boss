"""PAUSE, RESUME OR RESTORE A WHOLE WORK TREE (BOSSAPP-878).

* A dry run says what would pause, and changes nothing.
* Holding an epic snapshots every item and run under it, pauses what was running, and from then
  on nothing under it is claimed (claim_step), resumed by hand (resume_run refuses), woken by a
  finished blocker, or started by a deferred start — including an item added after the hold.
* Restoring puts it back EXACTLY: only the runs the hold paused resume; a run that was already
  paused stays paused; a start that waited only for the hold goes. A second restore is refused.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from boss_engine.services import work_holds, work_items, workflows

from ._ambient_support import eng, project  # noqa: F401 — fixtures used by name
from .test_one_work_item_graph_with_execution_policy import _publish, _start


def _status(c, run_id):
    return c.execute(text("SELECT status FROM workflow_runs WHERE id = :r"), {"r": run_id}).scalar()


def test_hold_the_tree_and_restore_it_exactly(eng, project):
    with eng.begin() as c:
        version = _publish(c, project)["id"]
        epic = work_items.create_item(c, project_id=project, title="Checkout epic")
        cart = work_items.create_item(c, project_id=project, title="Cart", parent_id=epic["id"])
        pay = work_items.create_item(c, project_id=project, title="Pay", parent_id=epic["id"])
        outside = work_items.create_item(c, project_id=project, title="Vendor contract")
        running = _start(c, project, version, work_item_id=cart["id"])
        paused_before = _start(c, project, version, work_item_id=pay["id"])
        workflows.pause_run(c, project, paused_before["id"], "person:ceo")
        work_items.add_relation(c, project_id=project, item_id=pay["id"], other_id=outside["id"],
                                kind="blocked_by", actor="person:ceo")
        assert _status(c, running["id"]) != "paused"

        preview = work_holds.pause_tree(c, project_id=project, root_kind="work_item",
                                        root_id=epic["id"], actor="person:ceo", dry_run=True)
        plan = {m["member_id"]: m["action"] for m in preview["members"]}
        assert plan[running["id"]] == "paused" and plan[paused_before["id"]] == "already_paused"
        assert plan[epic["id"]] == plan[cart["id"]] == "held"
        assert _status(c, running["id"]) != "paused"            # a dry run changes nothing
        assert not work_holds.list_holds(c, project)

        hold = work_holds.pause_tree(c, project_id=project, root_kind="work_item",
                                     root_id=epic["id"], actor="person:ceo", reason="runaway spend")
        assert _status(c, running["id"]) == "paused"
        assert work_items.get_item(c, project, cart["id"])["hold"]["id"] == hold["id"]
        assert work_items.tree(c, project)["items"][0]["on_hold"] is True
        with pytest.raises(work_holds.HoldConflict):
            work_holds.pause_tree(c, project_id=project, root_kind="work_item", root_id=epic["id"],
                                  actor="person:ceo")
        # Nothing resumes it by hand.
        with pytest.raises(workflows.WorkflowConflictError, match="on hold"):
            workflows.resume_run(c, project, running["id"], "person:ceo")
        # An item added under the epic after the hold is held too: its run is not claimed.
        late = work_items.create_item(c, project_id=project, title="Receipts", parent_id=epic["id"])
        late_run = _start(c, project, version, work_item_id=late["id"])
        assert work_holds.run_is_held(c, late_run["id"], mode="pause")
        assert workflows.claim_step(c, project_id=project, run_id=late_run["id"], worker_id="w1") is None
        # A finished blocker wakes nothing under the hold.
        woke = work_items.complete_item(c, project_id=project, item_id=outside["id"], actor="person:ceo")
        assert woke["woke"][0]["runs"] == [{"kind": "hold", "action": "held"}]
        assert _status(c, paused_before["id"]) == "paused"
        # A start asked for under the hold waits for the restore.
        deferred = work_items.defer_start(c, project_id=project, item_id=cart["id"],
                                          workflow_version_id=version, actor="person:ceo")
        assert deferred["status"] == "deferred" and deferred["held"] is True

        dry = work_holds.restore_tree(c, project_id=project, hold_id=hold["id"], actor="person:ceo",
                                      dry_run=True)
        back = {m["member_id"]: m["restored_action"] for m in dry["members"]}
        assert back[running["id"]] == "resumed" and back[paused_before["id"]] == "left_paused"
        assert _status(c, running["id"]) == "paused"

        restored = work_holds.restore_tree(c, project_id=project, hold_id=hold["id"],
                                           actor="person:ceo")
        assert restored["status"] == "restored"
        assert _status(c, running["id"]) != "paused"            # what the hold paused resumed
        assert _status(c, paused_before["id"]) == "paused"      # what was paused before stays
        started = c.execute(text(
            "SELECT count(*) FROM workflow_runs WHERE work_item_id = :w"), {"w": cart["id"]}).scalar()
        assert started == 2                                     # the deferred start went
        assert work_items.get_item(c, project, cart["id"])["hold"] is None
        with pytest.raises(work_holds.HoldConflict):
            work_holds.restore_tree(c, project_id=project, hold_id=hold["id"], actor="person:ceo")


def test_a_run_tree_can_be_held_in_drain_mode(eng, project):
    with eng.begin() as c:
        version = _publish(c, project)["id"]
        parent = _start(c, project, version)
        child = workflows.start_run(c, project_id=project, actor="test",
                                    input_data={"directive": "x"}, dedupe_key=f"dk_child_{parent['id']}",
                                    workflow_version_id=version, parent_run_id=parent["id"])
        hold = work_holds.pause_tree(c, project_id=project, root_kind="workflow_run",
                                     root_id=parent["id"], actor="person:ceo", mode="drain")
        actions = {m["member_id"]: m["action"] for m in hold["members"]}
        assert actions == {parent["id"]: "left_running", child["id"]: "left_running"}
        assert _status(c, child["id"]) != "paused"               # drain lets running work finish
        assert work_holds.run_is_held(c, child["id"]) and not work_holds.run_is_held(
            c, child["id"], mode="pause")
        work_holds.restore_tree(c, project_id=project, hold_id=hold["id"], actor="person:ceo")
        assert not work_holds.run_is_held(c, child["id"])
