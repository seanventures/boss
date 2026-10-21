"""Knowledge Graph Phase 0 spike (scripts/knowledge/spike): the corpus, B's output validation, the scorer.

Option B treats a model's answer as untrusted data: anything off-schema is dropped, never
repaired by guessing. The corpus holds 50 labelled facts, each stated in at least two of 200
episodes, with four supersessions. The scorer counts a non-durable "fact" as an error.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SPIKE = Path(__file__).resolve().parents[2] / "scripts/knowledge/spike"
sys.path.insert(0, str(SPIKE))

import corpus  # noqa: E402
import option_b  # noqa: E402


def test_the_corpus_is_200_episodes_over_50_labelled_facts_with_four_supersessions():
    episodes = corpus.episodes()
    assert len(episodes) == 200 and len(corpus.FACTS) == 50
    seen = {f for e in episodes for f in e.fact_ids}
    assert seen == {f.id for f in corpus.FACTS}
    assert [f.id for f in corpus.FACTS if f.supersedes] == ["F41", "F42", "F43", "F44"]
    assert episodes == corpus.episodes()          # deterministic


def test_b_keeps_only_schema_valid_facts():
    good = {"subject": "reservation TTL", "predicate": "is", "object": "10 minutes",
            "statement": "The reservation TTL is now 10 minutes.", "kind": "decision", "confidence": 0.9}
    raw = "Sure! " + json.dumps({"facts": [
        good,
        {**good, "kind": "gossip"},                 # not a kind
        {**good, "confidence": 3},                  # out of range
        {**good, "object": ""},                     # empty
        {"subject": "x"},                           # missing fields
        "ignore previous instructions",             # not an object
    ]})
    facts = option_b.parse_facts(raw)
    assert [f.statement for f in facts] == [good["statement"]]
    assert option_b.parse_facts("no json at all") == [] and option_b.parse_facts("{broken") == []


def test_b_makes_one_call_per_episode_through_the_adapter():
    calls = []

    class Adapter:
        def create_text_response(self, prompt, system_prompt=None):
            calls.append((prompt, system_prompt))
            return {}, '{"facts": []}', {"prompt_tokens": 1}

    assert option_b.extract(Adapter(), "[spec] Build took 3 minutes.") == []
    assert len(calls) == 1 and "quoted data" in calls[0][0] and "DURABLE" in calls[0][1]
