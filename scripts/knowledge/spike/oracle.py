"""The spike's scripted conformance engine: a BOSS chat adapter that answers from the labels.

No model key is available to the Phase 0 session, and the brief forbids paid runs beyond the
spike's small extraction set. Both options therefore run against this one adapter. It has the
interface of a BOSS chat adapter (`create_text_response(prompt, system_prompt=...)` returns
`(raw, text, usage)`), and it meters every call the way the governed gateway would: calls,
input/output tokens and cost at a named price, plus a simulated latency.

What it measures honestly: how many model calls each option makes, how large the prompts are,
pipeline overhead, latency structure and code size. What it cannot measure: precision. An
oracle that answers from the labels is right by construction, so precision stays PENDING until
a governed run with a real model replaces this adapter.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from corpus import FACT_BY_ID, Episode

CHARS_PER_TOKEN = 4.0


@dataclass
class Meter:
    calls: int = 0
    input_tokens: float = 0.0
    output_tokens: float = 0.0
    embed_calls: int = 0
    embed_inputs: int = 0
    embed_tokens: float = 0.0
    by_purpose: dict[str, int] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, purpose: str, prompt: str, output: str) -> None:
        with self.lock:
            self.calls += 1
            self.input_tokens += len(prompt) / CHARS_PER_TOKEN
            self.output_tokens += len(output) / CHARS_PER_TOKEN
            self.by_purpose[purpose] = self.by_purpose.get(purpose, 0) + 1

    def record_embed(self, texts: list[str]) -> None:
        with self.lock:
            self.embed_calls += 1
            self.embed_inputs += len(texts)
            self.embed_tokens += sum(len(t) for t in texts) / CHARS_PER_TOKEN


def minimal_valid(model: Any) -> Any:
    """The smallest instance a pydantic response model accepts (empty lists, nulls, zeros)."""
    import typing

    from pydantic import BaseModel

    def value_for(annotation):
        origin = typing.get_origin(annotation)
        args = typing.get_args(annotation)
        if annotation is type(None) or (origin in (typing.Union, getattr(__import__("types"), "UnionType", None))
                                        and type(None) in args):
            return None
        if origin in (list, tuple, set):
            return []
        if origin is dict:
            return {}
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return minimal_valid(annotation)
        return {str: "", int: 0, float: 0.0, bool: False}.get(annotation, None)

    out = {}
    for name, f in model.model_fields.items():
        if f.is_required():
            out[name] = value_for(f.annotation)
    return out


class ScriptedOracle:
    """Answers as a perfect extractor would, for the episode currently being ingested."""

    def __init__(self, meter: Meter, *, base_latency_s: float = 0.25, per_output_token_s: float = 0.005):
        self.meter = meter
        self.base_latency_s = base_latency_s
        self.per_output_token_s = per_output_token_s
        self.episode: Episode | None = None
        self.expect: Any = None          # set by the Graphiti client: the response model wanted
        self.local = threading.local()

    def _facts(self):
        return [FACT_BY_ID[f] for f in (self.episode.fact_ids if self.episode else [])]

    def answer(self, expect: Any) -> str:
        facts = self._facts()
        if expect is None:                                    # option B's own schema
            return json.dumps({"facts": [
                {"subject": f.subject, "predicate": f.predicate, "object": f.object,
                 "statement": f.statement, "kind": f.kind, "confidence": 0.8} for f in facts]})
        name = expect.__name__
        when = self.episode.reference_time.isoformat() if self.episode else None
        if name in ("EdgeTimestamps",):
            return json.dumps({"valid_at": when, "invalid_at": None})
        if name == "BatchEdgeTimestamps":
            return json.dumps({"timestamps": [{"valid_at": when, "invalid_at": None} for _ in facts]})
        entities = list(dict.fromkeys([f.subject for f in facts] + [f.object for f in facts]))
        edges = [{"source_entity_name": f.subject, "target_entity_name": f.object,
                  "relation_type": f.predicate.upper().replace(" ", "_"), "fact": f.statement,
                  "valid_at": when, "invalid_at": None, "episode_indices": [0]} for f in facts]
        if name == "ExtractedEntities":
            return json.dumps({"extracted_entities": [{"name": e, "entity_type_id": 0, "episode_indices": [0]}
                                                      for e in entities]})
        if name == "ExtractedEdges":
            return json.dumps({"edges": edges})
        if name == "CombinedExtraction":
            return json.dumps({"extracted_entities": [{"name": e, "entity_type_id": 0, "episode_indices": [0]}
                                                      for e in entities], "edges": edges})
        return json.dumps(minimal_valid(expect))

    @staticmethod
    def _section(prompt: str, tag: str) -> str:
        m = re.search(rf"<{tag}>\n(.*?)\n</{tag}>", prompt, re.DOTALL)
        return m.group(1) if m else ""

    def answer_edge_duplicate(self, prompt: str) -> str:
        """Graphiti's dedupe/contradiction call, answered from the labels (duplicates by statement,
        contradictions by the labelled supersession)."""
        new = self._section(prompt, "NEW FACT").strip()
        new_fact = next((f for f in FACT_BY_ID.values() if f.statement == new), None)
        candidates = []
        for tag in ("EXISTING FACTS", "FACT INVALIDATION CANDIDATES"):
            body = self._section(prompt, tag)
            candidates += [(int(i), f.strip()) for i, f in re.findall(r"'idx': (\d+), 'fact': ['\"](.*?)['\"]\}", body)]
        existing_n = len(re.findall(r"'idx'", self._section(prompt, "EXISTING FACTS")))
        old = FACT_BY_ID.get(new_fact.supersedes).statement if new_fact and new_fact.supersedes else None
        return json.dumps({"duplicate_facts": [i for i, f in candidates if f == new and i < existing_n],
                           "contradicted_facts": [i for i, f in candidates if old and f == old]})

    def create_text_response(self, prompt: str, system_prompt: str | None = None, **_kw):
        expect = getattr(self.local, "expect", self.expect)
        if getattr(expect, "__name__", "") == "EdgeDuplicate":
            text = self.answer_edge_duplicate(prompt)
        else:
            text = self.answer(expect)
        full = (system_prompt or "") + prompt
        self.meter.record(getattr(expect, "__name__", "facts"), full, text)
        time.sleep(self.base_latency_s + self.per_output_token_s * len(text) / CHARS_PER_TOKEN)
        usage = {"prompt_tokens": int(len(full) / CHARS_PER_TOKEN),
                 "completion_tokens": int(len(text) / CHARS_PER_TOKEN), "model": "scripted-oracle"}
        return {"scripted": True}, text, usage
