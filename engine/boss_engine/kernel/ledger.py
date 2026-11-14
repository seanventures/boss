"""THE RUN LEDGER, WRITE SIDE (K0, design §3).

What a run did lived in four tables that did not know about each other: llm_calls (spend),
agent_turns (transcript), tool_call_audit (tool decisions) and agent_acts (who). None carried a
run sequence, a retry lineage or the rules that produced the row, so "what did I already try,
and what did it cost?" had no answer an agent could ask for. The ledger joins them by run and
sequence (the `run_ledger` view) and adds `run_events` for what had no home: findings,
verification, decisions, escalations, budget waits, workspace snapshots, outcomes and the turns
of a native CLI loop.

Rules this module keeps:

  * BEST-EFFORT ON THE LEGACY PATHS. llm_telemetry, the tool-call audit and agent_turns call in
    here behind BOSS_LEDGER_EVENTS (default ON since AP-P0; set it to 0 to stop the
    dual-write). A ledger failure never fails the call it
    describes: they write in a savepoint and swallow, as the telemetry around them already does.
  * REDACTED AND CLIPPED ON THE WAY IN. Every string in `data` passes the secret redactor
    (redaction.redact_secrets, the one `_ask_reading` uses) before it is stored; `data` is at
    most 8 KB, a bigger body lives in agent_turns and is named by `content_ref`.
  * SEQ FROM ONE SEQUENCE (run_ledger_seq): never waits on another transaction, unique
    everywhere, increasing within every run.
  * LINEAGE SURVIVES RETRIES. A code task's retry chain is named by its first run
    (finalize_ctx.root_run_id); every row of every attempt carries it.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from sqlalchemy import text

from .types import EVENT_KINDS, Event

log = logging.getLogger("boss.kernel.ledger")

FLAG = "BOSS_LEDGER_EVENTS"
#: The largest `data` a run event stores, in bytes of JSON.
MAX_DATA_BYTES = 8_192
#: A string inside `data` longer than this keeps its head and tail.
MAX_STRING_CHARS = 2_000


#: Words that switch the dual-write off. Anything else, unset included, leaves it on.
_OFF_WORDS = ("0", "false", "no", "off")


def enabled() -> bool:
    """BOSS_LEDGER_EVENTS: ON unless set to a false word (AP-P0, docs/design/AGENT_PLATFORM.md
    §5 Phase 0: a ledger only a test turned on traced nothing). The legacy writers dual-write
    while it is on; the kernel's own sink writes whenever a caller hands it one. Retention is
    services/ledger_retention.py (monthly partitions dropped, never rows rewritten)."""
    return (os.environ.get(FLAG) or "").strip().lower() not in _OFF_WORDS


def new_event_id() -> str:
    """'evt_' + milliseconds + randomness: sorts by time like a ULID, no dependency."""
    return f"evt_{int(time.time() * 1000):012x}{secrets.token_hex(6)}"


def next_seq(connection: Any) -> int:
    return int(connection.execute(text("SELECT nextval('run_ledger_seq')")).scalar())


# ── Redaction and clipping ────────────────────────────────────────────────────────────────────

def _clip_string(value: str) -> str:
    if len(value) <= MAX_STRING_CHARS:
        return value
    half = MAX_STRING_CHARS // 2
    return f"{value[:half]}…[{len(value) - 2 * half} chars cut]…{value[-half:]}"


def redact_data(value: Any) -> Any:
    """Every string redacted, then clipped; containers walked. Redaction runs FIRST: clipping
    first could keep a secret inside the kept window while calling the row sanitised."""
    if isinstance(value, str):
        from ..services.redaction import redact_secrets

        return _clip_string(redact_secrets(value))
    if isinstance(value, Mapping):
        return {str(k): redact_data(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_data(v) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_data(str(value))


def _redact_whole(data: Mapping[str, Any]) -> dict:
    """Redact the JSON text, so a value is judged WITH its key: `{"password": "hunter2"}` is a
    secret only because of the key beside it, which a leaf-by-leaf pass never sees. Then every
    leaf again (a token shape inside a long string). Text the redactor left unparseable falls
    back to the leaf pass alone."""
    from ..services.redaction import redact_secrets

    try:
        whole = json.loads(redact_secrets(json.dumps(dict(data), default=str)))
    except ValueError:
        whole = dict(data)
    return redact_data(whole if isinstance(whole, dict) else dict(data))


def fit(data: Mapping[str, Any]) -> dict:
    """Redacted `data` within MAX_DATA_BYTES; past it, a marked preview of the whole."""
    cleaned = _redact_whole(dict(data or {}))
    body = json.dumps(cleaned, default=str)
    if len(body.encode("utf-8")) <= MAX_DATA_BYTES:
        return cleaned
    return {"_clipped": True, "bytes": len(body.encode("utf-8")),
            "preview": body[: MAX_DATA_BYTES - 200]}


# ── The shape of the kinds that matter (design §3.2) ─────────────────────────────────────────

class LedgerDataError(ValueError):
    """An event whose kind is unknown or whose data lacks what its kind requires."""


_ENUMS = {
    "finding": {"source": ("test", "lint", "review", "gate", "schema", "tool", "verifier"),
                "severity": ("blocker", "major", "minor", "info")},
    "outcome": {"scope": ("run", "stage"),
                "result": ("pass", "fail", "shipped_failing", "merged", "reverted")},
}
_REQUIRED = {
    "finding": ("source", "severity", "message"),
    "verify.result": ("command", "exit"),
    "decision": ("question", "chosen"),
    "outcome": ("scope", "result"),
}
_FIELDS = {
    "finding": ("source", "severity", "path", "line", "quote", "message", "test_id",
                "assertion", "fixed_in_seq"),
    "verify.result": ("command", "exit", "passed", "failed", "preexisting_failed", "tamper_ok",
                      "duration_ms"),
    "decision": ("question", "candidates", "chosen", "confidence", "by", "evidence_ids"),
    "outcome": ("scope", "result", "gates", "pr_url", "followups"),
}


def validate(kind: str, data: Mapping[str, Any]) -> dict:
    """The data a `kind` may carry. The four kinds readers depend on (finding, verify.result,
    decision, outcome) are held to their fields; the rest are free-form but still redacted."""
    if kind not in EVENT_KINDS:
        raise LedgerDataError(f"{kind!r} is not a ledger event kind")
    data = dict(data or {})
    if kind in _REQUIRED:
        missing = [k for k in _REQUIRED[kind] if data.get(k) in (None, "")]
        if missing:
            raise LedgerDataError(f"{kind} needs {', '.join(missing)}")
        unknown = sorted(k for k in data if k not in _FIELDS[kind])
        if unknown:
            raise LedgerDataError(f"{kind} does not carry {', '.join(unknown)}")
        for name, allowed in _ENUMS.get(kind, {}).items():
            if data.get(name) not in allowed:
                raise LedgerDataError(f"{kind}.{name} must be one of {allowed}")
    return data


# ── Lineage ───────────────────────────────────────────────────────────────────────────────────

_LINEAGE_CACHE: dict = {}
_LINEAGE_CACHE_MAX = 2_048


def lineage_of(connection: Any, run_id: Optional[str]) -> tuple:
    """(lineage_id, attempt) for a run: a code task's retry chain is named by its root run
    (finalize_ctx.root_run_id, set by retry_code_task and the repair children); an agent run may
    name one in finalize_ctx.lineage_id; anything else is its own lineage, attempt 1."""
    if not run_id:
        return None, 1
    cached = _LINEAGE_CACHE.get(run_id)
    if cached is not None:
        return cached
    found = None
    for table in ("code_task_runs", "agent_runs"):
        try:
            with connection.begin_nested():
                row = connection.execute(
                    text(f"SELECT finalize_ctx FROM {table} WHERE id = :r"), {"r": run_id}
                ).first()
        except Exception:  # noqa: BLE001 — a store without the table: its own lineage
            row = None
        if row is not None:
            ctx = row[0] if isinstance(row[0], Mapping) else _json(row[0])
            root = str(ctx.get("root_run_id") or ctx.get("lineage_id") or run_id)
            try:
                attempt = max(1, int(ctx.get("attempt") or 1))
            except (TypeError, ValueError):
                attempt = 1
            found = (root, attempt)
            break
    found = found or (run_id, 1)
    if len(_LINEAGE_CACHE) >= _LINEAGE_CACHE_MAX:
        _LINEAGE_CACHE.clear()
    _LINEAGE_CACHE[run_id] = found
    return found


def _json(value: Any) -> dict:
    try:
        parsed = json.loads(value) if isinstance(value, str) else {}
    except ValueError:
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


# ── Writing ───────────────────────────────────────────────────────────────────────────────────

def append(connection: Any, *, project_id: str, run_id: str, kind: str,
           data: Optional[Mapping[str, Any]] = None, seq: Optional[int] = None, step: int = 0,
           act_id: Optional[str] = None, root_act_id: Optional[str] = None,
           stage: Optional[str] = None, content_ref: Optional[str] = None,
           usage: Optional[Mapping[str, Any]] = None, latency_ms: int = 0,
           engine: Optional[str] = None, model: Optional[str] = None,
           scaffold_level: Optional[str] = None, policy: Optional[Mapping[str, Any]] = None,
           workspace_ref: Optional[str] = None, attempt: Optional[int] = None,
           lineage_id: Optional[str] = None) -> dict:
    """Append one run event and return {id, seq}. Raises on bad data (a caller's bug): the
    best-effort wrappers below are what the legacy paths call."""
    body = fit(validate(kind, data or {}))
    if lineage_id is None or attempt is None:
        found_lineage, found_attempt = lineage_of(connection, run_id)
        lineage_id = lineage_id or found_lineage
        attempt = attempt or found_attempt
    u = dict(usage or {})
    row = {
        "id": new_event_id(), "project_id": project_id, "run_id": run_id, "act_id": act_id,
        "root_act_id": root_act_id, "seq": int(seq) if seq is not None else next_seq(connection),
        "step": int(step or 0), "stage": stage, "kind": kind, "data": json.dumps(body),
        "content_ref": content_ref,
        "it": int(u.get("input_tokens") or u.get("prompt_tokens") or 0),
        "ot": int(u.get("output_tokens") or u.get("completion_tokens") or 0),
        "crt": int(u.get("cache_read_tokens") or 0), "cwt": int(u.get("cache_write_tokens") or 0),
        "cost": int(u.get("cost_usd_micros") or 0),
        "inc": str(u.get("billing") or "") in ("subscription", "local"),
        "lat": int(latency_ms or 0), "engine": engine, "model": model or u.get("model") or None,
        "level": scaffold_level, "policy": json.dumps(dict(policy or {})),
        "ws": workspace_ref, "attempt": int(attempt or 1), "lineage": lineage_id,
    }
    connection.execute(text("""
        INSERT INTO run_events (id, project_id, run_id, act_id, root_act_id, seq, step, stage,
            kind, data, content_ref, input_tokens, output_tokens, cache_read_tokens,
            cache_write_tokens, cost_usd_micros, included_in_subscription, latency_ms, engine,
            model, scaffold_level, policy, workspace_ref, attempt, lineage_id)
        VALUES (:id, :project_id, :run_id, :act_id, :root_act_id, :seq, :step, :stage, :kind,
            CAST(:data AS jsonb), :content_ref, :it, :ot, :crt, :cwt, :cost, :inc, :lat, :engine,
            :model, :level, CAST(:policy AS jsonb), :ws, :attempt, :lineage)
    """), row)
    return {"id": row["id"], "seq": row["seq"]}


def register_policy(connection: Any, policy_id: str, *, kind: str, body: Mapping[str, Any],
                    source: str) -> bool:
    """Record what `policy_id` means, once. True when this call recorded it."""
    done = connection.execute(text("""
        INSERT INTO policy_versions (id, kind, body, source)
        VALUES (:id, :kind, CAST(:body AS jsonb), :source) ON CONFLICT (id) DO NOTHING
    """), {"id": policy_id, "kind": kind, "body": json.dumps(dict(body), default=str),
           "source": source})
    return bool(getattr(done, "rowcount", 0))


def ledger_run_for(ctx: Mapping[str, Any], act_id: Optional[str], row_id: str) -> str:
    """The run a model call belongs to: the kernel's run, else the code task run, else the act it
    was signed under, else the call itself (a one-call run). Never empty: every row written while
    the ledger is on has a run_id."""
    return str(ctx.get("ledger_run_id") or ctx.get("run_id") or act_id or row_id)


def llm_call_fields(connection: Any, ctx: Mapping[str, Any], *, act_id: Optional[str],
                    row_id: str) -> dict:
    """The ledger columns of one llm_calls row, or {} when nothing asks for them (the flag is off
    and no kernel run is recording). Called inside record_llm_call's transaction."""
    if not (enabled() or ctx.get("ledger_seq") is not None):
        return {}
    run_id = ledger_run_for(ctx, act_id, row_id)
    lineage, _attempt = (ctx.get("lineage_id"), None) if ctx.get("lineage_id") \
        else lineage_of(connection, run_id)
    seq = ctx.get("ledger_seq")
    return {"run_id": run_id,
            "seq": int(seq) if seq is not None else next_seq(connection),
            "lineage_id": lineage,
            "policy": json.dumps(dict(ctx.get("policy") or {})) if ctx.get("policy") else None,
            "scaffold_level": ctx.get("scaffold_level"),
            "workspace_ref": ctx.get("workspace_ref")}


def tool_call_fields(connection: Any, *, run_id: Optional[str], act_id: Optional[str],
                     row_id: str, seq: Optional[int] = None) -> dict:
    """The ledger columns of one tool_call_audit row, or {} while the flag is off and no seq was
    handed in. A call outside any run is its own run, so it still reads back."""
    if not (enabled() or seq is not None):
        return {}
    ledger_run = run_id or act_id or row_id
    lineage, _attempt = lineage_of(connection, ledger_run)
    return {"ledger_run_id": ledger_run, "act_id": act_id,
            "seq": int(seq) if seq is not None else next_seq(connection),
            "lineage_id": lineage}


def record_turn_events(connection: Any, rows: list, *, project_id: Optional[str],
                       run_id: Optional[str], act_id: Optional[str], stage: Optional[str],
                       engine: Optional[str]) -> int:
    """One `delegate.turn` event per agent_turns row just written (the turn's body stays in
    agent_turns, named by content_ref). Never raises; returns events written."""
    if not enabled() or not rows or not project_id:
        return 0
    written = 0
    try:
        with connection.begin_nested():
            ledger_run = run_id or act_id or str(rows[0].get("id"))
            lineage, attempt = lineage_of(connection, ledger_run)
            for row in rows:
                append(connection, project_id=str(project_id), run_id=ledger_run,
                       kind="delegate.turn", act_id=act_id, stage=stage, engine=engine,
                       model=row.get("model"), content_ref=str(row.get("id")),
                       lineage_id=lineage, attempt=attempt,
                       latency_ms=int(row.get("latency_ms") or 0),
                       usage={"output_tokens": int(row.get("tokens") or 0)},
                       data={"direction": row.get("direction"), "tool": row.get("tool_name"),
                             "turn_index": row.get("turn_index"), "ok": row.get("ok"),
                             "level": row.get("level"), "truncated": row.get("truncated")})
                written += 1
    except Exception as exc:  # noqa: BLE001 — a transcript's ledger copy never fails the build
        log.warning("run ledger: turn events not written (non-fatal): %s", exc)
        return 0
    return written


# ── The kernel's sink ─────────────────────────────────────────────────────────────────────────

@dataclass
class SinkMeta:
    engine: str = ""
    stage: Optional[str] = None
    scaffold_level: Optional[str] = None
    #: Who the run acts as (RunSpec.actor): the tool-call audit row's `agent` (BOSSAPP-819).
    actor: Optional[str] = None


class DbSink:
    """Where a kernel run's events go when the caller has a database (design §3.1):

      model.response / model.error   one llm_calls row, through llm_telemetry.record_llm_call,
                                     so budgets, routing and cost screens see kernel calls too
      tool.result / .refused /       one tool_call_audit row (the tool-decision ledger)
        .deferred
      model.request / tool.requested kept in the run's own event list only (volume)
      everything else                one run_events row

    Each write is its own short transaction on `engine`, never the caller's connection: an
    agent run's tick is one long transaction, and the ledger must neither hold its locks nor
    vanish when that transaction rolls back. A write that fails is logged, never raised."""

    SKIP = frozenset({"model.request", "tool.requested"})

    def __init__(self, engine: Any):
        self.engine = engine

    def next_seq(self) -> Optional[int]:
        try:
            with self.engine.begin() as c:
                return next_seq(c)
        except Exception as exc:  # noqa: BLE001 — no seq from the database: the run counts
            log.warning("run ledger: no sequence (non-fatal): %s", exc)
            return None

    def write(self, event: Event, state: Any, meta: SinkMeta) -> None:
        if event.kind in self.SKIP:
            return
        try:
            if event.kind in ("model.response", "model.error"):
                self._model_call(event, state, meta)
            elif event.kind in ("tool.result", "tool.refused", "tool.deferred"):
                self._tool_call(event, state, meta)
            else:
                with self.engine.begin() as c:
                    append(c, project_id=state.project_id, run_id=state.run_id,
                           kind=event.kind, data=event.data, seq=event.seq, step=event.step,
                           act_id=state.act_id, stage=meta.stage,
                           usage=_usage_dict(event), latency_ms=event.latency_ms,
                           engine=meta.engine, scaffold_level=meta.scaffold_level,
                           policy=event.policy, workspace_ref=event.workspace_ref,
                           attempt=state.attempt, lineage_id=state.lineage_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("run ledger: %s not written (non-fatal): %s", event.kind, exc)

    def _model_call(self, event: Event, state: Any, meta: SinkMeta) -> None:
        import contextvars

        from ..services import llm_telemetry

        def write() -> None:
            llm_telemetry.set_llm_context(
                db_engine=self.engine, project_id=state.project_id, act_id=state.act_id,
                ledger_run_id=state.run_id, ledger_seq=event.seq, lineage_id=state.lineage_id,
                policy=dict(event.policy), scaffold_level=meta.scaffold_level,
                workspace_ref=event.workspace_ref, stage=meta.stage)
            usage = _usage_dict(event)
            llm_telemetry.record_llm_call(
                engine_name=meta.engine or "kernel", usage=usage,
                cost_usd_micros=int(usage.get("cost_usd_micros") or 0),
                latency_ms=event.latency_ms, turns=1, ok=event.kind == "model.response",
                error=None if event.kind == "model.response" else str(event.data.get("error")),
                stage=meta.stage, project_id=state.project_id)

        # A copy of the caller's context: the ledger keys must not leak into its later calls.
        contextvars.copy_context().run(write)

    def _tool_call(self, event: Event, state: Any, meta: Optional[SinkMeta] = None) -> None:
        from ..services.tool_call_audit import acting_principal, record_tool_call

        data = event.data
        decision = {"tool.result": "allow", "tool.refused": "refused",
                    "tool.deferred": "deferred"}[event.kind]
        # WHO (BOSSAPP-819): the run's actor (RunSpec.actor), and the identity principal it acts
        # as (identity.lineage.current), instead of "kernel" and nobody.
        agent = str(getattr(meta, "actor", None) or "").strip() or "kernel"
        with self.engine.begin() as c:
            record_tool_call(
                c, project_id=state.project_id, run_id=state.run_id, agent=agent,
                principal=acting_principal(), server_id=None, tool=str(data.get("tool") or ""),
                arguments=data.get("args") if isinstance(data.get("args"), dict) else {},
                decision=decision, matched_policy_ids=[], decision_id=data.get("decision_id"),
                outcome=str(data.get("status") or ""), latency_ms=event.latency_ms,
                act_id=state.act_id, seq=event.seq,
                idempotency_key=data.get("idempotency_key"))


def _usage_dict(event: Event) -> dict:
    return event.usage.as_adapter_usage() if event.usage is not None else {}


class MemorySink:
    """A run's events kept in a list (tests, and runs with no database)."""

    def __init__(self) -> None:
        self.events: list = []

    def next_seq(self) -> Optional[int]:
        return None

    def write(self, event: Event, state: Any, meta: SinkMeta) -> None:
        self.events.append(event)


__all__ = ["DbSink", "FLAG", "LedgerDataError", "MemorySink", "SinkMeta", "append", "enabled",
           "fit", "ledger_run_for", "lineage_of", "llm_call_fields", "new_event_id", "next_seq",
           "record_turn_events", "redact_data", "register_policy", "tool_call_fields",
           "validate"]
