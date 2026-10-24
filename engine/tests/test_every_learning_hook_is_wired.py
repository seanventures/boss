"""Learning while executing is wired into the ONE pipeline (BOSSAPP-768, spec §4.1, §4.2).

The enforcing guard for the class "an agent learned something and nothing kept it": every hook
the spec names is called at its place in the pipeline (never reconstructed afterwards), every
agent holds record_learning through the same baseline bundle, the shared primer tells every
agent to consult before acting and record what it learned (general guidance, no per-case list),
every typed output can carry its citations and what it learned, and every build's shared task
context carries what earlier runs learned.
"""
from __future__ import annotations

import inspect

from boss_engine.services import agent_baseline as ab
from boss_engine.services.knowledge import learning_writeback as lw


def test_every_hook_is_called_where_the_pipeline_sees_it():
    from boss_engine.services import agent_service, governed_code_task_service as g, harness_failures, workflows

    governed = inspect.getsource(g)
    assert 'hook="build_stage"' in inspect.getsource(g) and "def _abort_if_cancelled" in governed
    abort = governed[governed.index("def _abort_if_cancelled"):]
    assert 'hook="build_stage"' in abort[:abort.index("def ", 10)]          # at every stage boundary
    assert governed.count('hook="repair_checkpoint"') == 1
    assert "learning_context.for_build(" in governed                        # every stage's shared context
    assert "observe_turn(turn)" in inspect.getsource(harness_failures.check_turn)
    assert 'hook="step_complete"' in inspect.getsource(workflows.complete_step)
    assert 'hook="agent_decide"' in inspect.getsource(agent_service.AgentService.decide)
    assert set(lw._PLANNERS) == set(lw.HOOKS)


def test_every_agent_holds_record_learning_through_the_baseline():
    assert "record_learning" in ab.bundles()["knowledge.propose"]
    spec = ab.bundle_tool_spec("knowledge.propose", "record_learning")
    assert spec is not None and spec.kind == "write" and spec.reversibility == "safe"
    assert "evidence" in spec.parameters["required"] and "agent_role" not in str(spec.parameters)
    from boss_engine.tools.builtin import _DISCUSS_OPERATIONS_TOOLS

    assert "record_learning" in _DISCUSS_OPERATIONS_TOOLS                     # chat investigations too


def test_the_primer_tells_every_agent_to_consult_then_record():
    for word in ("record_learning", "kg_rules", "kg_precedent", "[R:", "[L:", "ruled out", "evidence"):
        assert word in ab.PRIMER, word
    assert "[R:" in ab.BUILD_PRIMER and "[L:" in ab.BUILD_PRIMER


def test_a_typed_output_carries_its_citations_and_what_it_learned():
    from boss_engine.services import typed_decisions as td

    batch = td.DecisionBatch(decisions={"decision": td._parse(
        '{"decision": {"answer": "a", "confidence": 0.9, "reason": "r", "cited_rule_ids": ["kgn_r1"], '
        '"cited_learning_ids": ["kgn_l1", "<ids of [L:<id>] learnings>"], "learned": [{"learning_kind": "fact"}]}}',
        [td.Question("decision", "q", {"a": "", "b": ""})])["decision"]}, backend="t", model="m", latency_ms=1)
    out = td._step_output({}, batch)
    assert out["cited_rule_ids"] == ["kgn_r1"] and out["cited_learning_ids"] == ["kgn_l1"]
    assert out["learned"] == [{"learning_kind": "fact"}]
    prompt = td._prompt("ctx", [td.Question("decision", "q", {"a": "", "b": ""})], precedent="- [L:kgn_l1] x")
    assert "cited_learning_ids" in prompt and "learned" in prompt
    assert "cited_learning_ids" not in td._prompt("ctx", [td.Question("decision", "q", {"a": "", "b": ""})])


def test_citations_read_the_tags_the_pack_quotes():
    assert lw.citations("used [L:kgn_abc123] and [R:kgn_rule99], not kgn_plain1") == ["kgn_abc123", "kgn_rule99"]
    assert lw.citations({"x": "kgn_plain12"}, bare=True) == ["kgn_plain12"]


def test_the_learning_code_holds_no_keyword_lists_of_cases():
    """What an agent learned is decided by the agent (record_learning) or by the pipeline's own
    typed records, never by matching words in prose."""
    source = inspect.getsource(lw)
    for smell in ("if \"timeout\" in", "if 'timeout' in", "KEYWORDS", "keyword_list"):
        assert smell not in source
