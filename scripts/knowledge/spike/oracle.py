"""The spike's scripted conformance engine: a BOSS chat adapter that answers from the labels.

No model key was available to the Phase 0 session. Both options ran against this one adapter;
option A (Graphiti) was removed after the 2026-10-21 decision. It has the
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
import threading
import time
from dataclasses import dataclass, field

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


class ScriptedOracle:
    """Answers as a perfect extractor would, for the episode currently being ingested."""

    def __init__(self, meter: Meter, *, base_latency_s: float = 0.25, per_output_token_s: float = 0.005):
        self.meter = meter
        self.base_latency_s = base_latency_s
        self.per_output_token_s = per_output_token_s
        self.episode: Episode | None = None

    def _facts(self):
        return [FACT_BY_ID[f] for f in (self.episode.fact_ids if self.episode else [])]

    def answer(self) -> str:
        """Option B's schema: the episode's labelled facts."""
        return json.dumps({"facts": [
            {"subject": f.subject, "predicate": f.predicate, "object": f.object,
             "statement": f.statement, "kind": f.kind, "confidence": 0.8} for f in self._facts()]})

    def create_text_response(self, prompt: str, system_prompt: str | None = None, **_kw):
        text = self.answer()
        full = (system_prompt or "") + prompt
        self.meter.record("facts", full, text)
        time.sleep(self.base_latency_s + self.per_output_token_s * len(text) / CHARS_PER_TOKEN)
        usage = {"prompt_tokens": int(len(full) / CHARS_PER_TOKEN),
                 "completion_tokens": int(len(text) / CHARS_PER_TOKEN), "model": "scripted-oracle"}
        return {"scripted": True}, text, usage
