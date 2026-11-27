"""Second ask on an open decision routes to `operate`/`decide`, not `new_code_change` (BOSSAPP-1237)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import agent_decision, intent_router as ir, turn_cards
from boss_engine.services.capability_manifest import CapabilityManifest


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as c:
        for ddl in (
            "CREATE TABLE workflow_runs (id TEXT, project_id TEXT, status TEXT, input TEXT,"
            " error TEXT, blocked_reason TEXT, created_at TEXT)",
            "CREATE TABLE workflow_step_runs (id TEXT, project_id TEXT, workflow_run_id TEXT,"
            " step_id TEXT, step_kind TEXT, status TEXT, input TEXT, output TEXT,"
            " last_error TEXT, attempt INTEGER, iteration INTEGER, decision_id TEXT,"
            " created_at TEXT)",
            "CREATE TABLE code_task_runs (id TEXT, project_id TEXT, task_id TEXT, title TEXT,"
            " status TEXT, phase TEXT, error TEXT, pull_request_url TEXT, state TEXT,"
            " created_at TEXT)",
            "CREATE TABLE decisions (id TEXT, project_id TEXT, question TEXT, status TEXT,"
            " recommendation TEXT, options TEXT, risk_tier INTEGER, default_action TEXT,"
            " external_ref TEXT)",
            "CREATE TABLE deployment_targets (id TEXT, project_id TEXT, app_name TEXT,"
            " environment TEXT, status TEXT, target_kind TEXT, target_ref TEXT, domains TEXT)",
        ):
            c.execute(text(ddl))
        c.execute(text(
            "INSERT INTO code_task_runs VALUES ('crun_timer','boss-dogfood',NULL,"
            "'Add SLA timer','completed','done',NULL,"
            "'http://boss-local/local_p/desk/pull/1','{}','2026-11-24')"
        ))
        c.execute(text(
            "INSERT INTO decisions VALUES ('dec_merge_1','boss-dogfood',"
            "'Approve code change: Add SLA timer (change #1)','pending',:rec,'[]',2,"
            "'approve','crun_timer')"
        ), {"rec": json.dumps({"action": "review_pull_request", "run_id": "crun_timer"})})
        c.execute(text(
            "INSERT INTO decisions VALUES ('dec_other','boss-dogfood',"
            "'Unrelated pending decision','pending',:rec,'[]',1,'approve',NULL)"
        ), {"rec": json.dumps({"action": "publish_document"})})
    with engine.begin() as conn:
        yield conn


def test_operate_candidate_and_decision_context_include_open_decisions(db, monkeypatch):
    router = ir.IntentRouter.__new__(ir.IntentRouter)
    router.settings = SimpleNamespace()
    monkeypatch.setattr(router, "_a_model_can_answer", lambda *_a, **_k: True)

    captured: dict = {}

    def fake_decide(connection, settings, project_id, *, situation, message, history,
                    context, candidates, hints, agent, ref, adapter=None):
        captured["context"] = context
        captured["candidates"] = candidates
        captured["hints"] = hints
        return agent_decision.AgentDecision(
            agent_decision.DECIDED,
            action="operate",
            confidence=0.95,
            reason="Follow-up refers to open decision dec_merge_1.",
            by={"backend": "stub"},
        )

    monkeypatch.setattr(agent_decision, "decide", fake_decide)

    observed_answer: dict = {}

    def fake_answer(self, connection, project_id, messages, *, actor, external_ref,
                    mode, thread_id, role, depth, writes):
        observed_answer["writes"] = writes
        offer = {
            "id": "pa_decide_1",
            "tool": "decide",
            "label": "Revise decision",
            "args": {"decision_id": "dec_merge_1", "action": "revise", "note": "Handle weekends."},
        }
        return ir.RouterTurn(
            reply="Staged the revision for decision dec_merge_1.",
            proposed_actions=[offer],
        )

    monkeypatch.setattr(ir.IntentRouter, "_answer", fake_answer)

    turn = router.route_turn(
        db,
        project_id="boss-dogfood",
        actor="ceo",
        messages=[
            {"role": "user", "content": "Send the SLA timer change back to handle weekends."},
            {"role": "assistant", "content": "I staged Revise decision above."},
            {"role": "user", "content": "Go ahead and revise that decision with weekend handling."},
        ],
        manifest=CapabilityManifest(capabilities=()),
        external_ref="crun_timer",
    )

    operate_cand = next(c for c in captured["candidates"] if c.key == "operate")
    assert "pending decision" in operate_cand.means.lower()
    assert "decide" in operate_cand.means.lower()
    open_decs = captured["context"].get("open decisions") or []
    assert open_decs, "expected open decisions in _decision_context"
    assert open_decs[0]["id"] == "dec_merge_1"
    assert any("open decision" in h.lower() for h in captured["hints"])
    assert observed_answer["writes"] is True
    assert turn.decision["action"] == "operate"
    # Second ask staged `decide`, NOT `new_code_change` ("New work" card must stay None).
    assert turn_cards.proposal(
        db, "boss-dogfood",
        offers=turn.proposed_actions,
        decision=turn.decision,
        message="Go ahead and revise that decision with weekend handling.",
    ) is None
