from __future__ import annotations

import json
import re
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from .core import emit_event, make_id, normalize_data_boundary


EVENT_TYPE_PATTERN = re.compile(r"^[a-z0-9_]+(?:\.[a-z0-9_]+)*(?:\.\*)?$")
#: `workflow` starts one of the project's own workflows. Added because every event-driven
#: example — a meeting ending, a demo request arriving, a trend detected — wanted the same
#: thing and there was no sink that did it: an event could reach a job or an outbound webhook,
#: and not the thing an operator had actually built.
SINK_KINDS = {"job", "webhook_out", "workflow"}

DEFAULT_EVENT_SUBSCRIPTIONS = (
    {
        "event_type": "code_task.completed",
        "sink_kind": "job",
        "sink_ref": "docs.refresh",
        "data_boundary": "internal",
        "config": {"consumer": "documentation"},
    },
    {
        "event_type": "code_task.pr_opened",
        "sink_kind": "job",
        "sink_ref": "docs.update",
        "data_boundary": "internal",
        "config": {"consumer": "documentation"},
    },
    {
        "event_type": "software_factory.pull_request_merged",
        "sink_kind": "job",
        "sink_ref": "docs.update",
        "data_boundary": "internal",
        "config": {"consumer": "documentation"},
    },
    {
        "event_type": "discovery.completed",
        "sink_kind": "job",
        "sink_ref": "docs.update",
        "data_boundary": "internal",
        "config": {"consumer": "documentation"},
    },
    {
        "event_type": "plan.dispatched",
        "sink_kind": "job",
        "sink_ref": "agent.orchestrator.next_move",
        "data_boundary": "internal",
        "config": {"consumer": "orchestration"},
    },
    {
        "event_type": "webhook.inbound.received",
        "sink_kind": "job",
        "sink_ref": "github.triage",
        "data_boundary": "internal",
        "config": {"consumer": "github-community"},
    },
    {
        "event_type": "github.issue_opened",
        "sink_kind": "job",
        "sink_ref": "github.triage",
        "data_boundary": "internal",
        "config": {"consumer": "github-community"},
    },
    {
        "event_type": "github.issue_comment",
        "sink_kind": "job",
        "sink_ref": "github.triage",
        "data_boundary": "internal",
        "config": {"consumer": "github-community"},
    },
    {
        "event_type": "github.pr_opened",
        "sink_kind": "job",
        "sink_ref": "github.pr_review",
        "data_boundary": "internal",
        "config": {"consumer": "github-community"},
    },
    {
        "event_type": "github.pr_review",
        "sink_kind": "job",
        "sink_ref": "github.pr_review",
        "data_boundary": "internal",
        "config": {"consumer": "github-community"},
    },
)


def validate_event_type(value: str) -> str:
    event_type = value.strip().lower()
    if not EVENT_TYPE_PATTERN.fullmatch(event_type):
        raise ValueError(
            "event_type must be an exact lowercase type or a suffix glob such as code_task.*."
        )
    return event_type


def validate_sink_kind(value: str) -> str:
    sink_kind = value.strip().lower()
    if sink_kind not in SINK_KINDS:
        raise ValueError("sink_kind must be one of: job, webhook_out, workflow.")
    return sink_kind


def event_type_matches(pattern: str, event_type: str) -> bool:
    if pattern.endswith(".*"):
        return event_type.startswith(pattern[:-1])
    return pattern == event_type


def refuse_per_rule_only(sink_kind: str, sink_ref: str) -> None:
    """A workflow sink may not start Incident Response (BOSSAPP-598: per rule, never per app).
    Said at setup, not only at dispatch: a subscription that can never start anything must not
    be saved as if it would."""
    from .event_dispatch import PER_RULE_ONLY, PER_RULE_WORD

    if sink_kind == "workflow" and sink_ref.strip() in PER_RULE_ONLY:
        raise ValueError(PER_RULE_WORD + ".")


def _row(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "event_type": row["event_type"],
        "sink_kind": row["sink_kind"],
        "sink_ref": row["sink_ref"],
        "data_boundary": row["data_boundary"],
        "enabled": bool(row["enabled"]),
        "config": row["config"] or {},
        "created_at": row["created_at"].isoformat()
        if hasattr(row["created_at"], "isoformat")
        else row["created_at"],
        "updated_at": row["updated_at"].isoformat()
        if hasattr(row["updated_at"], "isoformat")
        else row["updated_at"],
        # Whose standing grant a workflow sink runs on (None: started as BOSS itself).
        "on_behalf_of": row.get("on_behalf_of") if hasattr(row, "get") else None,
        "grant_id": row.get("grant_id") if hasattr(row, "get") else None,
    }


def _workflow_link(connection: Connection, project_id: str, workflow_key: str,
                   event_type: str) -> dict[str, Any]:
    """A WORKFLOW SINK RUNS UNATTENDED, so it runs on a standing grant — the schedule's rule
    (consent.require_for_schedule). In a person's session this returns the link to store, or
    raises ConsentRequired (409 consent_required: the console asks for the grant). Outside one
    (BOSS's own setup, an API key) it is {} and the sink starts runs as BOSS itself, as before."""
    from ..identity import consent as _consent

    spec = connection.execute(text(
        "SELECT v.spec FROM workflow_definitions d "
        "JOIN workflow_versions v ON v.id = d.active_version_id "
        "WHERE d.project_id = :p AND d.workflow_key = :k"),
        {"p": project_id, "k": workflow_key}).scalar()
    if spec is None:
        return {}
    return _consent.require_for_schedule(
        connection, project_id=project_id, spec=spec, input_data={},
        name=f"When {event_type} happens: {workflow_key}") or {}


def _store_link(connection: Connection, subscription_id: str, link: dict[str, Any]) -> None:
    connection.execute(text(
        "UPDATE event_subscriptions SET on_behalf_of = :o, grant_id = :g WHERE id = :i"),
        {"o": link.get("on_behalf_of"), "g": link.get("grant_id"), "i": subscription_id})


def _with_readiness(connection: Connection, subscription: dict[str, Any]) -> dict[str, Any]:
    """A subscription that starts a workflow comes back with what that workflow needs here.

    Saved, not refused: setting up "start meeting-to-tickets when a meeting ends" before the
    tracker is connected is a reasonable order, and the example setup scripts do it. But the
    event door refuses a start the project cannot finish, and nobody is at the keyboard when
    the event arrives — so whoever saves it is shown, now, what will be refused, and can put
    the keys they accept in `config.start_anyway` (review of the readiness change).
    """
    if subscription.get("sink_kind") != "workflow":
        return subscription
    from .workflow_readiness import readiness_of_version

    try:
        with connection.begin_nested():
            version_id = connection.execute(
                text("SELECT active_version_id FROM workflow_definitions "
                     "WHERE project_id = :p AND workflow_key = :k"),
                {"p": subscription["project_id"], "k": subscription["sink_ref"]},
            ).scalar()
    except Exception:  # noqa: BLE001 — an install without workflows has nothing to check
        version_id = None
    if not version_id:
        return {**subscription, "readiness": {
            "ready": False, "checked": [], "missing": [], "applies": False,
            "summary": f"there is no published workflow '{subscription['sink_ref']}' here, "
                       "so this subscription will start nothing until there is"}}
    readiness = readiness_of_version(connection, None, subscription["project_id"],
                                     version_id=str(version_id))
    accepted = (subscription.get("config") or {}).get("start_anyway")
    accepted = [str(k) for k in accepted] if isinstance(accepted, list) else []
    readiness["started_anyway"] = [m["key"] for m in readiness.get("missing") or []
                                   if m["key"] in accepted]
    readiness["will_refuse"] = [m["key"] for m in readiness.get("missing") or []
                                if m["key"] not in accepted]
    return {**subscription, "readiness": readiness}


def _invalidate(project_id: str) -> None:
    # Local import prevents the CRUD service and dispatcher from forming an
    # import cycle during application startup.
    from .event_dispatch import invalidate_subscription_cache

    invalidate_subscription_cache(project_id)


def list_event_subscriptions(
    connection: Connection,
    project_id: str,
    *,
    enabled_only: bool = False,
) -> list[dict[str, Any]]:
    query = """
        SELECT id, project_id, event_type, sink_kind, sink_ref, data_boundary,
               enabled, config, created_at, updated_at, on_behalf_of, grant_id
        FROM event_subscriptions
        WHERE project_id = :project_id
    """
    if enabled_only:
        query += " AND enabled = true"
    query += " ORDER BY event_type, sink_kind, sink_ref"
    rows = connection.execute(
        text(query), {"project_id": project_id}
    ).mappings().all()
    return [_row(row) for row in rows]


def _authorizer(actor: str) -> str:
    """Who passed the job-kind check that a job sink's stamp records: the verified principal
    when a request carries one, else the in-process actor."""
    from ..identity import lineage

    ctx = lineage.current_context()
    principal = getattr(getattr(ctx, "principal", None), "id", None) if ctx else None
    return str(principal or actor or "system")


def job_sink_held(connection: Connection, subscription: dict[str, Any]) -> bool:
    """IS THIS JOB SINK HELD AT DISPATCH (rc6)? The job-kind check (C-2) runs when a sink is set
    up, re-pointed or re-enabled, and stamps `kind_authorized_by`. A sink created before that
    check existed carries no stamp, so nobody's permission ever covered the kind it queues on
    every event: it is re-checked on its next use, here, and held (not queued) until a person
    who holds the kind's permission saves or re-enables it. BOSS's own default sinks
    (DEFAULT_EVENT_SUBSCRIPTIONS) are not held, and a kind BOSS does not know is always held.
    Shadow/off: never held, as require_job_kind allows there."""
    if subscription.get("sink_kind") != "job":
        return False
    from ..identity.job_permissions import _enforced, permission_for

    kind = str(subscription.get("sink_ref") or "")
    stamped = connection.execute(text(
        "SELECT kind_authorized_by FROM event_subscriptions WHERE id = :i"),
        {"i": subscription["id"]}).scalar()
    if not _enforced():
        return False
    if stamped == "boss":
        return False           # BOSS's own (seeded defaults, the canary): never a worker kind check
    if permission_for(kind) is None:
        return True
    if stamped:
        return False
    return not any(d["sink_ref"] == kind and d["event_type"] == subscription.get("event_type")
                   for d in DEFAULT_EVENT_SUBSCRIPTIONS)


def create_event_subscription(
    connection: Connection,
    project_id: str,
    event_type: str,
    sink_kind: str,
    sink_ref: str,
    *,
    data_boundary: str = "internal",
    enabled: bool = True,
    config: Optional[dict[str, Any]] = None,
    actor: str = "system",
    authorize: bool = True,
) -> dict[str, Any]:
    normalized_event_type = validate_event_type(event_type)
    normalized_sink_kind = validate_sink_kind(sink_kind)
    normalized_boundary = normalize_data_boundary(data_boundary)
    if not sink_ref.strip():
        raise ValueError("sink_ref must not be empty.")
    refuse_per_rule_only(normalized_sink_kind, sink_ref)
    if normalized_sink_kind == "job" and authorize:
        # C-2: a job sink queues that kind of job on every matching event, so setting one up
        # needs the kind's own permission (and step-up) — `integration:manage` alone never
        # buys a critical effect. `authorize=False` only for BOSS's own canary (no person).
        from ..identity.job_permissions import require_job_kind

        require_job_kind(sink_ref.strip(), project_id, db=connection)
    subscription_id = make_id("sub")
    authorized_by = (_authorizer(actor) if authorize else "boss") \
        if normalized_sink_kind == "job" else None
    try:
        row = connection.execute(
            text(
                """
                INSERT INTO event_subscriptions (
                    id, project_id, event_type, sink_kind, sink_ref,
                    data_boundary, enabled, config, kind_authorized_by
                )
                VALUES (
                    :id, :project_id, :event_type, :sink_kind, :sink_ref,
                    :data_boundary, :enabled, CAST(:config AS JSONB), :kind_authorized_by
                )
                RETURNING id, project_id, event_type, sink_kind, sink_ref,
                          data_boundary, enabled, config, created_at, updated_at
                """
            ),
            {
                "id": subscription_id,
                "project_id": project_id,
                "event_type": normalized_event_type,
                "sink_kind": normalized_sink_kind,
                "sink_ref": sink_ref.strip(),
                "data_boundary": normalized_boundary,
                "enabled": bool(enabled),
                "config": json.dumps(config or {}),
                "kind_authorized_by": authorized_by,
            },
        ).mappings().one()
    except IntegrityError as exc:
        raise ValueError(
            "The event subscription conflicts with an existing subscription or project."
        ) from exc
    if normalized_sink_kind == "workflow" and authorize:
        link = _workflow_link(connection, project_id, sink_ref.strip(), normalized_event_type)
        if link:
            _store_link(connection, subscription_id, link)
            row = {**row, "on_behalf_of": link.get("on_behalf_of"),
                   "grant_id": link.get("grant_id")}
    _invalidate(project_id)
    emit_event(
        connection,
        project_id,
        "event_subscription.created",
        actor,
        "event_subscription",
        subscription_id,
        {
            "event_type": normalized_event_type,
            "sink_kind": normalized_sink_kind,
            "sink_ref": sink_ref.strip(),
            "enabled": bool(enabled),
        },
    )
    return _with_readiness(connection, _row(row))


def update_event_subscription(
    connection: Connection,
    project_id: str,
    subscription_id: str,
    *,
    event_type: Optional[str] = None,
    sink_kind: Optional[str] = None,
    sink_ref: Optional[str] = None,
    data_boundary: Optional[str] = None,
    enabled: Optional[bool] = None,
    config: Optional[dict[str, Any]] = None,
    actor: str = "system",
) -> Optional[dict[str, Any]]:
    assignments: list[str] = []
    params: dict[str, Any] = {
        "project_id": project_id,
        "subscription_id": subscription_id,
    }
    if event_type is not None:
        assignments.append("event_type = :event_type")
        params["event_type"] = validate_event_type(event_type)
    if sink_kind is not None:
        assignments.append("sink_kind = :sink_kind")
        params["sink_kind"] = validate_sink_kind(sink_kind)
    if sink_ref is not None:
        if not sink_ref.strip():
            raise ValueError("sink_ref must not be empty.")
        assignments.append("sink_ref = :sink_ref")
        params["sink_ref"] = sink_ref.strip()
    if data_boundary is not None:
        assignments.append("data_boundary = :data_boundary")
        params["data_boundary"] = normalize_data_boundary(data_boundary)
    if enabled is not None:
        assignments.append("enabled = :enabled")
        params["enabled"] = bool(enabled)
    if config is not None:
        assignments.append("config = CAST(:config AS JSONB)")
        params["config"] = json.dumps(config)

    if sink_kind is not None or sink_ref is not None or enabled is True:
        # Re-pointing or re-enabling a sink: the same refusal as creating it. A retired
        # app-wide Respond automatically stays off; its row is kept for history.
        current = connection.execute(text(
            "SELECT sink_kind, sink_ref, enabled FROM event_subscriptions "
            "WHERE id = :subscription_id AND project_id = :project_id"), params).mappings().first()
        if current is not None:
            will_be_on = enabled if enabled is not None else bool(current["enabled"])
            if will_be_on:
                refuse_per_rule_only(params.get("sink_kind", current["sink_kind"]),
                                     params.get("sink_ref", current["sink_ref"]))
    if assignments:
        changed_fields = [item.split(" =", 1)[0] for item in assignments]
        assignments.append("updated_at = now()")
        query = f"""
            UPDATE event_subscriptions
            SET {', '.join(assignments)}
            WHERE id = :subscription_id AND project_id = :project_id
            RETURNING id, project_id, event_type, sink_kind, sink_ref,
                      data_boundary, enabled, config, created_at, updated_at
        """
        try:
            row = connection.execute(text(query), params).mappings().first()
        except IntegrityError as exc:
            raise ValueError(
                "The event subscription update conflicts with an existing subscription."
            ) from exc
    else:
        changed_fields = []
        row = connection.execute(
            text(
                """
                SELECT id, project_id, event_type, sink_kind, sink_ref,
                       data_boundary, enabled, config, created_at, updated_at
                FROM event_subscriptions
                WHERE id = :subscription_id AND project_id = :project_id
                """
            ),
            params,
        ).mappings().first()
    if row is None:
        return None
    if row["sink_kind"] == "job" and (sink_kind is not None or sink_ref is not None
                                      or enabled is True):
        # C-2: pointing (or re-enabling) a job sink is queuing that kind: its permission too.
        from ..identity.job_permissions import require_job_kind

        require_job_kind(str(row["sink_ref"]), project_id, db=connection)
        connection.execute(text(
            "UPDATE event_subscriptions SET kind_authorized_by = :by "
            "WHERE id = :subscription_id AND project_id = :project_id"),
            {"by": _authorizer(actor), "subscription_id": subscription_id,
             "project_id": project_id})
    if row["sink_kind"] == "workflow" and (sink_kind is not None or sink_ref is not None
                                           or enabled is True):
        # Re-pointed or re-enabled ("Renew authority"): on the grant of whoever did it now.
        link = _workflow_link(connection, project_id, str(row["sink_ref"]),
                              str(row["event_type"]))
        if link:
            _store_link(connection, subscription_id, link)
    _invalidate(project_id)
    emit_event(
        connection,
        project_id,
        "event_subscription.updated",
        actor,
        "event_subscription",
        subscription_id,
        {"changed_fields": changed_fields},
    )
    return _with_readiness(connection, _row(row))


def delete_event_subscription(
    connection: Connection,
    project_id: str,
    subscription_id: str,
    *,
    actor: str = "system",
) -> bool:
    row = connection.execute(
        text(
            """
            DELETE FROM event_subscriptions
            WHERE id = :subscription_id AND project_id = :project_id
            RETURNING id
            """
        ),
        {"subscription_id": subscription_id, "project_id": project_id},
    ).mappings().first()
    if row is None:
        return False
    _invalidate(project_id)
    emit_event(
        connection,
        project_id,
        "event_subscription.deleted",
        actor,
        "event_subscription",
        subscription_id,
        {},
    )
    return True


def ensure_event_subscription(
    connection: Connection,
    project_id: str,
    event_type: str,
    sink_kind: str,
    sink_ref: str,
    *,
    data_boundary: str = "internal",
    enabled: bool = True,
    config: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    refuse_per_rule_only(validate_sink_kind(sink_kind), sink_ref)
    row = connection.execute(
        text(
            """
            INSERT INTO event_subscriptions (
                id, project_id, event_type, sink_kind, sink_ref,
                data_boundary, enabled, config, kind_authorized_by
            )
            VALUES (
                :id, :project_id, :event_type, :sink_kind, :sink_ref,
                :data_boundary, :enabled, CAST(:config AS JSONB), 'boss'
            )
            ON CONFLICT (project_id, event_type, sink_kind, sink_ref)
            DO UPDATE SET
                data_boundary = EXCLUDED.data_boundary,
                enabled = EXCLUDED.enabled,
                config = EXCLUDED.config,
                kind_authorized_by = 'boss',
                updated_at = now()
            RETURNING id, project_id, event_type, sink_kind, sink_ref,
                      data_boundary, enabled, config, created_at, updated_at
            """
        ),
        {
            "id": make_id("sub"),
            "project_id": project_id,
            "event_type": validate_event_type(event_type),
            "sink_kind": validate_sink_kind(sink_kind),
            "sink_ref": sink_ref.strip(),
            "data_boundary": normalize_data_boundary(data_boundary),
            "enabled": bool(enabled),
            "config": json.dumps(config or {}),
        },
    ).mappings().one()
    _invalidate(project_id)
    return _with_readiness(connection, _row(row))


def seed_default_event_subscriptions(
    connection: Connection, project_id: str
) -> list[dict[str, Any]]:
    return [
        ensure_event_subscription(connection, project_id, **subscription)
        for subscription in DEFAULT_EVENT_SUBSCRIPTIONS
    ]
