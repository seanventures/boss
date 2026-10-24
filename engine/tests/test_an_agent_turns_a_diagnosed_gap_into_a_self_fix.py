"""AN AGENT TURNS A DIAGNOSED PLATFORM GAP INTO A SELF-FIX (BOSSAPP-571, the doorway).

Metatron (installed b07e24c3e, 2026-10-24) diagnosed two gaps in BOSS itself and could not act
on either: "there is no agent-callable path from a chat diagnosis to a boss_recommendations row,
only to start_self_fix which needs one already there". This is that path, end to end, on the
real pipeline with no paid model:

  1. a chat turn that only ASKED (writes not allowed) FILES the gap: `file_platform_gap` only
     queues for a person, so it runs in the turn — one recommendation, with the evidence, the
     acceptance tests and the turn's provenance;
  2. the same turn's `request_self_fix` comes back as a BUTTON (operator_only): nothing starts;
  3. a person PRESSES it (the press path: operator_confirmed) and the self-fix is claimed with
     its own budget reservation and its build queued as a worker job;
  4. the worker leases the job and Mender's build runs on BOSS's own source (the scripted
     conformance engine): its first stage, the investigation (the diagnosis), is recorded with
     its own time and budget, shown the filing agent's acceptance tests, and an agent reads the
     result with get_self_fix.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from boss_engine.agent import EngineAgent
from boss_engine.services import boss_recommendations
from boss_engine.services import scripted_engine
from boss_engine.services import self_fix as sf
from boss_engine.services import self_fix_build
from boss_engine.services.jobs import JobLeaseService
from boss_engine.tools import build_discuss_read_registry

from ._self_fix_support import Worker, harness, self_fix_on  # noqa: F401 - fixtures by name
from .test_workflow_capability_runner import eng, project  # noqa: F401

GAP = {
    "title": "Workflows an agent could start are not discoverable from chat",
    "symptom": "Asked to run the release workflow, the agent said no such workflow exists.",
    "evidence": ["engine/boss_engine/services/widget.py:4 double() returns x + 2",
                 "thread athr_example: 'I cannot find a workflow called release'"],
    "suspected_cause": "the widget label renders double(), which adds two",
    "proposed_design": "make double() multiply by two; the label follows",
    "acceptance_tests": ["widget.double(3) == 6", "the widget label for 3 says 'widget 6'"],
    "files": ["engine/boss_engine/services/widget.py"],
    "area": "workflow",
}


class ScriptedChat:
    """The chat model, scripted: file the gap, then offer the self-fix with the id it got."""

    def __init__(self) -> None:
        self.step = 0

    def create_chat_completion(self, messages, tools=None, tool_choice="auto"):
        self.step += 1
        if self.step == 1:
            return _call("file_platform_gap", GAP), {"total_tokens": 0}
        if self.step == 2:
            filed = next(json.loads(m["content"]) for m in reversed(messages)
                         if m.get("role") == "tool" and "recommendation_id" in str(m["content"]))
            return _call("request_self_fix", {"recommendation_id": filed["recommendation_id"]},
                         cid="call_2"), {"total_tokens": 0}
        return {"content": "Filed it for BOSS to fix itself; press the button to start the "
                           "self-fix.", "tool_calls": None,
                "raw": {"role": "assistant", "content": "Filed."}}, {"total_tokens": 0}


def _call(name: str, args: dict, cid: str = "call_1") -> dict:
    body = json.dumps(args)
    return {"content": None, "tool_calls": [{"id": cid, "name": name, "arguments": body}],
            "raw": {"role": "assistant", "content": None, "tool_calls": [
                {"id": cid, "type": "function", "function": {"name": name, "arguments": body}}]}}


def test_an_agent_files_a_gap_a_person_presses_and_the_self_fix_investigates(harness):
    h = harness
    h.script("good_fix")
    ctx = Worker(h.eng, h.settings)
    registry = build_discuss_read_registry(edition="community")
    agent = EngineAgent(ctx, registry=registry)
    agent._adapter = ScriptedChat()
    agent.ambient = {"_thread_id": "athr_gap_test"}

    # 1-2. A QUESTION's turn: nothing is allowed to write, yet the gap is filed in it, and the
    # self-fix comes back as a button.
    turn = agent.chat(project_id=h.project, actor="ceo", allow_writes=False,
                      message="why can't you find the release workflow?")
    filed = next(t["result"] for t in turn.trace if t["tool"] == "file_platform_gap")
    assert filed["ok"] and filed["created"], filed
    assert filed["signature"].startswith("platform_gap:workflow:")
    offered = [p for p in turn.pending_writes if p["tool"] == "request_self_fix"]
    assert offered and offered[0]["args"] == {"recommendation_id": filed["recommendation_id"]}
    with h.eng.begin() as c:
        assert c.execute(text("SELECT count(*) FROM self_fixes WHERE project_id = :p"),
                         {"p": h.project}).scalar() == 0, "nothing starts without the press"
        rec = next(r for r in boss_recommendations.list_recommendations(c, h.project)
                   if r["id"] == filed["recommendation_id"])
    assert rec["status"] == "open" and rec["acceptance_tests"] == GAP["acceptance_tests"]
    assert rec["evidence"] == GAP["evidence"] and rec["root_cause"] == GAP["suspected_cause"]
    assert rec["refs"][0]["thread_id"] == "athr_gap_test"
    assert rec["refs"][0]["via"] == "file_platform_gap"

    # Filed again from the same conversation: the same cause, counted once, not a second row.
    again = registry.invoke("file_platform_gap", ctx, project_id=h.project, actor="ceo",
                            args={**GAP, "signature": filed["signature"],
                                  "_thread_id": "athr_gap_test"})
    assert again["recommendation_id"] == filed["recommendation_id"] and not again["created"]

    # 3. THE PRESS (agent_action's path): the person's click, verbatim.
    pressed = registry.invoke("request_self_fix", ctx, project_id=h.project, actor="ceo",
                              args=offered[0]["args"], operator_confirmed=True)
    assert pressed["ok"] and pressed["outcome"] == "started", pressed
    sfx_id = pressed["self_fix_id"]
    with h.eng.begin() as c:
        row = sf.get(c, sfx_id)
        job = c.execute(text("SELECT id, kind, status FROM jobs WHERE project_id = :p AND "
                             "kind = 'self_fix.build' AND payload->>'self_fix_id' = :s"),
                        {"p": h.project, "s": sfx_id}).mappings().one()
        rec = next(r for r in boss_recommendations.list_recommendations(c, h.project)
                   if r["id"] == filed["recommendation_id"])
    assert row["status"] == sf.CLAIMED and row["step_kind"] == sf.PLATFORM_GAP
    assert row["budget_reservation_id"], "the self-fix reserves its own budget"
    assert row["evidence"]["hypothesis"]["acceptance_tests"] == GAP["acceptance_tests"]
    assert rec["status"] == "fixing" and rec["self_fix_id"] == sfx_id
    assert job["status"] == "queued"

    # 4. THE WORKER leases the build and Mender runs on BOSS's own source, under that lease.
    with h.eng.begin() as c:
        lease = JobLeaseService(h.settings).lease_owned_job(
            c, project_id=h.project, job_id=job["id"], worker_id="worker-gap-test",
            lease_ttl_seconds=900)
    scripted_engine.reset()
    out = self_fix_build.execute_job(h.eng, h.settings, self_fix_id=sfx_id, job_id=job["id"],
                                     worker_id="worker-gap-test",
                                     lease_token=lease["lease_token"])
    with h.eng.begin() as c:
        row = sf.get(c, sfx_id)
    assert row["status"] == sf.AWAITING_APPROVAL, (out, row.get("refusal"),
                                                   row.get("refusal_detail"))
    # The investigation stage's output: Mender's diagnosis against the code.
    assert row["diagnosis"]["hypothesis"] and row["location"].endswith("widget.py::double")
    calls = scripted_engine.run_state(sfx_id).calls
    diagnose = next(c for c in calls if c["stage"] == "self_fix_diagnose")
    assert diagnose["read_only"], "the investigation changes nothing"
    assert "widget.double(3) == 6" in diagnose["full_prompt"], "shown the acceptance tests"

    # An agent reads the result: the diagnosis, the diff, the tests, the card.
    read = registry.invoke("get_self_fix", ctx, project_id=h.project, actor="ceo",
                           args={"self_fix_id": sfx_id})
    assert read["self_fix"]["diagnosis"]["hypothesis"] == row["diagnosis"]["hypothesis"]
    assert read.get("card"), "awaiting a person's approval, with the card"
    gaps = registry.invoke("list_platform_gaps", ctx, project_id=h.project, actor="ceo",
                           args={})
    assert any(g["id"] == filed["recommendation_id"] and g["self_fix_id"] == sfx_id
               for g in gaps["gaps"])


def test_a_gap_with_no_evidence_or_no_acceptance_tests_is_not_filed(harness):
    h = harness
    registry = build_discuss_read_registry(edition="community")
    out = registry.invoke("file_platform_gap", Worker(h.eng, h.settings), project_id=h.project,
                          actor="ceo", args={**GAP, "evidence": [], "acceptance_tests": []})
    assert out["ok"] is False and "evidence" in out["error"] and "acceptance_tests" in out["error"]
    with h.eng.begin() as c:
        assert not boss_recommendations.list_recommendations(c, h.project)


def test_the_signature_is_bosss_and_a_given_one_is_kept():
    from boss_engine.tools.builtin import platform_gap_signature

    assert platform_gap_signature("", "Workflow", "Workflows are not discoverable (run 42)") == \
        "platform_gap:workflow:workflows_are_not_discoverable_run"
    assert platform_gap_signature("platform_gap:workflow:x_y", "", "t") == \
        "platform_gap:workflow:x_y"
    assert platform_gap_signature("knowledge:propose rule!", "", "t") == \
        "platform_gap:knowledge:propose_rule"
    assert sf.kind_of_signature("platform_gap:workflow:x") == sf.PLATFORM_GAP
    assert sf.kind_of_signature("deploy:port_held:8000") == "deploy"
    assert sf.kind_of_signature("build:boss:root") == "build"
