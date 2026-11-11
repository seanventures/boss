"""BOSSAPP-960: the broken feedback loops, each pinned where it was wrong.

Four bugs and two rewires, every one measured by the behaviour that was broken:
a model's claim scored as its own validation (self_fix), depends_on was parsed and
then ignored (prd_decomposition), the agent loop's model call died on one provider
hiccup, the budget ceiling the CLI honours was never passed, the risk tier was a
substring check, and repair retries shipped off by default.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from boss_engine.services import self_fix as sf
from boss_engine.services.prd_decomposition import decompose_prd_into_tasks, order_by_dependencies


# ── self_fix.py: the §5.2 "model evidence validated" signal is a fact about evidence ──────────

def _confidence(monkeypatch, entry):
    from boss_engine.services import step_recovery

    monkeypatch.setattr(step_recovery, "coded_decision",
                        lambda *a, **k: {"action": "retry"})
    monkeypatch.setattr(sf, "_code_exists", lambda settings, files: [])
    monkeypatch.setattr(sf, "_occurrences", lambda connection, signature: 1)
    return sf.confidence_for(object(), object(), kind="deploy", entry=entry,
                             evidence={"failure": {}}, state={})


def test_a_model_claim_without_citations_is_not_validated_evidence(monkeypatch):
    """The +2 'model_evidence_validated' used to fire whenever the model chose a BOSS-side
    action at all (`model_validated=model_on_boss`): the same event counted twice."""
    entry = {"action": "fix_boss", "by": {"engine": "claude_code"}, "pattern": "x",
             "signature": "sig"}
    scored = _confidence(monkeypatch, entry)
    assert "model_evidence_validated" not in {r["signal"] for r in scored["reasons"]}


def test_a_model_claim_with_grounded_citations_is_validated(monkeypatch):
    entry = {"action": "fix_boss", "by": {"engine": "claude_code"}, "pattern": "x",
             "signature": "sig",
             "evidence": ["deploy failed: Bind for 0.0.0.0:8000 failed"]}
    scored = _confidence(monkeypatch, entry)
    assert "model_evidence_validated" in {r["signal"] for r in scored["reasons"]}


# ── prd_decomposition: depends_on decides the order instead of being dropped ─────────────────

def test_depends_on_reorders_a_plan_whose_numbering_contradicts_it():
    tasks = [
        {"title": "wire-up", "order": 1, "depends_on": [2, 3]},
        {"title": "engine", "order": 2, "depends_on": []},
        {"title": "components", "order": 3, "depends_on": [2]},
    ]
    ordered = order_by_dependencies(tasks)
    assert [t["title"] for t in ordered] == ["engine", "components", "wire-up"]
    # The field survives into the plan advance_decomposition carries.
    assert all("depends_on" in t for t in ordered)


def test_a_cycle_or_an_unknown_dependency_degrades_to_the_order_sort():
    cycle = [
        {"title": "a", "order": 1, "depends_on": [2]},
        {"title": "b", "order": 2, "depends_on": [1]},
    ]
    assert [t["title"] for t in order_by_dependencies(cycle)] == ["a", "b"]
    unknown = [
        {"title": "a", "order": 1, "depends_on": [99]},
        {"title": "b", "order": 2, "depends_on": []},
    ]
    assert [t["title"] for t in order_by_dependencies(unknown)] == ["a", "b"]


def test_decompose_returns_a_dependency_honouring_order():
    class Adapter:
        def create_chat_completion(self, messages, **kwargs):
            plan = [
                {"title": "wire-up", "instructions": "wire", "order": 1,
                 "acceptance_criteria": [], "depends_on": [2]},
                {"title": "engine", "instructions": "build", "order": 2,
                 "acceptance_criteria": [], "depends_on": []},
            ]
            return {"content": json.dumps(plan)}, {}

    tasks = decompose_prd_into_tasks(object(), "a PRD", adapter=Adapter())
    assert [t["title"] for t in tasks] == ["engine", "wire-up"]
    assert tasks[1]["depends_on"] == [2]


# ── agent_loop: the model call is retried where it happens ───────────────────────────────────

def test_the_agent_loops_model_calls_ride_the_transient_retry():
    import boss_engine.services.agent_loop as agent_loop

    source = inspect.getsource(agent_loop)
    assert "_with_transient_retry(" in source
    # No bare completion call remains: every one is wrapped.
    assert "usage = adapter.create_chat_completion(" not in source


# ── engine_contract: the standing budget ceiling reaches every session ───────────────────────

class _Agent:
    engine = "fake"

    def __init__(self, budget_limit=True):
        from boss_engine.services.engine_contract import EngineCapabilities

        self.capabilities = EngineCapabilities(read_only=True, budget_limit=budget_limit,
                                               reports_cost=True)
        self.seen: dict = {}

    def run_role(self, prompt, cwd, *, read_only=False, model=None, resume_session_id=None,
                 max_turns=None, parallel_agents=0, max_budget_usd=None):
        self.seen = {"max_budget_usd": max_budget_usd}
        return {"summary": "done", "usage": {}, "cost_usd_micros": 0, "turns": 1}

    def run(self, spec, repo, sandbox):  # pragma: no cover — contract surface only
        raise NotImplementedError


def test_the_agents_standing_ceiling_bounds_every_session(tmp_path):
    from boss_engine.services.engine_contract import ContractedAgent

    agent = _Agent()
    held = ContractedAgent(agent)
    held.budget_ceiling = lambda: 1.75
    held.run_role("p", tmp_path)
    assert agent.seen["max_budget_usd"] == 1.75


def test_an_explicit_ceiling_wins_and_zero_budget_floors_at_a_cent(tmp_path):
    from boss_engine.services.engine_contract import ContractedAgent

    agent = _Agent()
    held = ContractedAgent(agent)
    held.budget_ceiling = lambda: 1.75
    held.run_role("p", tmp_path, max_budget_usd=0.5)
    assert agent.seen["max_budget_usd"] == 0.5
    held.budget_ceiling = lambda: 0.0
    held.run_role("p", tmp_path)
    # 0 would tell the CLI to stop before the first call; the governor refuses at the
    # stage boundary instead, so the floor is one cent.
    assert agent.seen["max_budget_usd"] == 0.01


def test_an_engine_without_the_capability_is_never_handed_a_ceiling(tmp_path):
    from boss_engine.services.engine_contract import ContractedAgent

    agent = _Agent(budget_limit=False)
    held = ContractedAgent(agent)
    held.budget_ceiling = lambda: 1.75
    held.run_role("p", tmp_path)
    assert agent.seen["max_budget_usd"] is None


def test_the_governed_pipeline_binds_the_runs_remaining_budget():
    import boss_engine.services.governed_code_task_service as gov

    source = inspect.getsource(gov)
    assert "budget_ceiling = _session_budget_ceiling" in source
    assert "unsettled_money" in inspect.getsource(gov)


# ── the typed risk tier and the repair default ───────────────────────────────────────────────

def test_the_governed_pipeline_asks_the_typed_risk_tier_with_the_keyword_fallback():
    import boss_engine.services.governed_code_task_service as gov

    source = inspect.getsource(gov)
    assert "model_risk_tier(" in source
    assert "risk_tier = infer_risk_tier(" in source  # the fallback the model path degrades to


def test_repair_retries_are_on_by_default():
    """The whole repair apparatus (evidence assembly, arbiter, budget-variable depth) shipped
    behind a default of 0, so a plain install ran single-pass builds and none of it ever ran."""
    from boss_engine.config import Settings

    assert Settings(_env_file=None).code_repair_max_retries >= 1
