from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Union
from uuid import uuid4

from ..config import Settings
from ..ports import sql


DATA_BOUNDARIES = {"public", "internal", "confidential", "restricted", "local-only"}
logger = logging.getLogger(__name__)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def stable_dedupe_key(kind: str, *parts: Any) -> str:
    """A dedupe key derived from WHAT the work is, so a repeat actually collides.

    `tasks.dedupe_key` is checked before every INSERT and the check works. Seven call sites
    passed `make_id(...)` — a fresh random id — so the value could never match an earlier row
    and the whole mechanism silently did nothing. Clicking "Send to PRD meeting" twice made two
    meetings, two tasks and two code tasks from one outcome.

    A dedupe key is an IDENTITY, not an id. It must be a pure function of the inputs that make
    two requests "the same request": the discovery run and the outcome index, or the directive
    text. Anything freshly generated is by construction unique and therefore useless here.

    Long or unstable parts are hashed so the key stays bounded and a directive's whitespace
    does not create a second identity.
    """
    import hashlib

    material = "\u241f".join(str(p or "").strip() for p in parts)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"{kind}:{digest}"


def make_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:20]}"


def json_hash(payload: Union[dict, list, str]) -> str:
    normalized = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def normalize_data_boundary(value: Optional[str] = None) -> str:
    boundary = (value or "internal").strip().lower()
    if boundary not in DATA_BOUNDARIES:
        raise ValueError(
            "data_boundary must be one of: "
            + ", ".join(sorted(DATA_BOUNDARIES))
        )
    return boundary


def emit_event(
    connection: Any,
    project_id: str,
    event_type: str,
    actor: str,
    entity_type: str,
    entity_id: str,
    data: dict,
) -> str:
    """Emit through the configured edition sink, with Postgres compatibility."""

    from ..ports import get_engine_ports

    ports = get_engine_ports()
    if ports is not None:
        event_id = ports.events.emit(
            connection,
            project_id,
            event_type,
            actor,
            entity_type,
            entity_id,
            data,
        )
    else:
        event_id = _emit_event_postgres(
            connection,
            project_id,
            event_type,
            actor,
            entity_type,
            entity_id,
            data,
        )
    _after_event(connection, event_type, entity_type, entity_id, data)
    return event_id


#: Events that finish a unit of work, or change one that finished (scorecards._EVENT_UNITS and
#: its decision events). Checked before anything is imported: every other event costs a lookup.
_SCORECARD_PREFIXES = ("workflow.run_", "workflow.step_", "workflow.compensation_completed",
                       "code_task.", "meeting.completed", "discovery.", "decision.")


def _after_event(connection: Any, event_type: str, entity_type: str, entity_id: str,
                 data: dict) -> None:
    """A terminal event writes its unit's run scorecard (services/scorecards.py), in a
    savepoint, never raising: the scorecard never undoes or fails what it records."""
    if not event_type.startswith(_SCORECARD_PREFIXES):
        return
    if getattr(getattr(connection, "dialect", None), "name", "") != "postgresql":
        return
    try:
        from .scorecards import on_event

        on_event(connection, event_type, entity_type, entity_id, data or {})
    except Exception:  # noqa: BLE001
        logger.warning("scorecard hook failed for %s", event_type, exc_info=True)


def has_event(connection: Any, entity_id: str, event_type: str) -> bool:
    """Has this event already been recorded for this entity?

    Exists so a step that must happen ONCE per entity can ask, without the caller reaching for
    SQL. The pipeline modules are held to the ports layer — `test_pipeline_hot_paths_have_no_
    direct_database_imports_or_text_calls` fails the build if they import sqlalchemy or call
    `text(` — and "have I done this already" is a legitimate question that had no home.

    Written for the deferred-findings follow-up, which was guarded against recursion and not
    against repetition: sixteen identical tasks, because the finalisation path can run more
    than once for the same run. The event the spawn already writes is the natural key.
    """
    row = connection.execute(
        sql("SELECT 1 FROM events WHERE entity_id = :e AND event_type = :k LIMIT 1"),
        {"e": entity_id, "k": event_type},
    ).first()
    return row is not None


def _emit_event_postgres(
    connection: Any,
    project_id: str,
    event_type: str,
    actor: str,
    entity_type: str,
    entity_id: str,
    data: dict,
) -> str:
    event_id = make_id("evt")
    connection.execute(
        sql(
            """
            INSERT INTO events (
                id, project_id, event_type, actor, entity_type, entity_id,
                data, created_at
            )
            VALUES (
                :id, :project_id, :event_type, :actor, :entity_type, :entity_id,
                CAST(:data AS JSONB), clock_timestamp()
            )
            """
        ),
        {
            "id": event_id,
            "project_id": project_id,
            "event_type": event_type,
            "actor": actor,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "data": json.dumps(data),
        },
    )
    try:
        # Imported lazily to keep the hot event writer source-compatible and to
        # avoid a module cycle through jobs.enqueue_job -> core.emit_event.
        from .event_dispatch import dispatch_event, is_dispatchable_event

        if not is_dispatchable_event(event_type):
            return event_id
        # Isolate dispatcher SQL in a savepoint.  A broken subscription or
        # enqueue must never poison the business transaction that wrote the
        # canonical event.
        with connection.begin_nested():
            dispatch_event(
                connection,
                event_id=event_id,
                project_id=project_id,
                event_type=event_type,
                actor=actor,
                entity_type=entity_type,
                entity_id=entity_id,
                data=data,
            )
    except Exception as exc:  # noqa: BLE001 - fail open for the canonical write
        logger.exception(
            "event_dispatch.failed",
            extra={
                "project_id": project_id,
                "event_id": event_id,
                "event_type": event_type,
                "entity_type": entity_type,
                "entity_id": entity_id,
            },
        )
        try:
            with connection.begin_nested():
                connection.execute(
                    sql(
                        """
                        INSERT INTO events (
                            id, project_id, event_type, actor,
                            entity_type, entity_id, data
                        )
                        VALUES (
                            :id, :project_id, 'job.dispatch_failed', 'system',
                            'event', :entity_id, CAST(:data AS JSONB)
                        )
                        """
                    ),
                    {
                        "id": make_id("evt"),
                        "project_id": project_id,
                        "entity_id": event_id,
                        "data": json.dumps(
                            {
                                "source_event_type": event_type,
                                "source_entity_type": entity_type,
                                "source_entity_id": entity_id,
                                "error": str(exc),
                            }
                        ),
                    },
                )
        except Exception:  # noqa: BLE001 - structured log is the last resort
            logger.exception(
                "event_dispatch.failure_event_write_failed",
                extra={
                    "project_id": project_id,
                    "event_id": event_id,
                    "event_type": event_type,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                },
            )
    return event_id


def record_policy_eval(
    connection: Any,
    project_id: str,
    boundary: str,
    decision: str,
    *,
    action: Optional[str] = None,
    reason: Optional[str] = None,
    detail: Optional[dict] = None,
) -> str:
    """Append a durable record of a policy evaluation at any enforcement boundary (spec §13:
    policy_eval_log). `decision` is allow | deny | defer. This is the audit trail behind
    'policy violation attempts (tracked and blocked)' and replay/'why did we do that'."""
    eval_id = make_id("pev")
    connection.execute(
        sql(
            """
            INSERT INTO policy_eval_log (id, project_id, boundary, action, decision, reason, detail)
            VALUES (:id, :p, :b, :a, :d, :r, CAST(:det AS JSONB))
            """
        ),
        {"id": eval_id, "p": project_id, "b": boundary, "a": action, "d": decision,
         "r": reason, "det": json.dumps(detail or {})},
    )
    return eval_id


def attach_decision_reason(recommendation: dict | None, reason: str | None) -> dict:
    """Copy the blocking reason onto the recommendation so inbox JSON can expose it.

    The decisions table has no reason column; create_blocking used to emit the
    string only on the decision.created event. Sealed validation and the CEO
    inbox need it on the card.
    """
    rec = dict(recommendation or {})
    text = str(reason or "").strip()
    if text:
        rec.setdefault("reason_code", text)
        rec.setdefault("reason", text)
    return rec


def decision_reason_from_payload(recommendation: dict | None, fallback: str | None = None) -> str | None:
    rec = recommendation if isinstance(recommendation, dict) else {}
    for key in ("reason_code", "reason"):
        value = rec.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    text = str(fallback or "").strip()
    return text or None


def create_blocking_decision(
    connection: Any,
    project_id: str,
    task_id: Optional[str],
    question: str,
    recommendation: dict,
    cost_summary: dict,
    reason: str,
    *,
    risk_tier: int = 2,
    default_action: str = "hold",
) -> str:
    """Create a hold-default decision through the configured edition sink."""

    from ..ports import get_engine_ports

    ports = get_engine_ports()
    if ports is not None:
        return ports.decisions.create_blocking(
            connection,
            project_id,
            task_id,
            question,
            recommendation,
            cost_summary,
            reason,
            risk_tier=risk_tier,
            default_action=default_action,
        )
    return _create_blocking_decision_postgres(
        connection,
        project_id,
        task_id,
        question,
        recommendation,
        cost_summary,
        reason,
        risk_tier=risk_tier,
        default_action=default_action,
    )


def _create_blocking_decision_postgres(
    connection: Any,
    project_id: str,
    task_id: Optional[str],
    question: str,
    recommendation: dict,
    cost_summary: dict,
    reason: str,
    *,
    risk_tier: int = 2,
    default_action: str = "hold",
) -> str:
    # ONE OPEN DECISION PER CAUSE, not one per occurrence.
    #
    # Every escalation raised a brand-new row, so a run that failed the same way thirty times
    # put thirty identical items in the queue: "Model capacity exhausted…" x30 and "Missing
    # active reservation for PR proposal" x14, all unanswerable and all saying the same thing.
    # That is not a display bug -- they are distinct rows -- and it buries the decisions that
    # a person actually has to make underneath repeats of one they cannot act on anyway.
    #
    # A repeat is evidence the cause is ongoing, so it extends the deadline and counts the
    # occurrence on the decision already open. Approving or resolving it clears the cause; the
    # next occurrence after that legitimately opens a new one.
    #
    # A STEP'S GATE IS ITS RUN'S, NOT ITS WORDS'. A decision that gates a workflow step carries
    # the run and step it belongs to (run_decisions.open_step_decision), and they are part of the
    # cause: two runs of one workflow at the same Approval gate ask the same question, and
    # merging them handed both runs ONE card, so one Approve released both. A decision with no
    # run identity merges exactly as before (NULL IS NOT DISTINCT FROM NULL).
    rec_in = recommendation if isinstance(recommendation, dict) else {}
    gate_run = str(rec_in.get("workflow_run_id") or "").strip() or None
    gate_step = (str(rec_in.get("step_id") or "").strip() or None) if gate_run else None
    existing = connection.execute(
        sql(
            """
            SELECT id, recommendation FROM decisions
            WHERE project_id = :project_id
              AND question = :question
              AND status = 'pending'
              AND task_id IS NOT DISTINCT FROM :task_id
              AND recommendation->>'workflow_run_id' IS NOT DISTINCT FROM :gate_run
              AND (CAST(:gate_run AS TEXT) IS NULL
                   OR recommendation->>'step_id' IS NOT DISTINCT FROM :gate_step)
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"project_id": project_id, "question": question, "task_id": task_id,
         "gate_run": gate_run, "gate_step": gate_step},
    ).mappings().first()
    if existing:
        prior = existing["recommendation"] or {}
        if isinstance(prior, str):
            try:
                prior = json.loads(prior)
            except ValueError:
                prior = {}
        occurrences = int(prior.get("occurrences") or 1) + 1
        merged = {**prior, "occurrences": occurrences}
        connection.execute(
            sql(
                """
                UPDATE decisions
                   SET deadline_at = :deadline_at,
                       recommendation = CAST(:recommendation AS JSONB),
                       updated_at = NOW()
                 WHERE id = :id
                """
            ),
            {
                "id": existing["id"],
                "deadline_at": now_utc() + timedelta(hours=24),
                "recommendation": json.dumps(merged),
            },
        )
        return str(existing["id"])

    decision_id = make_id("dec")
    deadline_at = now_utc() + timedelta(hours=24)
    rec = attach_decision_reason(recommendation, reason)
    connection.execute(
        sql(
            """
            INSERT INTO decisions (
                id, project_id, task_id, question, recommendation, options, risk_tier,
                cost_summary, deadline_at, default_action, status
            )
            VALUES (
                :id, :project_id, :task_id, :question, CAST(:recommendation AS JSONB),
                CAST(:options AS JSONB), :risk_tier, CAST(:cost_summary AS JSONB), :deadline_at,
                :default_action, 'pending'
            )
            """
        ),
        {
            "id": decision_id,
            "project_id": project_id,
            "task_id": task_id,
            "question": question,
            "recommendation": json.dumps(rec),
            "options": json.dumps(
                [
                    {"action": "approve", "label": "Approve and retry"},
                    {"action": "reject", "label": "Keep blocked"},
                ]
            ),
            "risk_tier": risk_tier,
            "cost_summary": json.dumps(cost_summary),
            "deadline_at": deadline_at,
            "default_action": default_action,
        },
    )
    _emit_event_postgres(
        connection,
        project_id,
        "decision.created",
        "system",
        "decision",
        decision_id,
        {
            "reason": reason,
            "deadline_at": deadline_at.isoformat(),
            "default_action": default_action,
            "risk_tier": risk_tier,
        },
    )
    return decision_id


def create_artifact(
    connection: Any,
    settings: Settings,
    project_id: str,
    task_id: Optional[str],
    artifact_type: str,
    title: str,
    content: dict,
    created_by_job_id: Optional[str] = None,
    data_boundary: Optional[str] = None,
) -> str:
    boundary_source = data_boundary or content.get("data_boundary")
    if not boundary_source and task_id:
        row = connection.execute(
            sql("SELECT data_boundary FROM tasks WHERE id = :task_id"),
            {"task_id": task_id},
        ).mappings().first()
        boundary_source = row["data_boundary"] if row else None
    boundary = normalize_data_boundary(boundary_source)
    artifact_id = make_id("art")
    artifact_dir = settings.artifact_root / project_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f"{artifact_id}.json"
    artifact_path.write_text(json.dumps(content, indent=2), encoding="utf-8")
    connection.execute(
        sql(
            """
            INSERT INTO artifacts (
                id, project_id, task_id, artifact_type, title, uri, content,
                content_hash, created_by_job_id, data_boundary
            )
            VALUES (
                :id, :project_id, :task_id, :artifact_type, :title, :uri,
                CAST(:content AS JSONB), :content_hash, :created_by_job_id,
                :data_boundary
            )
            """
        ),
        {
            "id": artifact_id,
            "project_id": project_id,
            "task_id": task_id,
            "artifact_type": artifact_type,
            "title": title,
            "uri": str(artifact_path),
            "content": json.dumps(content),
            "content_hash": json_hash(content),
            "created_by_job_id": created_by_job_id,
            "data_boundary": boundary,
        },
    )
    emit_event(
        connection,
        project_id,
        "artifact.created",
        "system",
        "artifact",
        artifact_id,
        {"artifact_type": artifact_type, "title": title, "data_boundary": boundary},
    )
    return artifact_id


def expires_at(minutes: int) -> datetime:
    return now_utc() + timedelta(minutes=minutes)
