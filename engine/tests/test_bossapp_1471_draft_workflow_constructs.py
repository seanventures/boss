"""Tests for BOSSAPP-1471: draft_workflow (_spec_from_steps) authors parallel, for_each,
and bounded_loop workflow specs that pass workflows.validate_workflow_spec."""
from __future__ import annotations

from boss_engine.services import workflow_drafting as wd
from boss_engine.services import workflows


def _validate(spec: dict) -> dict:
    assert spec.get("ok") is not False, f"draft failed: {spec}"
    normalized, problems = workflows.normalise_workflow_spec(spec, check_agents=False)
    assert problems == [], f"spec validation failed: {problems}"
    return normalized


def test_draft_parallel_with_inline_branches_and_auto_join():
    spec = wd._spec_from_steps(
        [
            {"id": "intake", "agent": True, "instructions": "Collect problem statement"},
            {
                "id": "investigate",
                "parallel": {
                    "branches": [
                        {"id": "check_logs", "agent": True, "instructions": "Inspect error logs"},
                        {"id": "check_metrics", "agent": True, "instructions": "Inspect latency metrics"},
                    ],
                    "success_policy": "all",
                },
            },
            {"id": "summarize", "agent": True, "instructions": "Write root-cause summary"},
        ],
        {"inputs": ["incident_id"]},
    )
    norm = _validate(spec)
    by_id = {s["id"]: s for s in norm["steps"]}
    assert by_id["intake"]["next"] == ["investigate"]
    assert by_id["investigate"]["kind"] == "parallel"
    assert by_id["investigate"]["branches"] == ["check_logs", "check_metrics"]
    join_id = by_id["investigate"]["join"]
    assert by_id[join_id]["kind"] == "join"
    assert by_id[join_id]["wait_for"] == ["check_logs", "check_metrics"]
    assert by_id["check_logs"]["next"] == [join_id]
    assert by_id["check_metrics"]["next"] == [join_id]
    assert by_id[join_id]["next"] == ["summarize"]


def test_draft_parallel_with_explicit_top_level_branches_and_join():
    spec = wd._spec_from_steps(
        [
            {"id": "fork", "kind": "parallel", "branches": ["a", "b"], "join": "merge", "success_policy": "any"},
            {"id": "a", "agent": True, "instructions": "Branch A"},
            {"id": "b", "agent": True, "instructions": "Branch B"},
            {"id": "merge", "kind": "join", "success_policy": "any"},
            {"id": "finish", "agent": True, "instructions": "Final step"},
        ],
        {},
    )
    norm = _validate(spec)
    by_id = {s["id"]: s for s in norm["steps"]}
    assert by_id["fork"]["branches"] == ["a", "b"]
    assert by_id["fork"]["join"] == "merge"
    assert by_id["merge"]["wait_for"] == ["a", "b"]
    assert by_id["merge"]["success_policy"] == "any"
    assert by_id["a"]["next"] == ["merge"]
    assert by_id["b"]["next"] == ["merge"]
    assert by_id["merge"]["next"] == ["finish"]


def test_draft_for_each_inline_and_top_level_body():
    spec = wd._spec_from_steps(
        [
            {
                "id": "scan_sources",
                "for_each": {
                    "items": "input.source_urls",
                    "body": {
                        "id": "extract_one",
                        "agent": True,
                        "instructions": "Extract facts from the source URL",
                        "input": {"url": {"from_item": "url"}},
                    },
                    "max_items": 50,
                    "concurrency": 5,
                },
            },
            {"id": "publish_report", "agent": True, "instructions": "Publish combined findings"},
        ],
        {},
    )
    norm = _validate(spec)
    by_id = {s["id"]: s for s in norm["steps"]}
    assert by_id["scan_sources"]["kind"] == "for_each"
    assert by_id["scan_sources"]["items"] == "input.source_urls"
    assert by_id["scan_sources"]["body"] == "extract_one"
    assert by_id["scan_sources"]["max_items"] == 50
    assert by_id["scan_sources"]["concurrency"] == 5
    assert by_id["scan_sources"]["next"] == ["publish_report"]
    assert by_id["extract_one"]["next"] == []
    assert "source_urls" in norm["input_schema"]["properties"]
    assert norm["input_schema"]["properties"]["source_urls"]["type"] == "array"


def test_draft_bounded_loop_single_and_multi_step_body():
    spec = wd._spec_from_steps(
        [
            {
                "id": "refine_hypothesis",
                "bounded_loop": {
                    "body": [
                        {"id": "hypothesize", "agent": True, "instructions": "Propose root cause"},
                        {"id": "verify", "agent": True, "instructions": "Verify against evidence"},
                    ],
                    "until": {"path": "steps.verify.output.confirmed", "op": "eq", "value": False},
                    "max_iterations": 4,
                    "on_exhausted": "continue",
                },
            },
            {"id": "write_brief", "agent": True, "instructions": "Write final investigation brief"},
        ],
        {},
    )
    norm = _validate(spec)
    by_id = {s["id"]: s for s in norm["steps"]}
    assert by_id["refine_hypothesis"]["kind"] == "bounded_loop"
    assert by_id["refine_hypothesis"]["body"] == ["hypothesize", "verify"]
    assert by_id["refine_hypothesis"]["max_iterations"] == 4
    assert by_id["refine_hypothesis"]["on_exhausted"] == "continue"
    assert by_id["refine_hypothesis"]["next"] == ["write_brief"]
    assert by_id["hypothesize"]["next"] == []
    assert by_id["verify"]["next"] == []


def test_draft_kg_investigation_pipeline_combining_parallel_for_each_and_bounded_loop():
    spec = wd._spec_from_steps(
        [
            {"id": "scope", "agent": True, "instructions": "Scope investigation question"},
            {
                "id": "gather_parallel",
                "parallel": {
                    "branches": [
                        {"id": "query_kg", "agent": True, "instructions": "Query knowledge graph"},
                        {"id": "query_code", "agent": True, "instructions": "Search repository code"},
                    ],
                },
            },
            {
                "id": "inspect_leads",
                "for_each": {
                    "items": "steps.query_kg.output.leads",
                    "body": {"id": "check_lead", "agent": True, "instructions": "Validate one lead"},
                    "concurrency": 3,
                },
            },
            {
                "id": "converge",
                "bounded_loop": {
                    "body": ["synthesize", "critique"],
                    "expression": {"path": "steps.critique.output.needs_more_evidence", "op": "eq", "value": True},
                    "max_iterations": 3,
                    "on_exhausted": "continue",
                },
            },
            {"id": "synthesize", "agent": True, "instructions": "Synthesize findings"},
            {"id": "critique", "agent": True, "instructions": "Critique evidence gaps"},
            {"id": "deliver", "agent": True, "instructions": "Deliver final investigation report"},
        ],
        {"inputs": ["question"]},
    )
    norm = _validate(spec)
    kinds = {s["id"]: s["kind"] for s in norm["steps"]}
    assert kinds["gather_parallel"] == "parallel"
    assert kinds["inspect_leads"] == "for_each"
    assert kinds["converge"] == "bounded_loop"
    by_id = {s["id"]: s for s in norm["steps"]}
    assert by_id[by_id["gather_parallel"]["join"]]["next"] == ["inspect_leads"]
    assert by_id["inspect_leads"]["next"] == ["converge"]
    assert by_id["converge"]["next"] == ["deliver"]


def test_draft_structured_refusals_are_clear():
    r1 = wd._spec_from_steps([{"id": "p", "parallel": {"branches": [{"id": "only_one", "agent": True}]}}], {})
    assert r1["ok"] is False and "at least two branches" in r1["error"]

    r2 = wd._spec_from_steps([{"id": "l", "bounded_loop": {"body": "a", "until": {"path": "x", "op": "truthy"}, "max_iterations": 500}}], {})
    assert r2["ok"] is False and "1..100" in r2["error"]

    r3 = wd._spec_from_steps([{"id": "fe", "for_each": {"body": {"id": "b", "agent": True}}}], {})
    assert r3["ok"] is False and "items" in r3["error"]
