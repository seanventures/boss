"""ONLINE EVALUATORS: a sampled share of an agent's live runs scored, within a weekly cap (AP-P4).

docs/design/AGENT_PLATFORM.md §4.9, D9. An agent's spec declares them (`spec.evaluators`, kind
`online`; agents/spec.py validates):

    evaluators:
      - {kind: online, name: finished, by: rule, rule: completed, sample: 1.0}
      - {kind: online, name: helpful, by: jev, sample: 0.1, weekly_usd_cap: 2,
         question: "Did the reply answer what was asked, with evidence?"}
      - {kind: online, name: judge, by: judge, sample: 0.05, weekly_usd_cap: 5,
         criteria: {...}, when: {evaluator: helpful, below: 0.5}}

  by: rule    deterministic over the run's trace (RULES), free — the default
  by: jev     the decision model (agent_decision.decide: Jev when the install has it, else the
              routed model), a governed call
  by: judge   an LLM judge — OPT-IN: refused by the spec validator without weekly_usd_cap > 0;
              its call is bounded by what is left of the cap (ReadPhase.max_usd_micros)

SAMPLING is deterministic per (evaluator, run): the same run is always in or out, so a re-fire
never double-scores and a test can count. CHAINING (`when`) runs an evaluator only when another
scored below a bar on this run.

THE WEEKLY CAP is `weekly_usd_cap`, per (agent, evaluator, ISO week starting Monday UTC), kept in
`online_evaluator_weeks`. A call is handed what is left of the cap as its bound
(min(the evaluator's per-call bound, cap - spent); agent_decision's ReadPhase.max_usd_micros), its
real cost is added after it, and reaching the cap PAUSES the evaluator for the rest of the week
(`paused_at`, an `online_evaluator.paused` event); every later sampled run is counted in
`skipped_at_cap` and costs nothing. A runner that honours its bound therefore never takes the
week past the cap (the only overshoot possible is a provider billing past the bound it was
given). scoped_budgets has no weekly period, so the cap is kept here; the money itself is still
each call's llm_calls row.

Every verdict is a `feedback` row (source rule / decision_model / judge, the evaluator's name as
key, its cost), which also extends the run's payload retention (trace_payloads, 180 days).
Invoked when a run ends (services/traces.DbSpanSink.run_finished); never raises into a run.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional

from sqlalchemy import text

log = logging.getLogger(__name__)

VERSION = "online_evaluators@1"
#: The most one decision-model evaluation may cost (its read phase is bounded by it).
JEV_BOUND_MICROS = 20_000
#: The most one judge call may cost, before the cap's remainder bounds it further.
JUDGE_BOUND_MICROS = 100_000


# ── Rules (deterministic, free) over a run's trace (services/traces.run_trace) ────────────────

def _completed(trace: Mapping[str, Any], spec: Mapping[str, Any]) -> tuple:
    ok = trace.get("status") == "ok"
    return (1.0 if ok else 0.0), f"the run ended {trace.get('status')}"


def _no_failed_tools(trace: Mapping[str, Any], spec: Mapping[str, Any]) -> tuple:
    bad = [s["name"] for s in trace.get("spans") or []
           if s.get("kind") in ("tool", "subagent", "handoff")
           and s.get("status") in ("error", "refused")]
    return (0.0 if bad else 1.0), (f"failed tools: {', '.join(bad)}" if bad
                                    else "no tool call failed")


def _no_refusals(trace: Mapping[str, Any], spec: Mapping[str, Any]) -> tuple:
    root = (trace.get("spans") or [{}])[0]
    stopped = str((root.get("attributes") or {}).get("stopped") or "")
    return (0.0 if stopped == "refused" else 1.0), (f"stopped: {stopped}" if stopped
                                                     else "accepted")


def _under_budget(trace: Mapping[str, Any], spec: Mapping[str, Any]) -> tuple:
    limits = (spec.get("spec") or {}).get("limits") or {}
    cap = limits.get("run_usd")
    spent = int((trace.get("totals") or {}).get("cost_usd_micros") or 0)
    if not cap:
        return 1.0, "no run_usd limit"
    ok = spent <= float(cap) * 1e6
    return (1.0 if ok else 0.0), f"spent ${spent / 1e6:.4f} of ${float(cap):.2f}"


def _cited_evidence(trace: Mapping[str, Any], spec: Mapping[str, Any]) -> tuple:
    tools = (trace.get("totals") or {}).get("tool_calls") or 0
    return (1.0 if tools else 0.0), (f"{tools} tool calls behind the answer" if tools
                                     else "answered without reading anything")


RULES: dict = {"completed": _completed, "no_failed_tools": _no_failed_tools,
               "no_refusals": _no_refusals, "under_budget": _under_budget,
               "cited_evidence": _cited_evidence}


# ── The evaluator ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OnlineEvaluator:
    name: str
    by: str = "rule"
    rule: Optional[str] = None
    question: str = ""
    criteria: Mapping = field(default_factory=dict)
    sample: float = 1.0
    cap_micros: Optional[int] = None
    when: Optional[Mapping] = None

    @property
    def source(self) -> str:
        return {"rule": "rule", "jev": "decision_model", "judge": "judge"}[self.by]

    def bound(self) -> int:
        return {"rule": 0, "jev": JEV_BOUND_MICROS, "judge": JUDGE_BOUND_MICROS}[self.by]


def parse(doc: Mapping[str, Any]) -> list:
    """The online evaluators of a spec document, chained ones after the one they wait on."""
    items = ((doc or {}).get("spec") or {}).get("evaluators") or []
    out = []
    for ev in items:
        if not isinstance(ev, Mapping) or ev.get("kind") != "online" or not ev.get("name"):
            continue
        cap = ev.get("weekly_usd_cap")
        out.append(OnlineEvaluator(
            name=str(ev["name"]), by=str(ev.get("by") or "rule"), rule=ev.get("rule"),
            question=str(ev.get("question") or ""), criteria=dict(ev.get("criteria") or {}),
            sample=float(ev["sample"]) if ev.get("sample") is not None else 1.0,
            cap_micros=int(round(float(cap) * 1e6)) if cap is not None else None,
            when=dict(ev["when"]) if isinstance(ev.get("when"), Mapping) else None))
    first = [e for e in out if not e.when]
    return first + [e for e in out if e.when]


def sampled(run_id: str, name: str, rate: float) -> bool:
    """Deterministic: the same (evaluator, run) is always in or always out."""
    if rate >= 1:
        return True
    if rate <= 0:
        return False
    digest = hashlib.sha256(f"{name}:{run_id}".encode()).hexdigest()
    return int(digest[:8], 16) / 0x1_0000_0000 < rate


def week_start(now: datetime) -> date:
    day = now.astimezone(timezone.utc).date()
    return day - timedelta(days=day.weekday())


# ── Model-backed evaluators (injectable: tests and the battle runner pass their own) ──────────

Runner = Callable[..., dict]


def _summary(trace: Mapping[str, Any]) -> str:
    root = (trace.get("spans") or [{}])[0]
    steps = [f"- {s['kind']} {s['name']} → {s['status']}"
             for s in (trace.get("spans") or [])[1:40] if s.get("kind") != "step"]
    return (f"Run {trace.get('run_id')} by {trace.get('agent_key')}: {trace.get('status')}.\n"
            + "\n".join(steps) + f"\nFinal: {json.dumps((root.get('attributes') or {}), default=str)[:1500]}")


def _jev(c: Any, *, project_id: str, evaluator: OnlineEvaluator, trace: Mapping[str, Any],
         max_usd_micros: int, settings: Any = None) -> dict:
    from .agent_decision import Candidate, ReadPhase, decide

    if settings is None:
        from ..config import get_settings

        settings = get_settings()
    got = decide(c, settings, project_id,
                 situation=f"Score a finished run of the agent {trace.get('agent_key')}: "
                           f"{evaluator.question or evaluator.name}",
                 message=_summary(trace),
                 candidates=[Candidate("pass", "the run did what was asked", acts=False),
                             Candidate("fail", "the run fell short", acts=False)],
                 agent="online evaluator", ref=f"eval:{evaluator.name}:{trace.get('run_id')}",
                 read=ReadPhase(max_turns=1, max_usd_micros=int(max_usd_micros)))
    score = {"pass": 1.0, "fail": 0.0}.get(str(got.action or ""))
    return {"score": score, "comment": got.reason or got.error,
            "cost_usd_micros": int(got.cost_usd_micros or 0),
            "value": {"status": got.status, "by": got.by}}


def _judge(c: Any, *, project_id: str, evaluator: OnlineEvaluator, trace: Mapping[str, Any],
           max_usd_micros: int, settings: Any = None) -> dict:
    """The opt-in LLM judge: the routed chat model (a structured decision), bounded by what is
    left of the week's cap."""
    from .agent_decision import Candidate, ReadPhase, decide

    if settings is None:
        from ..config import get_settings

        settings = get_settings()
    criteria = json.dumps(dict(evaluator.criteria), default=str)[:3000]
    got = decide(c, settings, project_id,
                 situation=f"Judge a finished run of the agent {trace.get('agent_key')} "
                           f"against these criteria: {criteria}",
                 message=_summary(trace),
                 candidates=[Candidate("pass", "it meets the criteria", acts=False),
                             Candidate("fail", "it does not", acts=False)],
                 agent="judge", ref=f"judge:{evaluator.name}:{trace.get('run_id')}",
                 check=lambda answer, corpus: "",
                 read=ReadPhase(max_turns=1, max_usd_micros=int(max_usd_micros)))
    score = {"pass": 1.0, "fail": 0.0}.get(str(got.action or ""))
    return {"score": score, "comment": got.reason or got.error,
            "cost_usd_micros": int(got.cost_usd_micros or 0),
            "value": {"status": got.status, "by": got.by}}


RUNNERS: dict = {"jev": _jev, "judge": _judge}


# ── The week ───────────────────────────────────────────────────────────────────────────────────

def _week(c: Any, project_id: str, agent_key: str, ev: OnlineEvaluator, week: date) -> dict:
    c.execute(text("""
        INSERT INTO online_evaluator_weeks (project_id, agent_key, evaluator, week_start,
            cap_usd_micros)
        VALUES (:p, :a, :e, :w, :cap) ON CONFLICT DO NOTHING
    """), {"p": project_id, "a": agent_key, "e": ev.name, "w": week, "cap": ev.cap_micros})
    return dict(c.execute(text("""
        SELECT * FROM online_evaluator_weeks WHERE project_id = :p AND agent_key = :a
           AND evaluator = :e AND week_start = :w FOR UPDATE
    """), {"p": project_id, "a": agent_key, "e": ev.name, "w": week}).mappings().first())


def _bump(c: Any, project_id: str, agent_key: str, ev: OnlineEvaluator, week: date,
          **changes: Any) -> None:
    sets = ", ".join(f"{k} = {k} + :{k}" if k != "paused_at" else "paused_at = :paused_at"
                     for k in changes)
    c.execute(text(f"UPDATE online_evaluator_weeks SET {sets}, cap_usd_micros = :cap, "
                   "updated_at = now() WHERE project_id = :p AND agent_key = :a "
                   "AND evaluator = :e AND week_start = :w"),
              {**changes, "p": project_id, "a": agent_key, "e": ev.name, "w": week,
               "cap": ev.cap_micros})


def evaluate_run(engine: Any, project_id: str, run_id: str, *, agent_key: Optional[str] = None,
                 spec_doc: Optional[Mapping] = None, now: Optional[datetime] = None,
                 runners: Optional[Mapping[str, Runner]] = None) -> list:
    """Score one finished run with its agent's online evaluators. Returns one entry per
    evaluator: {evaluator, outcome: not_sampled|skipped_chain|paused|evaluated, score?, cost?}."""
    from ..agents import versions
    from .evals import record_feedback
    from .traces import run_trace

    when = now or datetime.now(timezone.utc)
    week = week_start(when)
    with engine.begin() as c:
        if agent_key is None:
            agent_key = c.execute(text(
                "SELECT agent_key FROM trace_spans WHERE run_id = :r AND kind = 'run' "
                "AND agent_key IS NOT NULL LIMIT 1"), {"r": run_id}).scalar()
        if not agent_key:
            return []
        version = None
        if spec_doc is None:
            version = versions.active(c, project_id, agent_key)
            spec_doc = (version or {}).get("spec") or {}
        evaluators = parse(spec_doc)
        if not evaluators:
            return []
        trace = run_trace(c, project_id, run_id, depth=1) or {"run_id": run_id, "spans": [],
                                                                 "totals": {}}
    spec_version = (version or {}).get("version") or trace.get("spec_version")
    scores: dict = {}
    out: list = []
    for ev in evaluators:
        result = {"evaluator": ev.name, "by": ev.by}
        with engine.begin() as c:
            row = _week(c, project_id, agent_key, ev, week)
            if not sampled(run_id, ev.name, ev.sample):
                _bump(c, project_id, agent_key, ev, week, seen=1)
                out.append({**result, "outcome": "not_sampled"})
                continue
            if ev.when:
                parent = scores.get(str(ev.when.get("evaluator")))
                if parent is None or parent >= float(ev.when.get("below") or 0):
                    _bump(c, project_id, agent_key, ev, week, seen=1, sampled=1)
                    out.append({**result, "outcome": "skipped_chain"})
                    continue
            spent = int(row["spent_usd_micros"] or 0)
            bound = ev.bound()
            if ev.cap_micros is not None:
                left = ev.cap_micros - spent
                if row["paused_at"] is not None or left <= 0:
                    _bump(c, project_id, agent_key, ev, week, seen=1, sampled=1,
                          skipped_at_cap=1,
                          **({} if row["paused_at"] else {"paused_at": when}))
                    if row["paused_at"] is None:
                        from .core import emit_event

                        emit_event(c, project_id, "online_evaluator.paused", "boss",
                                   "agent", agent_key,
                                   {"evaluator": ev.name, "week_start": week.isoformat(),
                                    "spent_usd_micros": spent, "cap_usd_micros": ev.cap_micros})
                    out.append({**result, "outcome": "paused"})
                    continue
                bound = min(bound, left) if bound else 0
        try:
            if ev.by == "rule":
                fn = RULES.get(str(ev.rule))
                if fn is None:
                    raise ValueError(f"no rule {ev.rule!r}")
                score, comment = fn(trace, spec_doc)
                verdict = {"score": score, "comment": comment, "cost_usd_micros": 0}
            else:
                runner = (runners or RUNNERS).get(ev.by)
                with engine.begin() as c:
                    verdict = runner(c, project_id=project_id, evaluator=ev, trace=trace,
                                     max_usd_micros=bound)
        except Exception as exc:  # noqa: BLE001 — an evaluator's failure is recorded, not raised
            verdict = {"score": None, "comment": f"evaluator failed: {type(exc).__name__}: "
                                                 f"{exc}"[:500], "cost_usd_micros": 0}
        cost = int(verdict.get("cost_usd_micros") or 0)
        with engine.begin() as c:
            row = _week(c, project_id, agent_key, ev, week)
            reached = ev.cap_micros is not None and int(row["spent_usd_micros"] or 0) + cost \
                >= ev.cap_micros
            _bump(c, project_id, agent_key, ev, week, seen=1, sampled=1, evaluated=1,
                  spent_usd_micros=cost,
                  **({"paused_at": when} if reached and row["paused_at"] is None else {}))
            record_feedback(c, project_id, key=ev.name, source=ev.source, created_by="boss",
                            run_id=run_id, score=verdict.get("score"),
                            value=verdict.get("value"), comment=verdict.get("comment"),
                            evaluator=ev.name, evaluator_version=f"{VERSION}:{ev.by}",
                            agent_key=agent_key, spec_version=spec_version,
                            cost_usd_micros=cost)
        if verdict.get("score") is not None:
            scores[ev.name] = float(verdict["score"])
        out.append({**result, "outcome": "evaluated", "score": verdict.get("score"),
                    "cost_usd_micros": cost})
    return out


def on_run_end(engine: Any, project_id: str, run_id: str, *, agent_key: str) -> list:
    """The trace sink's hook: evaluate when the agent has online evaluators. Never raises."""
    try:
        return evaluate_run(engine, project_id, run_id, agent_key=agent_key)
    except Exception as exc:  # noqa: BLE001
        log.warning("online evaluators: run %s not evaluated (non-fatal): %s", run_id, exc)
        return []


def weeks(c: Any, project_id: str, *, agent_key: Optional[str] = None,
          now: Optional[datetime] = None) -> list:
    """This week's state of every online evaluator (console A5: paused at its cap or not)."""
    week = week_start(now or datetime.now(timezone.utc))
    rows = c.execute(text("""
        SELECT * FROM online_evaluator_weeks WHERE project_id = :p AND week_start = :w
           AND (CAST(:a AS text) IS NULL OR agent_key = :a) ORDER BY agent_key, evaluator
    """), {"p": project_id, "w": week, "a": agent_key}).mappings().all()
    out = []
    for row in rows:
        item = dict(row)
        item["week_start"] = item["week_start"].isoformat()
        for k in ("paused_at", "updated_at"):
            item[k] = item[k].isoformat() if item.get(k) else None
        item["paused"] = item["paused_at"] is not None
        out.append(item)
    return out


__all__ = ["JUDGE_BOUND_MICROS", "JEV_BOUND_MICROS", "OnlineEvaluator", "RULES", "RUNNERS",
           "evaluate_run", "on_run_end", "parse", "sampled", "week_start", "weeks"]
