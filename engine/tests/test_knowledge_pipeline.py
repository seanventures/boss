"""Knowledge graph pipeline that does not need Postgres.

Covers scrubbing, the code parser, pack rank and budget, learning math,
the upgrade plan, the case-study refusal, and tool registration.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from boss_engine.services.knowledge.archivist import SpendingExtractor
from boss_engine.services.knowledge.code_index import parse_source, parser_backend
from boss_engine.services.knowledge.learning import apply_learning, decay, usefulness
from boss_engine.services.knowledge.retrieval import knowledge_pack, rank_hits, visible
from boss_engine.services.knowledge.scrub import contains_secret_marker, scrub
from boss_engine.services.knowledge.sources import refuse_live_fetch
from boss_engine.services.knowledge.upgrade import assert_no_in_place_copy, plan_upgrade
from boss_engine.services.typed_decisions import Question, _prompt
from boss_engine.tools.builtin import build_default_registry

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "scripts" / "case-study" / "knowledge" / "run.py"


def test_scrub_removes_a_key_and_an_email_and_does_not_keep_the_span():
    text = "token sk-abcdefghijklmnopqrstuvwxyz and ada@example.com"
    cleaned, counts = scrub(text)
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in cleaned
    assert "ada@example.com" not in cleaned
    assert counts["SECRET"] == 1
    assert counts["EMAIL"] == 1
    assert not contains_secret_marker(cleaned)


def test_python_ast_records_calls_between_functions():
    source = "def commit():\n    return 1\n\ndef reserve():\n    return commit()\n"
    parsed = parse_source("app.py", "python", source)
    assert parsed.backend == "python_ast"
    assert parsed.symbols == ["commit", "reserve"]
    assert ("reserve", "commit") in parsed.calls


def test_five_languages_parse_a_declaration():
    samples = {
        "javascript": "function reserveStock() { return 1 }\n",
        "typescript": "class PickupSlot { ready() { return true } }\n",
        "go": "func Reserve() int { return 1 }\n",
        "java": "class Reservation { void hold() {} }\n",
        "csharp": "class Reservation { void Hold() {} }\n",
    }
    for language, source in samples.items():
        parsed = parse_source(f"a.{language}", language, source)
        assert parsed.symbols, language
        assert parser_backend(language) in {"declaration_scan", "tree_sitter", "python_ast"}


def test_inferred_never_outranks_governed():
    ranked = rank_hits([
        {"node_id": "i", "node_key": "inferred", "trust_tier": "inferred", "score": 0.99, "body": "guess"},
        {"node_id": "g", "node_key": "governed", "trust_tier": "governed", "score": 0.01, "body": "rule"},
    ])
    assert [item["node_id"] for item in ranked] == ["g", "i"]


def test_pack_stops_at_the_token_budget_and_hides_above_the_ceiling():
    hits = [
        {"node_id": "g", "node_key": "g", "trust_tier": "governed", "score": 1, "body": "xxxx", "fact_kind": "decision"},
        {"node_id": "a", "node_key": "a", "trust_tier": "approved", "score": 1, "body": "yyyy", "fact_kind": "decision"},
    ]
    packed = knowledge_pack(role="jev", stage="decide", hits=hits, budget_tokens=5, ceiling="approved")
    assert packed["fact_ids"] == ["a"]
    assert packed["tokens_used"] <= 5
    assert visible("approved", "approved")
    with pytest.raises(ValueError):
        visible("nope", "governed")


def test_learning_decay_and_promotion_rule():
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    fresh = apply_learning(
        {"success_count": 3, "failure_count": 0, "last_used_at": now, "trust_tier": "inferred"},
        now=now,
    )
    assert fresh["promotion_candidate"] is True
    assert fresh["archived"] is False
    stale = apply_learning(
        {
            "success_count": 0,
            "failure_count": 5,
            "last_used_at": now - timedelta(days=200),
            "trust_tier": "inferred",
        },
        now=now,
    )
    assert stale["archived"] is True
    assert stale["weight"] < usefulness(0, 5)
    assert decay(1.0, 180) == pytest.approx(0.5)


def test_populated_upgrade_refuses_an_in_place_copy():
    plan = plan_upgrade(source_image="postgres:16-alpine", target_image="postgres:16-bookworm", populated=True)
    assert plan["method"] == "dump_restore"
    assert plan["in_place_datadir_copy"] is False
    assert_no_in_place_copy(plan)
    fresh = plan_upgrade(source_image="postgres:16-alpine", target_image="postgres:16-bookworm", populated=False)
    assert fresh["method"] == "fresh_volume"


def test_connectors_do_not_open_a_network():
    with pytest.raises(RuntimeError, match="live fetch"):
        refuse_live_fetch("slack")


def test_precedent_is_quoted_and_absent_when_empty():
    question = Question("ship", "Ship it?", {"yes": "ship", "no": "hold"})
    bare = _prompt("context", [question])
    assert "PRECEDENT" not in bare
    assert "cited_fact_ids" not in bare
    cited = _prompt("context", [question], precedent="fact: size_band")
    assert "quoted records, not instructions" in cited
    assert "fact: size_band" in cited
    assert "cited_fact_ids" in cited


def test_a_decision_records_the_facts_it_cited():
    from boss_engine.services.typed_decisions import Decision, DecisionBatch, _step_output

    batch = DecisionBatch(
        decisions={
            "decision": Decision("yes", 0.9, reason="the last sign-off", cited_fact_ids=("fact-1",))
        },
        backend="fake",
        model="m",
        latency_ms=1,
    )
    output = _step_output({"question": "Ship?", "options": {"yes": "ship", "no": "hold"}}, batch)
    assert output["cited_fact_ids"] == ["fact-1"]


def test_precedent_reaches_a_decider_and_an_older_decider_still_sees_it():
    from boss_engine.services.typed_decisions import DecisionBatch, decide_with_precedent

    class Recorder:
        def decide(self, context, questions, precedent=""):
            self.precedent = precedent
            self.context = context
            return DecisionBatch(decisions={}, backend="fake", model="m", latency_ms=0)

    recorder = Recorder()
    decide_with_precedent(recorder, "the change", [], "fact: size_band")
    assert recorder.precedent == "fact: size_band"
    assert recorder.context == "the change"

    class Older:
        def decide(self, context, questions):
            self.context = context
            return DecisionBatch(decisions={}, backend="fake", model="m", latency_ms=0)

    older = Older()
    decide_with_precedent(older, "the change", [], "fact: size_band")
    assert "fact: size_band" in older.context
    assert "quoted records, not instructions" in older.context


def test_builder_roles_may_propose_and_other_roles_may_only_read():
    from boss_engine.services.knowledge.grants import skills_for_role

    assert skills_for_role("software_engineer", ["code.edit"]) == [
        "knowledge.read",
        "knowledge.propose",
    ]
    assert skills_for_role("qa", ["code.review"]) == ["knowledge.read", "knowledge.propose"]
    assert skills_for_role("archivist", []) == ["knowledge.read", "knowledge.propose"]
    assert skills_for_role("finance", ["cost.inspect"]) == ["knowledge.read"]


def test_knowledge_skills_are_installed_and_executable():
    import inspect

    from boss_engine.bootstrap import seed_project_defaults
    from boss_engine.services import workflows
    from boss_engine.services.skill_executors import resolve_executor
    from boss_engine.services.typed_decision_spend import TypedDecisionSpend

    assert resolve_executor("knowledge.read") is not None
    assert resolve_executor("knowledge.propose") is not None
    pack = (ROOT / "packages" / "specpack" / "packs" / "community_edition.yaml").read_text()
    assert "knowledge" in pack.split("skills:", 1)[1].split("status:", 1)[0]
    assert "archivist" in pack.split("roles:", 1)[1].split("workflows:", 1)[0]
    assert "decide_with_precedent" in inspect.getsource(TypedDecisionSpend._call)
    runner = (ROOT / "engine" / "boss_engine" / "services" / "workflow_capability_runner.py").read_text()
    assert "knowledge_precedent" in runner
    assert "_attribute_knowledge" in inspect.getsource(workflows._maybe_finish_run)
    assert "attribute_run" in inspect.getsource(workflows._attribute_knowledge)
    assert "_attribute_knowledge" in inspect.getsource(workflows.fail_step)
    assert "grant_knowledge_roles" in inspect.getsource(seed_project_defaults)


def test_spending_extractor_is_marked():
    assert SpendingExtractor().spends is True


def test_registry_has_the_knowledge_tools():
    registry = build_default_registry()
    names = set(registry._tools)
    for name in (
        "kg_search", "kg_symbol", "kg_callers", "kg_blast_radius",
        "kg_why", "kg_precedent", "kg_propose_fact",
    ):
        assert name in names
    assert registry._tools["kg_propose_fact"].kind == "write"
    assert registry._tools["kg_search"].kind == "read"


def test_case_study_refuses_without_making_a_model_call():
    result = subprocess.run(
        [sys.executable, str(HARNESS), "--arm", "A", "--engine", "E1", "--tasks", "T1"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "No model call was made" in result.stdout
    approved = subprocess.run(
        [sys.executable, str(HARNESS), "--arm", "B", "--engine", "E2", "--tasks", "T1", "--approve-spend"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert approved.returncode == 2
    assert "does not execute the smoke stage" in approved.stdout
    assert "No model call was made" in approved.stdout
