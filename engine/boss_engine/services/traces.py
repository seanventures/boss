"""RUN TRACES: the spans the kernel writes, stored and read back as a tree (AP-P4, BOSSAPP-1055).

docs/design/AGENT_PLATFORM.md §4.8. The write side is `DbSpanSink`, which kernel/trace.py's
mandatory `trace` middleware uses whenever a run has a database: one short transaction per span
write (never the caller's: an agent run's tick is one long transaction, and a trace must not
vanish when it rolls back), the payloads redacted and stored by services/trace_payloads.py, and
the finished span exported over OTLP when BOSS's own tracing is on (self_telemetry; off by
default, D8).

The read side answers the operator's two questions:

  run_trace(run_id)         the run as a tree: every span with its depth, its place on the
                            run's timeline (offset/width %, the waterfall), its cost and tokens,
                            and the child runs (subagents: spans parented into this run, and
                            runs under this run's child agent_acts), to `depth` levels.
  run_for_decision(id)      the run behind a Decision, whichever path raised it (a held tool
                            call, a code task's merge gate, an agent run waiting on approval,
                            an act that recorded it). Decision → run → tree: two hops.

COST COMES FROM llm_calls, ALWAYS. A model span names its llm_calls row by (run_id, seq). Rows of
the run that no span names (a CLI delegate's calls, a path not yet on the kernel, a model call
recorded before its span) are shown as ledger-only model spans under the run, so the costs of a
run's spans sum to its llm_calls total — `totals.reconciled` says so, and the tests hold it.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import text

log = logging.getLogger(__name__)

#: Rows of llm_calls a run reads: the ledger's run id, or a code task run's legacy column.
_CALLS_SQL = """
    SELECT id, seq, model, stage, engine, cost_usd_micros, prompt_tokens, completion_tokens,
           cache_read_tokens, cache_write_tokens, latency_ms, ok, error, created_at,
           included_in_subscription, act_id
      FROM llm_calls
     WHERE project_id = :p AND (run_id = :r OR (run_id IS NULL AND code_task_run_id = :r))
     ORDER BY created_at, seq NULLS LAST, id
"""


# ── Write side ─────────────────────────────────────────────────────────────────────────────────

def _ts(seconds: Optional[float]) -> Optional[datetime]:
    return None if seconds is None else datetime.fromtimestamp(float(seconds), tz=timezone.utc)


class DbSpanSink:
    """kernel/trace.py's sink on a database. Never raises (the middleware logs, too)."""

    _CACHE_MAX = 1024

    def __init__(self, engine: Any, *, backend: Any = None):
        self.engine = engine
        self._backend = backend
        self._backend_known = backend is not None
        self._runs: dict = {}

    def backend(self) -> Any:
        if not self._backend_known:
            from .trace_payloads import backend_from_settings

            self._backend = backend_from_settings()
            self._backend_known = True
        return self._backend

    def _run_facts(self, c: Any, span: Any) -> dict:
        """The agent version that ran and the root act, once per run."""
        facts = self._runs.get(span.run_id)
        if facts is not None:
            return facts
        facts = {"spec_version": None, "root_act_id": None}
        if span.agent_key:
            facts["spec_version"] = c.execute(text(
                "SELECT version FROM agent_spec_versions WHERE project_id = :p "
                "AND kind = 'agent' AND agent_key = :k AND active"),
                {"p": span.project_id, "k": span.agent_key}).scalar()
        if span.act_id:
            facts["root_act_id"] = c.execute(text(
                "SELECT root_act_id FROM agent_acts WHERE act_id = :a"),
                {"a": span.act_id}).scalar()
        if len(self._runs) >= self._CACHE_MAX:
            self._runs.clear()
        self._runs[span.run_id] = facts
        return facts

    def write_span(self, span: Any) -> None:
        from . import trace_payloads

        with self.engine.begin() as c:
            facts = self._run_facts(c, span)
            refs = {}
            for direction in ("input", "output"):
                value = getattr(span, direction)
                if value is None:
                    refs[direction] = None
                    continue
                refs[direction] = trace_payloads.store(
                    c, project_id=span.project_id, run_id=span.run_id, span_id=span.span_id,
                    direction=direction, value=value, backend=self.backend())
            c.execute(text("""
                INSERT INTO trace_spans (span_id, trace_id, parent_span_id, project_id, run_id,
                    act_id, root_act_id, kind, name, status, step, seq, child_run_id,
                    decision_id, agent_key, spec_version, attributes, input_ref, output_ref,
                    started_at, ended_at, duration_ms)
                VALUES (:span_id, :trace_id, :parent, :p, :r, :act, :root, :kind, :name,
                    :status, :step, :seq, :child, :decision, :agent, :version,
                    CAST(:attrs AS jsonb), :inp, :outp, :started, :ended, :ms)
                ON CONFLICT (span_id) DO UPDATE SET
                    status = EXCLUDED.status, ended_at = EXCLUDED.ended_at,
                    duration_ms = EXCLUDED.duration_ms, step = EXCLUDED.step,
                    seq = COALESCE(EXCLUDED.seq, trace_spans.seq),
                    child_run_id = COALESCE(EXCLUDED.child_run_id, trace_spans.child_run_id),
                    decision_id = COALESCE(EXCLUDED.decision_id, trace_spans.decision_id),
                    attributes = trace_spans.attributes || EXCLUDED.attributes,
                    input_ref = COALESCE(EXCLUDED.input_ref, trace_spans.input_ref),
                    output_ref = COALESCE(EXCLUDED.output_ref, trace_spans.output_ref)
            """), {"span_id": span.span_id, "trace_id": span.trace_id,
                   "parent": span.parent_span_id, "p": span.project_id, "r": span.run_id,
                   "act": span.act_id, "root": facts["root_act_id"], "kind": span.kind,
                   "name": str(span.name)[:300], "status": span.status, "step": int(span.step),
                   "seq": span.seq, "child": span.child_run_id, "decision": span.decision_id,
                   "agent": span.agent_key, "version": facts["spec_version"],
                   "attrs": json.dumps(_clean_attributes(span.attributes), default=str),
                   "inp": refs["input"], "outp": refs["output"],
                   "started": _ts(span.started_at), "ended": _ts(span.ended_at),
                   "ms": int(span.duration_ms)})
        if span.ended_at is not None:
            try:
                from .self_telemetry import export_trace_span

                export_trace_span(span)
            except Exception:  # noqa: BLE001 — export is optional and never fails the run
                log.debug("trace: OTel export skipped", exc_info=True)
        if span.kind == "run" and span.ended_at is not None and span.status in (
                "ok", "error", "cancelled", "refused"):
            self.run_finished(span)

    def run_finished(self, span: Any) -> None:
        """A run ended: count it (boss_agent_run_*), and score it with the agent's online
        evaluators (services/online_evaluators: sampled, capped, never raising)."""
        try:
            from .self_telemetry import metrics_count_agent_run

            metrics_count_agent_run(span.agent_key, span.status, span.duration_ms)
        except Exception:  # noqa: BLE001
            log.debug("trace: run metrics skipped", exc_info=True)
        if not span.agent_key:
            return
        try:
            from .online_evaluators import on_run_end

            on_run_end(self.engine, span.project_id, span.run_id, agent_key=span.agent_key)
        except Exception as exc:  # noqa: BLE001 — an evaluator never fails the run it scores
            log.warning("trace: online evaluators skipped for %s (non-fatal): %s",
                        span.run_id, exc)


def _clean_attributes(attributes: Mapping[str, Any]) -> dict:
    """Attributes are metadata (names, counts, statuses), but a caller's error text can carry a
    secret: the same redaction as the ledger's events."""
    from ..kernel.ledger import redact_data

    return redact_data(dict(attributes or {}))


# ── Read side ──────────────────────────────────────────────────────────────────────────────────

def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else value


def _spans(c: Any, project_id: str, run_id: str) -> list:
    return [dict(r) for r in c.execute(text("""
        SELECT span_id, trace_id, parent_span_id, run_id, act_id, root_act_id, kind, name,
               status, step, seq, child_run_id, decision_id, agent_key, spec_version, attributes,
               input_ref, output_ref, started_at, ended_at, duration_ms
          FROM trace_spans WHERE project_id = :p AND run_id = :r
         ORDER BY started_at, span_id
    """), {"p": project_id, "r": run_id}).mappings()]


def _usage(call: Mapping[str, Any]) -> dict:
    return {"input_tokens": int(call.get("prompt_tokens") or 0),
            "output_tokens": int(call.get("completion_tokens") or 0),
            "cache_read_tokens": int(call.get("cache_read_tokens") or 0),
            "cache_write_tokens": int(call.get("cache_write_tokens") or 0)}


def _run_row(c: Any, project_id: str, run_id: str) -> Optional[dict]:
    """What BOSS knows of a run that has no span of its own (a code task, an agent run)."""
    for table, cols in (("code_task_runs", "title AS name, status, created_at, updated_at"),
                        ("agent_runs", "'agent run' AS name, status, created_at, updated_at")):
        try:
            with c.begin_nested():
                row = c.execute(text(f"SELECT id, {cols} FROM {table} "
                                     "WHERE id = :r AND project_id = :p"),
                                {"r": run_id, "p": project_id}).mappings().first()
        except Exception:  # noqa: BLE001 — a store without the table
            row = None
        if row is not None:
            return dict(row, table=table)
    return None


def _child_runs(c: Any, project_id: str, run_id: str, spans: list) -> list:
    """Runs started under this one: spans parented into this run's spans (a subagent run in
    process, or one started with this run's traceparent), runs a subagent/handoff span names,
    and runs under this run's acts' child acts (agent_acts.parent_act_id)."""
    ids = [s["span_id"] for s in spans]
    found: list = []
    if ids:
        found += [r[0] for r in c.execute(text(
            "SELECT DISTINCT run_id FROM trace_spans WHERE project_id = :p AND run_id <> :r "
            "AND parent_span_id = ANY(:ids)"), {"p": project_id, "r": run_id, "ids": ids})]
    found += [s["child_run_id"] for s in spans if s.get("child_run_id")]
    acts = sorted({s["act_id"] for s in spans if s.get("act_id")})
    if acts:
        children = [r[0] for r in c.execute(text(
            "SELECT act_id FROM agent_acts WHERE parent_act_id = ANY(:a)"), {"a": acts})]
        if children:
            found += [r[0] for r in c.execute(text(
                "SELECT DISTINCT run_id FROM trace_spans WHERE project_id = :p "
                "AND act_id = ANY(:a) AND run_id <> :r"),
                {"p": project_id, "a": children, "r": run_id})]
            found += [r[0] for r in c.execute(text(
                "SELECT DISTINCT run_id FROM llm_calls WHERE project_id = :p "
                "AND act_id = ANY(:a) AND run_id IS NOT NULL AND run_id <> :r"),
                {"p": project_id, "a": children, "r": run_id})]
    out, seen = [], {run_id}
    for child in found:
        if child and child not in seen:
            seen.add(child)
            out.append(child)
    return out


def run_trace(c: Any, project_id: str, run_id: str, *, depth: int = 2) -> Optional[dict]:
    """The run as a tree (see the module docstring), or None when BOSS has no trace of it."""
    spans = _spans(c, project_id, run_id)
    calls = [dict(r) for r in c.execute(text(_CALLS_SQL), {"p": project_id, "r": run_id})
             .mappings()]
    run_row = None if any(s["kind"] == "run" for s in spans) else _run_row(c, project_id, run_id)
    if not spans and not calls and run_row is None:
        return None
    by_seq = {int(call["seq"]): call for call in calls if call.get("seq") is not None}
    named: set = set()
    root = next((s for s in spans if s["kind"] == "run"), None)
    if root is None:
        first = min([s["started_at"] for s in spans] + [k["created_at"] for k in calls]
                    + ([run_row["created_at"]] if run_row else []))
        last = max([s.get("ended_at") or s["started_at"] for s in spans]
                   + [k["created_at"] for k in calls]
                   + ([run_row["updated_at"]] if run_row else []))
        root = {"span_id": f"run:{run_id}", "trace_id": None, "parent_span_id": None,
                "run_id": run_id, "act_id": None, "root_act_id": None, "kind": "run",
                "name": (run_row or {}).get("name") or run_id,
                "status": {"completed": "ok", "failed": "error", "running": "running"}.get(
                    str((run_row or {}).get("status") or ""), "ok"),
                "step": 0, "seq": None, "child_run_id": None, "decision_id": None,
                "agent_key": None, "spec_version": None,
                "attributes": {"from_ledger": True, "table": (run_row or {}).get("table")},
                "input_ref": None, "output_ref": None, "started_at": first, "ended_at": last,
                "duration_ms": int((last - first).total_seconds() * 1000)}
        spans = [root] + spans
    out_spans: list = []
    for span in spans:
        call = by_seq.get(int(span["seq"])) if span["kind"] == "model" and span.get("seq") \
            is not None else None
        if call is not None:
            named.add(call["id"])
        out_spans.append({**span, "llm_call_id": call["id"] if call else None,
                          "cost_usd_micros": int(call["cost_usd_micros"] or 0) if call else 0,
                          "included_in_subscription": bool(call["included_in_subscription"])
                          if call else False,
                          **({"usage": _usage(call), "model": call["model"]} if call else {})})
    for call in calls:
        if call["id"] in named:
            continue
        at = call["created_at"]
        started = at - timedelta(milliseconds=int(call.get("latency_ms") or 0))
        out_spans.append({
            "span_id": f"llm:{call['id']}", "trace_id": root.get("trace_id"),
            "parent_span_id": root["span_id"], "run_id": run_id, "act_id": call.get("act_id"),
            "root_act_id": None, "kind": "model",
            "name": f"model {call.get('model') or call.get('engine') or 'call'}",
            "status": "ok" if call.get("ok") else "error", "step": 0, "seq": call.get("seq"),
            "child_run_id": None, "decision_id": None, "agent_key": None, "spec_version": None,
            "attributes": {"from_ledger": True, "stage": call.get("stage"),
                           "error": call.get("error")},
            "input_ref": None, "output_ref": None, "started_at": started, "ended_at": at,
            "duration_ms": int(call.get("latency_ms") or 0), "llm_call_id": call["id"],
            "cost_usd_micros": int(call["cost_usd_micros"] or 0),
            "included_in_subscription": bool(call["included_in_subscription"]),
            "usage": _usage(call), "model": call.get("model")})
    ordered = _tree_order(out_spans, root["span_id"])
    start = root["started_at"]
    end = max([s.get("ended_at") or s["started_at"] for s in ordered] + [start])
    total_ms = max(1, int((end - start).total_seconds() * 1000))
    for span in ordered:
        offset = int((span["started_at"] - start).total_seconds() * 1000)
        width = int(span.get("duration_ms") or 0)
        span["offset_pct"] = round(max(0.0, min(100.0, offset * 100.0 / total_ms)), 2)
        span["width_pct"] = round(max(0.0, min(100.0 - span["offset_pct"],
                                               width * 100.0 / total_ms)), 2)
        span["started_at"], span["ended_at"] = _iso(span["started_at"]), _iso(span["ended_at"])
    ledger_total = sum(int(k["cost_usd_micros"] or 0) for k in calls)
    span_total = sum(int(s["cost_usd_micros"]) for s in ordered)
    children = []
    if depth > 1:
        for child in _child_runs(c, project_id, run_id, spans):
            got = run_trace(c, project_id, child, depth=depth - 1)
            if got is not None:
                children.append(got)
    kinds = [s["kind"] for s in ordered]
    trace_id = root.get("trace_id")
    return {
        "run_id": run_id, "project_id": project_id, "trace_id": trace_id,
        "traceparent": f"00-{trace_id}-{root['span_id']}-01" if trace_id else None,
        "name": root["name"], "status": root["status"], "agent_key": root.get("agent_key"),
        "spec_version": root.get("spec_version"), "act_id": root.get("act_id"),
        "root_act_id": root.get("root_act_id"),
        "actor": (root.get("attributes") or {}).get("actor"),
        "started_at": _iso(start), "ended_at": _iso(end), "duration_ms": total_ms,
        "totals": {
            "cost_usd_micros": span_total, "llm_calls_cost_usd_micros": ledger_total,
            "reconciled": span_total == ledger_total, "llm_calls": len(calls),
            "model_calls": kinds.count("model"), "tool_calls": kinds.count("tool"),
            "subagents": kinds.count("subagent"), "handoffs": kinds.count("handoff"),
            "interrupts": kinds.count("interrupt"), "steps": kinds.count("step"),
            "input_tokens": sum(int(k.get("prompt_tokens") or 0) for k in calls),
            "output_tokens": sum(int(k.get("completion_tokens") or 0) for k in calls),
            "cache_read_tokens": sum(int(k.get("cache_read_tokens") or 0) for k in calls),
            "with_children_cost_usd_micros": span_total + sum(
                ch["totals"]["with_children_cost_usd_micros"] for ch in children)},
        "spans": ordered,
        "children": children,
    }


def _tree_order(spans: list, root_id: str) -> list:
    """Depth-first, children by start time; every span gets `depth`. A span whose parent is
    not in this run (a child run's root, an orphan) hangs from the root."""
    ids = {s["span_id"] for s in spans}
    kids: dict = {}
    for span in spans:
        if span["span_id"] == root_id:
            continue
        parent = span.get("parent_span_id")
        kids.setdefault(parent if parent in ids else root_id, []).append(span)
    for group in kids.values():
        group.sort(key=lambda s: (s["started_at"], s["span_id"]))
    out: list = []
    root = next(s for s in spans if s["span_id"] == root_id)

    def walk(span: dict, depth: int) -> None:
        span["depth"] = depth
        out.append(span)
        for kid in kids.get(span["span_id"], []):
            walk(kid, depth + 1)

    walk(root, 0)
    return out


def span_detail(c: Any, project_id: str, span_id: str, *, backend: Any = None) -> Optional[dict]:
    """One span with its redacted input and output (the "selected step" panel)."""
    from . import trace_payloads

    row = c.execute(text("SELECT * FROM trace_spans WHERE project_id = :p AND span_id = :s"),
                    {"p": project_id, "s": span_id}).mappings().first()
    if row is None:
        return None
    span = {k: _iso(v) for k, v in dict(row).items()}
    if row["kind"] == "model" and row["seq"] is not None:
        call = c.execute(text(
            "SELECT id, model, cost_usd_micros, prompt_tokens, completion_tokens, "
            "cache_read_tokens, cache_write_tokens, latency_ms FROM llm_calls "
            "WHERE project_id = :p AND run_id = :r AND seq = :q"),
            {"p": project_id, "r": row["run_id"], "q": row["seq"]}).mappings().first()
        if call is not None:
            span.update(llm_call_id=call["id"], model=call["model"],
                        cost_usd_micros=int(call["cost_usd_micros"] or 0), usage=_usage(call))
    for direction in ("input", "output"):
        ref = row[f"{direction}_ref"]
        span[direction] = trace_payloads.load(c, ref, backend=backend) if ref else None
    return span


# ── Decision → run ─────────────────────────────────────────────────────────────────────────────

def run_for_decision(c: Any, project_id: str, decision_id: str) -> Optional[dict]:
    """{run_id, found_by} for the run behind a Decision, or None. Tried in order: the
    interrupt span that waited on it, the held tool call's audit row, an agent run parked on
    it, a code task whose merge gate it is, a meeting that raised it, an act that records it."""
    probes = (
        ("interrupt_span", "SELECT run_id FROM trace_spans WHERE project_id = :p "
                           "AND decision_id = :d ORDER BY started_at LIMIT 1"),
        ("tool_call_audit", "SELECT run_id FROM tool_call_audit WHERE project_id = :p "
                            "AND decision_id = :d AND run_id IS NOT NULL "
                            "ORDER BY recorded_at LIMIT 1"),
        ("agent_run", "SELECT id FROM agent_runs WHERE project_id = :p "
                      "AND pending ->> 'decision_id' = :d LIMIT 1"),
        ("code_task_run", "SELECT id FROM code_task_runs WHERE project_id = :p "
                          "AND decision_id = :d LIMIT 1"),
        ("meeting_run", "SELECT id FROM meeting_runs WHERE project_id = :p "
                        "AND decision_id = :d LIMIT 1"),
        ("agent_act", "SELECT COALESCE(code_task_run_id, "
                      "(SELECT run_id FROM trace_spans s WHERE s.act_id = a.act_id LIMIT 1), "
                      "(SELECT run_id FROM llm_calls l WHERE l.act_id = a.act_id "
                      "AND l.run_id IS NOT NULL LIMIT 1)) "
                      "FROM agent_acts a WHERE project_id = :p AND decision_id = :d LIMIT 1"),
    )
    for how, sql in probes:
        try:
            with c.begin_nested():
                found = c.execute(text(sql), {"p": project_id, "d": decision_id}).scalar()
        except Exception:  # noqa: BLE001 — a store without that table
            found = None
        if found:
            return {"run_id": str(found), "found_by": how}
    return None


# ── Health (console A5) ────────────────────────────────────────────────────────────────────────

def agent_health(c: Any, project_id: str, *, hours: int = 24) -> list:
    """Per agent, from its run spans in the window: runs, ok rate, failures, p95 duration, cost
    (llm_calls of those runs), the last run, the last failed run (to open its trace), and the
    version that ran last."""
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours)))
    rows = c.execute(text("""
        WITH runs AS (
          SELECT agent_key, run_id, status, duration_ms, started_at, spec_version
            FROM trace_spans
           WHERE project_id = :p AND kind = 'run' AND agent_key IS NOT NULL
             AND started_at >= :since)
        SELECT r.agent_key,
               count(*) AS runs,
               count(*) FILTER (WHERE r.status = 'ok') AS ok,
               count(*) FILTER (WHERE r.status = 'error') AS failed,
               count(*) FILTER (WHERE r.status IN ('held', 'running')) AS open,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY r.duration_ms) AS p95_ms,
               max(r.started_at) AS last_run_at,
               (array_agg(r.run_id ORDER BY r.started_at DESC)
                  FILTER (WHERE r.status = 'error'))[1] AS last_failed_run_id,
               (array_agg(r.spec_version ORDER BY r.started_at DESC))[1] AS spec_version,
               COALESCE((SELECT sum(l.cost_usd_micros) FROM llm_calls l
                          WHERE l.project_id = :p AND l.run_id IN (
                            SELECT run_id FROM runs x WHERE x.agent_key = r.agent_key)), 0)
                 AS cost_usd_micros
          FROM runs r GROUP BY r.agent_key ORDER BY r.agent_key
    """), {"p": project_id, "since": since}).mappings().all()
    out = []
    for row in rows:
        finished = int(row["ok"]) + int(row["failed"])
        out.append({"agent_key": row["agent_key"], "spec_version": row["spec_version"],
                    "runs": int(row["runs"]), "ok": int(row["ok"]), "failed": int(row["failed"]),
                    "open": int(row["open"]),
                    "ok_rate": round(int(row["ok"]) / finished, 4) if finished else None,
                    "p95_ms": int(row["p95_ms"] or 0),
                    "cost_usd_micros": int(row["cost_usd_micros"] or 0),
                    "last_run_at": _iso(row["last_run_at"]),
                    "last_failed_run_id": row["last_failed_run_id"],
                    "state": "failing" if int(row["failed"]) else "healthy"})
    return out


__all__ = ["DbSpanSink", "agent_health", "run_for_decision", "run_trace", "span_detail"]
