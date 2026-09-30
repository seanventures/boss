"""Which providers did this project actually call?

Why this exists
---------------
"BOSS is not coupled to any one model" is a claim, and until now there was no way to check it.
An operator could set `BOSS_CODE_AGENT_ENGINE`, `BOSS_CHAT_LLM_ENGINE`, `BOSS_LLM_ENGINE`,
`BOSS_CODE_REVIEW_ENGINE` and the strong/low-cost pair — six settings, in three different
vocabularies — and then had no instrument to confirm the result. Hope is not a validation.

Every model call already writes an `llm_calls` row carrying its engine and model. This turns
that ledger into the answer.

Transport is not vendor
-----------------------
The distinction this module exists to make: routing `anthropic/claude-opus-4.5` through
OpenRouter means the TRANSPORT is OpenRouter and the WEIGHTS are Anthropic's — your prompt
still reached Anthropic. A report that collapsed those two into one number would let somebody
claim "no Anthropic" while sending Anthropic every token. So both are reported, always, and
`violations` is computed against whichever one the caller says they care about.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Iterable, Optional

from ..ports import sql

#: model-name fragment -> who trained the weights. Ordered: first match wins, so the more
#: specific fragments come first.
_VENDOR_PATTERNS: tuple[tuple[str, str], ...] = (
    ("anthropic/", "anthropic"),
    ("claude", "anthropic"),
    ("openai/", "openai"),
    ("gpt-", "openai"),
    ("o1-", "openai"),
    ("o3-", "openai"),
    ("codex", "openai"),
    ("google/", "google"),
    ("gemini", "google"),
    ("deepseek", "deepseek"),
    ("qwen", "alibaba"),
    ("moonshot", "moonshot"),
    ("kimi", "moonshot"),
    ("z-ai/", "zhipu"),
    ("glm", "zhipu"),
    ("minimax", "minimax"),
    ("mistral", "mistral"),
    ("llama", "meta"),
    ("grok", "xai"),
    ("x-ai/", "xai"),
    ("nemotron", "nvidia"),
)


def vendor_of(model: Optional[str]) -> str:
    """Who trained the weights behind this model name.

    Deliberately independent of the transport: `anthropic/claude-opus-4.5` is Anthropic
    whether it arrived over OpenRouter, Bedrock or Anthropic's own API.
    """
    m = (model or "").strip().lower()
    if not m:
        return "unknown"
    for fragment, vendor in _VENDOR_PATTERNS:
        if fragment in m:
            return vendor
    return "unknown"


def transport_of(engine: Optional[str]) -> str:
    """The endpoint BOSS called. Shares one mapping with the rate limiter, on purpose:
    two answers to 'which provider is this' is one answer too many."""
    from .model_rate_limiter import provider_of

    return provider_of(engine)


@dataclass
class ProviderUsage:
    transport: str
    vendor: str
    calls: int = 0
    ok: int = 0
    failed: int = 0
    cost_usd_micros: int = 0
    models: set = field(default_factory=set)
    engines: set = field(default_factory=set)
    stages: set = field(default_factory=set)

    def as_dict(self) -> dict:
        return {
            "transport": self.transport,
            "vendor": self.vendor,
            "calls": self.calls,
            "ok": self.ok,
            "failed": self.failed,
            "cost_usd": round(self.cost_usd_micros / 1_000_000, 6),
            "models": sorted(self.models),
            "engines": sorted(self.engines),
            "stages": sorted(self.stages),
        }


def summarise(rows: Iterable[dict]) -> list[dict]:
    """Group raw llm_calls rows by (transport, vendor)."""
    buckets: dict[tuple[str, str], ProviderUsage] = {}
    for row in rows:
        engine = row.get("engine") or ""
        model = row.get("model") or ""
        key = (transport_of(engine), vendor_of(model))
        usage = buckets.setdefault(key, ProviderUsage(transport=key[0], vendor=key[1]))
        usage.calls += 1
        if row.get("ok", True):
            usage.ok += 1
        else:
            usage.failed += 1
        usage.cost_usd_micros += int(row.get("cost_usd_micros") or 0)
        if model:
            usage.models.add(model)
        if engine:
            usage.engines.add(engine)
        if row.get("stage"):
            usage.stages.add(str(row["stage"]))
    return [u.as_dict() for u in
            sorted(buckets.values(), key=lambda u: (-u.calls, u.transport, u.vendor))]


def violations(summary: list[dict], *, expect: Optional[str], dimension: str = "vendor") -> list[dict]:
    """Every bucket that is NOT the provider the caller expected.

    `dimension` is the question being asked. "vendor" answers "did anything reach Anthropic?";
    "transport" answers "did anything leave through an endpoint other than this one?". They
    are different questions and a validation run should be explicit about which it means.
    """
    want = (expect or "").strip().lower()
    if not want:
        return []
    return [b for b in summary if (b.get(dimension) or "") != want]


_SELECT = (
    "SELECT engine, model, stage, ok, cost_usd_micros, created_at, code_task_run_id "
    "FROM llm_calls WHERE project_id = :p"
)


def audit_project(connection, project_id: str, *, since_hours: Optional[int] = None,
                  run_id: Optional[str] = None, expect: Optional[str] = None,
                  dimension: str = "vendor", limit: int = 20000) -> dict:
    """The providers a project actually called, from the ledger rather than from config."""
    clause, params = _SELECT, {"p": project_id}
    if run_id:
        clause += " AND code_task_run_id = :r"
        params["r"] = run_id
    if since_hours:
        clause += " AND created_at >= now() - make_interval(hours => :h)"
        params["h"] = int(since_hours)
    clause += " ORDER BY created_at DESC LIMIT :lim"
    params["lim"] = int(limit)
    rows = [dict(r) for r in connection.execute(sql(clause), params).mappings()]
    summary = summarise(rows)
    bad = violations(summary, expect=expect, dimension=dimension)
    return {
        "project_id": project_id,
        "run_id": run_id,
        "calls": len(rows),
        "window_hours": since_hours,
        "by_provider": summary,
        "expected": (expect or None),
        "dimension": dimension,
        "violations": bad,
        # The single line a validation run is actually asking for.
        "clean": (not bad) if expect else None,
    }
