"""Meetings deliberate, workflows execute, and both report back to the squad's board
(BOSSAPP-908; design §13.2).

* Decided work goes to a workflow with a HANDOFF PACKET: the goal, what was decided and by whom,
  what was found, what was tried and failed, the dissent, how to verify, and who to report to.
  The run is linked to the item and is an artifact in the squad's memory.
* When the run ends its result comes back once: a finding citing the run (and its PR) and a
  handoff back to the lead. A failed run goes into tried-and-failed memory.
* A squad convenes a panel the same way; the panel's verdict comes back as a finding.
* A launch that fails is said on the board and remembered, never silently dropped.
* The production launchers are the tools agents already call (request_meeting, start_workflow).
* Squad memory is what a late member reads first; at close it becomes one KG learning.
"""
from __future__ import annotations

from boss_engine.services.ambient import board, collab, squad_memory, squad_plugins, squad_turns

from ._ambient_support import eng, project  # noqa: F401
from ._squad_support import (CRITIC, ENGINEER, LEAD, QA, Launchers, ScriptedSquadModel, move,
                             settings, squad_item)


def _decide_a_draft_pr(c, item_id, launchers):
    model = ScriptedSquadModel({
        LEAD: [move("proposal", action="open_draft_pr", summary="Bump lodash and escape /emails",
                    verify="CI green incl. the /emails test", workflow="squad_demo_build")],
        ENGINEER: [move("critique", position="agree", summary="Small and safe")],
        CRITIC: [move("critique", position="object", summary="Split the PR")]})
    for who in (LEAD, ENGINEER, CRITIC):
        squad_turns.take_turn(c, settings(), item_id, who, model=model, launchers=launchers)
    return squad_turns.settle(c, settings(), item_id, end_of_round=True, launchers=launchers)


def test_decided_work_goes_to_a_workflow_with_the_packet_the_squad_built(eng, project):
    launchers = Launchers()
    with eng.begin() as c:
        item_id = squad_item(c, project)["item"]["id"]
        collab.post(c, item_id, kind="finding", sender=QA,
                    body={"summary": "No test covers /emails"})
        squad_memory.note(c, item_id, actor=LEAD, tried_and_failed="bump only: critic objected")
        settled = _decide_a_draft_pr(c, item_id, launchers)
        links = {(l["kind"], l["ref"]) for l in board.links(c, item_id)}
        memory = squad_memory.read(c, item_id)
        handoff = [m for m in collab.messages(c, item_id, kinds=("handoff",))
                   if m["body"].get("via")][0]
    run_id = settled["executed"]["ref"]
    start = launchers.calls[0]["start"]
    packet = start["inputs"]["handoff"]
    assert start["workflow_key"] == "squad_demo_build" and start["actor"] == "security_reviewer"
    assert start["inputs"]["ambient_work_item_id"] == item_id
    assert packet["decided"]["act_id"] == settled["act_id"] and packet["report_back_to"] == LEAD
    assert "No test covers /emails" in packet["found"]
    assert packet["tried_and_failed"] == ["bump only: critic objected"]
    assert packet["dissent"][0]["party"] == CRITIC and packet["verify"].startswith("CI green")
    assert "Already tried and failed" in start["directive"] and "Split the PR" in start["directive"]
    assert ("workflow_run", run_id) in links and run_id in memory["artifacts"]
    assert handoff["sender"] == LEAD and handoff["body"]["via"] == {"kind": "workflow_run",
                                                                    "ref": run_id}


def test_a_finished_run_comes_back_once_with_its_pull_request(eng, project):
    launchers = Launchers(pr="acme/web#41")
    with eng.begin() as c:
        item_id = squad_item(c, project)["item"]["id"]
        run_id = _decide_a_draft_pr(c, item_id, launchers)["executed"]["ref"]
        back = squad_plugins.collect_results(c, item_id)
        again = squad_plugins.collect_results(c, item_id)
        finding = [m for m in collab.messages(c, item_id, kinds=("finding",))
                   if m["sender"] == "boss"][0]
        returned = [m for m in collab.messages(c, item_id, kinds=("handoff",))
                    if m["body"].get("returned_from")]
        links = {(l["kind"], l["ref"]) for l in board.links(c, item_id)}
    assert [b["ref"] for b in back] == [run_id] and again == []
    assert {e["kind"] for e in finding["body"]["evidence"]} == {"workflow_run", "pull_request"}
    assert all(e["checked"] in ("exists", "shape") for e in finding["body"]["evidence"])
    assert returned[0]["body"]["to"] == LEAD and returned[0]["body"]["returned_from"]["ref"] == run_id
    assert ("pull_request", "acme/web#41") in links


def test_a_failed_run_is_remembered_as_tried_and_failed(eng, project):
    launchers = Launchers(run_status="failed")
    with eng.begin() as c:
        item_id = squad_item(c, project)["item"]["id"]
        _decide_a_draft_pr(c, item_id, launchers)
        back = squad_plugins.collect_results(c, item_id)
        memory = squad_memory.read(c, item_id)
    assert back[0]["failed"] and back[0]["status"] == "failed"
    assert any("tests failed: 2 of 40" in r for r in memory["tried_and_failed"])


def test_a_squad_convenes_a_panel_and_its_verdict_comes_back(eng, project):
    launchers = Launchers()
    with eng.begin() as c:
        item_id = squad_item(c, project)["item"]["id"]
        model = ScriptedSquadModel({LEAD: [move(
            "proposal", action="convene_meeting", summary="Bump only, or bump and escape?")]})
        turn = squad_turns.take_turn(c, settings(), item_id, LEAD, model=model,
                                     launchers=launchers)
        back = squad_plugins.collect_results(c, item_id)
        verdict = [m for m in collab.messages(c, item_id, kinds=("finding",))
                   if m["sender"] == "boss"][0]
    convene = launchers.calls[0]["convene"]
    assert set(convene["seats"]) == {"security_reviewer", "software_engineer", "qa",
                                     "adversarial_reviewer"}
    assert "Bump only, or bump and escape?" in convene["directive"]
    assert turn["settled"]["executed"]["kind"] == "meeting" and back[0]["kind"] == "meeting"
    assert "Bump and escape in one PR" in verdict["body"]["summary"]
    assert verdict["body"]["evidence"][0]["kind"] == "meeting"


def test_a_launch_that_fails_is_said_on_the_board_and_remembered(eng, project):
    launchers = Launchers(refuse="no workflow 'squad_demo_build' in this project")
    with eng.begin() as c:
        item_id = squad_item(c, project)["item"]["id"]
        settled = _decide_a_draft_pr(c, item_id, launchers)
        said = [m for m in collab.messages(c, item_id, kinds=("finding",))
                if m["sender"] == "boss"]
        memory = squad_memory.read(c, item_id)
    assert settled["state"] == "decided" and "error" in settled["executed"]
    assert "Could not start squad_demo_build" in said[0]["body"]["summary"]
    assert any("no workflow" in r for r in memory["tried_and_failed"])


def test_production_launchers_are_the_tools_agents_call(eng, project, monkeypatch):
    called = []

    class Registry:
        def invoke(self, tool, ctx, *, project_id, actor, args, operator_confirmed=False):
            called.append((tool, actor, sorted(args), operator_confirmed))
            return ({"status": "running", "run_id": "wfr_x"} if tool == "start_workflow"
                    else {"status": "running", "meeting": {"run_id": "mrun_x"}})

    import boss_engine.tools.builtin as builtin

    monkeypatch.setattr(builtin, "build_default_registry", lambda: Registry())
    launchers = squad_plugins.ToolLaunchers(settings())
    with eng.begin() as c:
        started = launchers.start(c, project_id=project, actor="security_reviewer",
                                  workflow_key="fast_fix", directive="d", title="t",
                                  inputs={"ambient_work_item_id": "w"})
        convened = launchers.convene(c, project_id=project, actor="security_reviewer",
                                     directive="d", seats=["qa"], objective="o")
    assert started == {"run_id": "wfr_x"} and convened == {"meeting_run_id": "mrun_x"}
    assert called == [("start_workflow", "security_reviewer",
                       ["directive", "inputs", "title", "workflow_key"], True),
                      ("request_meeting", "security_reviewer",
                       ["directive", "objective", "seats"], True)]


def test_squad_memory_is_read_first_and_becomes_one_learning_at_close(eng, project):
    with eng.begin() as c:
        got = squad_item(c, project)
        item_id = got["item"]["id"]
        collab.post(c, item_id, kind="finding", sender=ENGINEER,
                    body={"summary": "Three call sites use _.template"})
        squad_memory.note(c, item_id, actor=LEAD, tried_and_failed="bump only: critic objected",
                          phase="Round 2", next_step="Escape /emails")
        packed = squad_memory.pack(c, item_id)
        model = ScriptedSquadModel({QA: [move("pass")]})
        squad_turns.take_turn(c, settings(), item_id, QA, model=model)
        board.start(c, item_id, owner=LEAD, token=got["lease_token"])
        board.finish(c, item_id, actor=LEAD, lifecycle="resolved", token=got["lease_token"],
                     outcome="patched")
        closed = squad_memory.close(c, item_id, actor=LEAD)
        after = squad_memory.read(c, item_id)
    assert "TRIED AND FAILED" in packed and "bump only: critic objected" in packed
    assert "Three call sites use _.template" in packed and "Escape /emails" in packed
    assert "bump only: critic objected" in model.prompts[0][1]
    assert closed["skipped"] is None and closed["learning"]
    assert after["open"] is False
