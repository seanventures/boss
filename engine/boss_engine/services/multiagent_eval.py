"""IS A SUPERVISOR WORTH ITS TOKENS? The AP-P7 acceptance harness (a) (BOSSAPP-1058; design §5
Phase 7: "a lead with 3 parallel read-only sub-agents beats the single-agent baseline on the
dataset's judge score at a reported token multiple. Both numbers are shown").

One P4 dataset of research briefs (services/evals: datasets, examples, experiments). Each brief has
three sub-questions answered from a small corpus through one read tool (`search`). Two targets run
over it as two experiments:

  lead       a supervisor with three read-only `researcher` sub-agents (kernel/subagents): one
             `task` per sub-question in ONE turn, so they run in parallel; the lead synthesises
             their capped reports.
  baseline   one agent with the same tool, searching each sub-question in turn.

Each result keeps the answer, the run id and its TOKENS — the lead's own calls plus every child's
(the child usage the `task` result carries) — and the experiment summary keeps the totals. The
report is the pass rate of each (`check_example`: the rule judge, the answer must cite the
corpus's facts) and the TOKEN MULTIPLE (lead / baseline).

THE ENGINE. Real-money model spend is frozen, so the model is the scripted engine with `measure`
on (tokens = what a real call of that size would spend, chars/4). That makes the token multiple a
measurement of the coordination's overhead — context per child, the reports, the synthesis — and
says NOTHING about quality: the scripted researchers are equally good by construction. The
quality half (an LLM judge over a real engine's answers) is `judge.status: pending_real_engine`
until the operator approves the spend; `compare_research(engine_factory=...)` runs it on any
engine when that is approved.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

#: Three sub-questions per brief; the corpus the `search` tool reads.
BRIEFS: list = [
    {"brief": "Should the checkout service move from Postgres 13 to 16?",
     "parts": ["postgres 16 performance", "postgres 16 breaking changes", "checkout load"]},
    {"brief": "Is the AML alert backlog a staffing or a tuning problem?",
     "parts": ["aml alert volume", "aml false positive rate", "analyst capacity"]},
    {"brief": "Why did build times double last month?",
     "parts": ["build cache hit rate", "test suite growth", "runner pool size"]},
    {"brief": "Can the help desk bot answer billing questions safely?",
     "parts": ["billing question mix", "bot refusal accuracy", "pii exposure"]},
]

CORPUS: dict = {
    "postgres 16 performance": "PG16 parallel aggregates cut report time 31%",
    "postgres 16 breaking changes": "PG16 removes the old vacuum_defer setting",
    "checkout load": "checkout peaks at 1,900 writes/s on Fridays",
    "aml alert volume": "alerts grew 2.4x after the March rule change",
    "aml false positive rate": "97% of the new alerts closed as false positives",
    "analyst capacity": "analyst headcount is flat at 14",
    "build cache hit rate": "the remote cache hit rate fell from 82% to 41%",
    "test suite growth": "the suite grew 9% in the month",
    "runner pool size": "the runner pool is unchanged at 40",
    "billing question mix": "38% of tickets are billing",
    "bot refusal accuracy": "the bot refuses 99.1% of account-specific asks",
    "pii exposure": "no PII left the bot in a 5,000-ticket red-team",
}

LEAD_SYSTEM = ("You are the lead researcher. Split the brief into its sub-questions, give each to "
               "a researcher sub-agent with `task` IN ONE MESSAGE, then write the answer from "
               "their reports, citing each fact.")
SINGLE_SYSTEM = ("You are a researcher. Search each part of the brief, then write the answer, "
                 "citing each fact.")


def _search_tool() -> Any:
    from ..kernel.toolset import FunctionToolSet, Tool
    from ..kernel.types import ToolSchema

    def search(args: dict) -> dict:
        q = str(args.get("q") or "").strip().lower()
        hit = CORPUS.get(q) or next((v for k, v in CORPUS.items() if k in q or q in k), None)
        return {"hits": [hit] if hit else [], "query": q}

    return FunctionToolSet([Tool(ToolSchema(
        "search", "Search the research corpus for one sub-question.",
        {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"],
         "additionalProperties": False}), search)])


def _facts(messages: list) -> list:
    """Every fact a tool or a sub-agent returned in this conversation, in order."""
    import json

    out: list = []
    for message in messages:
        if message.get("role") != "tool":
            continue
        try:
            body = json.loads(message.get("content") or "{}")
        except ValueError:
            continue
        for fact in body.get("hits") or []:
            out.append(str(fact))
        if body.get("report"):
            out.append(str(body["report"]))
    return out


def scripted_brain(parts: list) -> Callable:
    """The scripted model for one brief: what a competent lead, sub-agent or single agent does."""
    from ..kernel.scripted import Call, Calls, Say

    def brain(messages: list, tools: Optional[list]) -> Any:
        system = str(messages[0].get("content") or "") if messages else ""
        offered = tools or []
        facts = _facts(messages)
        if "task" in offered:                              # the lead
            if not any(m.get("role") == "tool" for m in messages):
                return Calls(*[Call("task", {"subagent": "researcher", "brief": p})
                               for p in parts])
            return Say("Answer, from three researchers: " + "; ".join(facts))
        if "sub-agent" in system:                           # a researcher sub-agent
            if not facts:
                return Call("search", {"q": messages[-1]["content"]})
            return Say("Finding: " + "; ".join(facts))
        searched = sum(1 for m in messages if m.get("role") == "tool")   # the single agent
        if searched < len(parts):
            return Call("search", {"q": parts[searched]})
        return Say("Answer: " + "; ".join(facts))

    return brain


def _tokens(usage: Any) -> int:
    return int(usage.input_tokens + usage.output_tokens + usage.cache_read_tokens
               + usage.cache_write_tokens)


def run_lead(example: dict, *, project_id: str, engine_factory: Optional[Callable] = None,
             sink: Any = None, store: Any = None) -> dict:
    from .. import kernel
    from ..kernel import presets
    from ..kernel.scripted import Reply, ScriptedEngine
    from ..kernel.subagents import Coordination, SubagentDef, Subagents

    parts = list((example.get("input") or {}).get("parts") or [])
    model = engine_factory() if engine_factory else ScriptedEngine(
        [], after=Reply(scripted_brain(parts)), measure=True)
    supervisor = Subagents(
        Coordination(enabled=True, max_subagents=3, max_parallel=3, max_depth=1,
                     max_tokens=60_000, general_purpose=False),
        defs={"researcher": SubagentDef(ref="researcher", description="answers one sub-question "
                                        "from the corpus", report_tokens=400)})
    started = time.monotonic()
    result = kernel.run(kernel.RunSpec(
        engine=model, tools=_search_tool(), policies=presets.agent_loop(6),
        project_id=project_id, system=LEAD_SYSTEM, stage="research_lead",
        messages=[{"role": "user", "content": str((example.get("input") or {}).get("brief"))}],
        middleware=(supervisor,), sink=sink, actor="agent:research_lead", store=store))
    children = [t["result"] for t in result.trace
                if t.get("tool") == "task" and isinstance(t.get("result"), dict)]
    child_tokens = sum(int(c.get("tokens") or 0) for c in children)
    return {"output": {"text": result.text, "subagents": len(children)},
            "run_id": result.run_id, "status": result.status,
            "tokens": _tokens(result.usage) + child_tokens, "lead_tokens": _tokens(result.usage),
            "child_tokens": child_tokens, "subagents": len(children),
            "latency_ms": int((time.monotonic() - started) * 1000),
            "cost_usd_micros": int(result.usage.cost_usd_micros or 0)}


def run_single(example: dict, *, project_id: str, engine_factory: Optional[Callable] = None,
               sink: Any = None, store: Any = None) -> dict:
    from .. import kernel
    from ..kernel import presets
    from ..kernel.scripted import Reply, ScriptedEngine

    parts = list((example.get("input") or {}).get("parts") or [])
    model = engine_factory() if engine_factory else ScriptedEngine(
        [], after=Reply(scripted_brain(parts)), measure=True)
    started = time.monotonic()
    result = kernel.run(kernel.RunSpec(
        engine=model, tools=_search_tool(), policies=presets.agent_loop(8),
        project_id=project_id, system=SINGLE_SYSTEM, stage="research_single",
        messages=[{"role": "user", "content": str((example.get("input") or {}).get("brief"))}],
        sink=sink, actor="agent:research_single", store=store))
    return {"output": {"text": result.text}, "run_id": result.run_id, "status": result.status,
            "tokens": _tokens(result.usage), "subagents": 0,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "cost_usd_micros": int(result.usage.cost_usd_micros or 0)}


def ensure_dataset(c: Any, project_id: str, *, actor: str) -> dict:
    """The research dataset (one confirmed example per brief; the reference: cite its facts)."""
    from . import evals

    ds = evals.ensure_dataset(c, project_id, "ap-p7-research", kind="custom", created_by=actor,
                              description="AP-P7 (a): lead + 3 sub-agents vs one agent")
    if not evals.list_examples(c, project_id, ds["id"], status="confirmed"):
        for brief in BRIEFS:
            evals.add_example(c, project_id, ds["id"], source="manual", proposed_by=actor,
                              input=brief,
                              reference={"mentions_any": [CORPUS[p] for p in brief["parts"]]},
                              dedupe_key=brief["brief"][:60])
    return evals.get_dataset(c, project_id, ds["id"])


def _experiment(engine: Any, project_id: str, dataset_id: str, *, name: str, actor: str,
                target: Callable, engine_name: str) -> dict:
    from . import evals

    with engine.begin() as c:
        xp = evals.start_experiment(c, project_id, dataset_id, name=name, created_by=actor,
                                    source="bench", engine=engine_name)
        examples = evals.list_examples(c, project_id, dataset_id, status="confirmed")
    tokens = subagents = 0
    rows: list = []
    for example in examples:
        got = target(example)
        fails = evals.check_example(example, got["output"])
        if got.get("status") != "completed":
            fails.append(f"the run ended {got.get('status')}")
        tokens += int(got["tokens"])
        subagents += int(got.get("subagents") or 0)
        rows.append({"example": example["id"], "tokens": got["tokens"], "passed": not fails})
        with engine.begin() as c:
            evals.record_result(c, project_id, xp["id"], example["id"], passed=not fails,
                                failures=fails, output={**got["output"],
                                                        "tokens": got["tokens"]},
                                run_id=got["run_id"], cost_usd_micros=got["cost_usd_micros"],
                                latency_ms=got["latency_ms"])
    with engine.begin() as c:
        done = evals.finish_experiment(c, project_id, xp["id"], extra={
            "tokens": tokens, "subagents": subagents})
    summary = dict(done["summary"] if isinstance(done.get("summary"), dict) else {})
    return {"experiment_id": xp["id"], "total": summary.get("total"),
            "pass_rate": summary.get("pass_rate"), "tokens": tokens,
            "subagents_per_brief": (subagents // len(examples)) if examples else 0,
            "rows": rows}


def compare_research(engine: Any, project_id: str, *, actor: str,
                     engine_factory: Optional[Callable] = None,
                     engine_name: str = "scripted") -> dict:
    """Both experiments over the research dataset, and the token multiple. Every run (and every
    child) checkpoints in Postgres and records its spans and ledger (DbSink)."""
    from ..kernel.durable import store_for
    from ..kernel.ledger import DbSink

    store, sink = store_for(engine), DbSink(engine)
    with engine.begin() as c:
        ds = ensure_dataset(c, project_id, actor=actor)
    lead = _experiment(engine, project_id, ds["id"], name="lead + 3 parallel sub-agents",
                       actor=actor, engine_name=engine_name,
                       target=lambda ex: run_lead(ex, project_id=project_id,
                                                  engine_factory=engine_factory, store=store,
                                                  sink=sink))
    base = _experiment(engine, project_id, ds["id"], name="single agent baseline",
                       actor=actor, engine_name=engine_name,
                       target=lambda ex: run_single(ex, project_id=project_id,
                                                    engine_factory=engine_factory, store=store,
                                                    sink=sink))
    lead["parallel"] = True
    multiple = round(lead["tokens"] / base["tokens"], 3) if base["tokens"] else None
    return {"dataset_id": ds["id"], "lead": lead, "baseline": base, "token_multiple": multiple,
            "judge": {"status": "pending_real_engine" if engine_name == "scripted" else "ran",
                      "note": ("The rule judge (check_example) passed both; a quality "
                               "comparison needs a real engine and an LLM judge, which spend "
                               "money: pending the operator's approval.")
                      if engine_name == "scripted" else ""}}


__all__ = ["BRIEFS", "CORPUS", "compare_research", "ensure_dataset", "run_lead", "run_single",
           "scripted_brain"]
