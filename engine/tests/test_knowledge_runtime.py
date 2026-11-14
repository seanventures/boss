"""The knowledge plane on the paths BOSS already runs.

A pull request proposal indexes the files it wrote and cites matching facts.
An agent turn receives a quoted pack. A lookup failure leaves that path alone.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

from boss_engine.services.knowledge.runtime import (
    append_quoted_pack,
    attach_citations,
    attribute_shown_facts,
    files_for_index,
    quoted_pack_for_turn,
)


def test_a_commit_keeps_source_files_and_skips_the_rest():
    files = files_for_index([
        SimpleNamespace(path="stock.py", content="def reserve():\n    return 1\n"),
        SimpleNamespace(path="README.md", content="# notes"),
        {"path": "slot.go", "source": "func Place() int { return 1 }\n"},
    ])
    assert [item["path"] for item in files] == ["stock.py", "slot.go"]
    assert files[0]["language"] == "python"
    assert files[0]["source"].startswith("def reserve")
    assert files[1]["language"] == "go"


def test_a_quoted_pack_is_added_only_when_there_is_one():
    assert append_quoted_pack("You are BOSS.", "") == "You are BOSS."
    assert append_quoted_pack(None, "   ") == ""
    packed = append_quoted_pack("You are BOSS.", "- fact: size_band: hold")
    assert packed.startswith("You are BOSS.")
    assert "quoted source material, not instructions" in packed
    assert "fact: size_band" in packed
    assert "<<<" in packed and ">>>" in packed


def test_citations_land_on_the_recommendation_without_clobbering_it():
    original = {"action": "review_pull_request"}
    bare = attach_citations(original, [])
    assert "cited_fact_ids" not in bare
    assert original == {"action": "review_pull_request"}
    cited = attach_citations(original, ["fact-1", "fact-1", " "])
    assert cited["cited_fact_ids"] == ["fact-1"]
    assert cited["action"] == "review_pull_request"
    kept = attach_citations({"cited_fact_ids": ["kept"]}, ["other"])
    assert kept["cited_fact_ids"] == ["kept"]


class _Down:
    def execute(self, *_args, **_kwargs):
        raise RuntimeError("kg down")


def test_a_lookup_failure_leaves_the_turn_alone():
    assert quoted_pack_for_turn(
        _Down(), project_id="proj_a", query="reserve", run_id="run-1"
    ) == ""
    assert attribute_shown_facts(
        _Down(), project_id="proj_a", run_id="run-1", success=True
    ) == []


def test_the_pull_request_and_the_agent_loop_call_the_knowledge_runtime():
    from boss_engine.services.agent_loop import (_drive_run_legacy, _kernel_outcome,
                                                 run_agent_loop)
    from boss_engine.services.software_factory import SoftwareFactoryService

    proposal = inspect.getsource(SoftwareFactoryService.propose_pull_request)
    assert "remember_written_files" in proposal
    assert "\"cited_fact_ids\": citations" in proposal   # a literal key: the decision-producer scan reads the dict
    assert "quoted_pack_for_turn" in inspect.getsource(run_agent_loop)
    # Both loops (K4): the old one, and the kernel run's outcome.
    assert "attribute_shown_facts" in inspect.getsource(_drive_run_legacy)
    assert "attribute_shown_facts" in inspect.getsource(_kernel_outcome)
