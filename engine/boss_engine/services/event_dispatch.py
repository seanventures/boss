from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import normalize_data_boundary
from .event_subscription_service import (
    event_type_matches,
    list_event_subscriptions,
)
from .jobs import enqueue_job


logger = logging.getLogger(__name__)

DISPATCHABLE_EVENT_TYPES = frozenset(
    {
        "code_task.pr_opened",
        "code_task.completed",
        "code_task.completed_no_pr",
        "plan.dispatched",
        "plan.created",
        "discovery.completed",
        "meeting.completed",
        "software_factory.pull_request_merged",
        "webhook.inbound.received",
        "github.issue_opened",
        "github.issue_comment",
        "github.pr_opened",
        "github.pr_review",
        # An app's alert opened / resolved an incident (incident_response). Any workflow may
        # follow it EXCEPT Incident Response itself, which only a rule's own Respond
        # automatically starts (PER_RULE_ONLY below).
        "incident.opened",
        "incident.resolved",
    }
)

_BOUNDARY_RANK = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "restricted": 3,
    "local-only": 4,
}
_CACHE_TTL_SECONDS = 5.0

#: INCIDENT RESPONSE STARTS PER RULE, NEVER PER APP (BOSSAPP-598). An app-wide switch (the
#: incident card's old "Respond automatically", an `incident.opened` workflow subscription)
#: answered every alert of every app the same way; the person now chooses per alert rule on the
#: app's Alerts tab. So the only unattended door that may start these workflows is an alert
#: rule's own Respond automatically (door PER_RULE_DOOR); every other door is refused here, in
#: the one function every unattended start goes through.
PER_RULE_ONLY = frozenset({"incident_response"})
PER_RULE_DOOR = "alert_rule"
#: THE INCIDENT SQUAD'S DOOR (design §12.5). The squad is itself a per-incident decision: the
#: Chief of Staff routed ONE incident to it under the project's autonomy matrix, so the squad
#: starting that incident's fix is not the app-wide switch PER_RULE_ONLY retired. The door's
#: name is not trusted: `squad_authority` re-derives the authority from the database — the
#: door_id must be a live ambient incident item of this project, and either the class's level
#: here lets an agent start a run without a person (the gate: A2+, consensus satisfied), or a
#: person answered that item's card (consent). Anything else is refused like every other door.
SQUAD_DOOR = "incident_squad"
SQUAD_CLASSES = frozenset({"incident_response", "incident_staging_mitigation",
                           "perf_regression"})
SQUAD_PERSON_CHOICES = frozenset({"Roll back + fix", "Fix only"})
SQUAD_WORD = ("The incident squad may start this fix only on an incident routed to it, at an "
              "autonomy level that lets agents start a run, or after a person approved it")
PER_RULE_WORD = ("Incident Response starts per alert rule: turn on Respond automatically for "
                 "the rule on the app's Alerts tab")
_subscription_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_cache_lock = threading.RLock()


def invalidate_subscription_cache(project_id: Optional[str] = None) -> None:
    with _cache_lock:
        if project_id is None:
            _subscription_cache.clear()
        else:
            _subscription_cache.pop(project_id, None)


def _enabled_subscriptions(
    connection: Connection, project_id: str
) -> list[dict[str, Any]]:
    now = time.monotonic()
    with _cache_lock:
        cached = _subscription_cache.get(project_id)
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]
    subscriptions = list_event_subscriptions(
        connection, project_id, enabled_only=True
    )
    with _cache_lock:
        _subscription_cache[project_id] = (now, subscriptions)
    return subscriptions


def is_dispatchable_event(event_type: str) -> bool:
    return event_type in DISPATCHABLE_EVENT_TYPES


def _event_boundary(data: dict[str, Any]) -> str:
    boundary = data.get("data_boundary", "internal")
    return normalize_data_boundary(boundary if isinstance(boundary, str) else None)


def _can_cross_boundary(event_boundary: str, sink_boundary: str) -> bool:
    # A sink may be more restrictive than an event, never less restrictive.
    return _BOUNDARY_RANK[sink_boundary] >= _BOUNDARY_RANK[event_boundary]


def squad_authority(connection: Connection, *, project_id: str,
                    item_id: str) -> dict[str, Any]:
    """Why the incident squad may start Incident Response for this item, or why not.

    {ok, why, basis: level|person, by, task_class, level}. Derived from rows, never from the
    caller: the item (ambient, an incident class, this project, not closed); then a person's
    approval on its card (consent), else the gate for `start_workflow` at the class's level here
    (allowed without a person, so A2+ on a class whose consensus a squad can satisfy)."""
    from .ambient import policy

    row = connection.execute(text(
        "SELECT id, task_class, lifecycle, ambient FROM work_items "
        "WHERE id = :i AND project_id = :p"), {"i": item_id, "p": project_id}).mappings().first()
    base = {"ok": False, "basis": None, "by": None, "task_class": None, "level": None}
    if row is None or not row["ambient"]:
        return {**base, "why": "no ambient work item with that id in this project"}
    klass = str(row["task_class"] or "")
    base["task_class"] = klass
    if klass not in SQUAD_CLASSES:
        return {**base, "why": f"{klass or 'an unclassified item'} is not an incident"}
    if row["lifecycle"] in ("done", "dismissed", "failed", "cancelled"):
        return {**base, "why": f"the item is {row['lifecycle']}"}
    stopped = policy.stop_state(connection, project_id)
    if stopped["stopped"]:
        return {**base, "why": f"agent work is stopped ({stopped['scope']})"}
    person = connection.execute(text(
        "SELECT decided_by FROM agent_act_decisions WHERE project_id = :p AND work_item_id = :i "
        "AND decided_by LIKE 'person:%' AND chosen = ANY(:c) ORDER BY created_at DESC LIMIT 1"),
        {"p": project_id, "i": item_id, "c": sorted(SQUAD_PERSON_CHOICES)}).scalar()
    verdict = policy.gate(connection, project_id, klass, "start_workflow")
    base["level"] = verdict["level"]
    if person:
        return {**base, "ok": True, "basis": "person", "by": str(person),
                "why": f"{person} approved the fix on the incident's card"}
    if verdict["allowed"] and not verdict["needs_person"]:
        return {**base, "ok": True, "basis": "level", "by": "agent:sre",
                "why": verdict["reason"]}
    return {**base, "why": verdict["reason"]}


def start_workflow_on_grant(
    connection: Connection,
    *,
    project_id: str,
    workflow_key: str,
    link: Optional[dict[str, Any]],
    actor: str,
    input_data: dict[str, Any],
    dedupe_key: str,
    door: str,
    door_id: str,
    door_name: str,
    start_anyway: Any = None,
    log_extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Start the current version of a workflow UNATTENDED, the one way every door does it: the
    same readiness preflight every start makes, on the standing grant `link` names
    ({on_behalf_of, grant_id}; None runs as BOSS itself), checked now. Never raises.

    {run_id, entry, word, lost_reason}: `entry` is what the event dispatcher records; a refusal
    is said where people look (record_refused) and in `word`. An event subscription and an
    alert rule's Respond automatically both start their workflow through here."""
    from .workflows import start_run

    extra = {"project_id": project_id, "workflow_key": workflow_key, "door": door,
             "door_id": door_id, **(log_extra or {})}
    authority: Optional[dict[str, Any]] = None
    if workflow_key in PER_RULE_ONLY and door == SQUAD_DOOR:
        authority = squad_authority(connection, project_id=project_id, item_id=door_id)
        if not authority["ok"]:
            logger.warning("event_dispatch.squad_refused", extra={**extra,
                                                                  "why": authority["why"]})
            return {"run_id": None, "lost_reason": None, "word": SQUAD_WORD,
                    "authority": authority,
                    "entry": {"kind": "workflow", "refused": True, "workflow_key": workflow_key,
                              "reason": "squad_not_authorised", "why": authority["why"]}}
        logger.info("event_dispatch.squad_authorised",
                    extra={**extra, "basis": authority["basis"], "by": authority["by"]})
    elif workflow_key in PER_RULE_ONLY and door != PER_RULE_DOOR:
        logger.warning("event_dispatch.per_rule_only", extra=extra)
        return {"run_id": None, "lost_reason": None, "word": PER_RULE_WORD,
                "entry": {"kind": "workflow", "refused": True, "workflow_key": workflow_key,
                          "reason": "per_rule_only"}}
    active = connection.execute(
        text("SELECT active_version_id FROM workflow_definitions "
             "WHERE project_id = :p AND workflow_key = :k"),
        {"p": project_id, "k": workflow_key},
    ).scalar()
    if not active:
        # Said once, with the door, rather than raised: one misconfigured door must not stop
        # the others.
        logger.warning("event_dispatch.workflow_missing", extra=extra)
        return {"run_id": None, "entry": None, "lost_reason": None,
                "word": f"There is no published {workflow_key} workflow here"}
    from .workflow_readiness import (
        WorkflowNotReadyError, record_refused, record_started_anyway, refuse_unless_ready,
    )
    from boss_engine.identity import consent as _consent
    from boss_engine.identity import lineage as _lineage

    try:
        # THE SAME PREFLIGHT EVERY START DOOR MAKES. A start this project cannot finish is
        # refused here rather than started and failed at its last step — unless the operator
        # accepted starting without it when they set the door up (`start_anyway`).
        try:
            readiness = refuse_unless_ready(
                connection, None, project_id, version_id=str(active), input_data=input_data,
                start_anyway=[str(k) for k in start_anyway]
                if isinstance(start_anyway, list) else [])
        except WorkflowNotReadyError as refused:
            # SAID WHERE PEOPLE LOOK, not only in a log: a door that started runs yesterday
            # must not silently stop starting anything.
            unaccepted = {**refused.readiness, "missing": [
                m for m in refused.readiness["missing"]
                if m["key"] not in refused.readiness.get("started_anyway", [])]}
            record = record_refused(
                connection, project_id, actor, door=door, door_id=door_id,
                door_name=door_name, workflow_key=workflow_key, readiness=unaccepted)
            missing = [m["key"] for m in record["missing"]]
            return {"run_id": None, "lost_reason": None,
                    "entry": {"kind": "workflow", "refused": True, "workflow_key": workflow_key,
                              "missing": missing},
                    "word": "Not started: this project is missing " + ", ".join(missing)}
        # ON THE GRANT OF WHOEVER SET IT UP, checked at EVERY start like a schedule's firing:
        # an ended grant stops the door with "Renew authority" instead of running on stale
        # authority.
        acting, lost_reason, run = None, None, None
        try:
            if link and link.get("on_behalf_of"):
                acting = _consent.schedule_acting(connection, link)
            with (_lineage.acting(acting) if acting is not None else _lineage.nullcontext()):
                run = start_run(
                    connection,
                    project_id=project_id,
                    actor=str((acting.principal_id if acting is not None else None) or actor),
                    input_data=input_data,
                    dedupe_key=dedupe_key,
                    workflow_version_id=str(active),
                )
        except _consent.AuthorityLost as lost:
            lost_reason = lost.reason
        if lost_reason is not None:
            return {"run_id": None, "lost_reason": lost_reason,
                    "entry": {"kind": "workflow", "refused": True, "workflow_key": workflow_key,
                              "reason": f"authority_lost:{lost_reason}"},
                    "word": "Renew authority: the grant it runs on has ended"}
        if not run.get("duplicate"):
            record_started_anyway(connection, project_id, actor, str(run.get("id") or ""),
                                  readiness)
        return {"run_id": run.get("id"), "lost_reason": None, "word": "Started",
                "authority": authority,
                "entry": {"kind": "workflow", "run_id": run.get("id"),
                          "workflow_key": workflow_key}}
    except Exception:  # noqa: BLE001 — one door must not take the others down
        logger.warning("event_dispatch.workflow_start_failed", extra=extra, exc_info=True)
        return {"run_id": None, "entry": None, "lost_reason": None,
                "word": "Could not start the workflow"}


def dispatch_event(
    connection: Connection,
    *,
    event_id: str,
    project_id: str,
    event_type: str,
    actor: str,
    entity_type: str,
    entity_id: str,
    data: dict[str, Any],
) -> list[dict[str, Any]]:
    """Fan an allowlisted event out to durable, deduplicated lease jobs."""
    if event_type not in DISPATCHABLE_EVENT_TYPES:
        return []

    event_boundary = _event_boundary(data)
    enqueued: list[dict[str, Any]] = []
    for subscription in _enabled_subscriptions(connection, project_id):
        if not event_type_matches(subscription["event_type"], event_type):
            continue
        if subscription["sink_ref"] == "docs.update":
            # The docs consumer has a deliberately narrower, operator-configurable
            # completion allowlist than the dispatcher as a whole.
            from ..config import get_settings
            from .docs_agent import docs_trigger_events

            if event_type not in docs_trigger_events(get_settings()):
                continue
        sink_boundary = normalize_data_boundary(subscription["data_boundary"])
        if not _can_cross_boundary(event_boundary, sink_boundary):
            logger.warning(
                "event_dispatch.boundary_blocked",
                extra={
                    "project_id": project_id,
                    "event_id": event_id,
                    "event_type": event_type,
                    "subscription_id": subscription["id"],
                    "event_boundary": event_boundary,
                    "sink_boundary": sink_boundary,
                },
            )
            continue
        config = subscription.get("config") or {}
        dedupe_hash = hashlib.sha256(
            f"{subscription['id']}:{event_id}".encode("utf-8")
        ).hexdigest()
        if subscription["sink_kind"] == "webhook_out":
            # Local imports avoid a module cycle through jobs -> core -> dispatcher.
            from ..config import get_settings
            from .webhooks import WebhookService

            delivery = WebhookService(get_settings()).enqueue_delivery(
                connection,
                project_id,
                subscription["sink_ref"],
                event_type,
                {
                    "actor": actor,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "event_data": data,
                },
                f"event-dispatch:{dedupe_hash}",
                event_id=event_id,
                data_boundary=sink_boundary,
                priority=int(config.get("priority", 100)),
                max_attempts=int(config.get("max_attempts", 3)),
                actor="event-dispatcher",
            )
            enqueued.append(delivery)
            continue
        if subscription["sink_kind"] == "workflow":
            # START THE OPERATOR'S OWN WORKFLOW. sink_ref is the workflow KEY, so the event
            # runs whatever version is current — the same rule a schedule follows.
            #
            # Deduped on the SUBSCRIPTION AND THE EVENT, like every other sink here: one
            # delivery of one event starts one run, however many times the dispatcher is
            # retried. The event's own data becomes the run's input, which is what makes
            # "a demo request arrived" carry the company with it.
            got = start_workflow_on_grant(
                connection, project_id=project_id, workflow_key=subscription["sink_ref"],
                link=(subscription if subscription.get("on_behalf_of") else None),
                actor=str(config.get("actor") or "event-dispatcher"),
                input_data={"event_type": event_type, "entity_type": entity_type,
                            "entity_id": entity_id, **(data or {})},
                dedupe_key=f"event:{dedupe_hash}", door="subscription",
                door_id=subscription["id"],
                door_name=f"The subscription to {subscription['event_type']}",
                start_anyway=config.get("start_anyway"), log_extra={"event_id": event_id})
            if got.get("lost_reason") is not None:
                connection.execute(text(
                    "UPDATE event_subscriptions SET enabled = false, updated_at = now(), "
                    "config = config || CAST(:c AS JSONB) WHERE id = :i"),
                    {"i": subscription["id"], "c": json.dumps(
                        {"last_error": "Renew authority: the standing grant this "
                                       f"subscription runs on has ended ({got['lost_reason']})."})})
                invalidate_subscription_cache(project_id)
            if got.get("entry") is not None:
                enqueued.append(got["entry"])
            continue
        if subscription["sink_kind"] != "job":
            logger.warning(
                "event_dispatch.unsupported_sink",
                extra={
                    "project_id": project_id,
                    "event_id": event_id,
                    "subscription_id": subscription["id"],
                    "sink_kind": subscription["sink_kind"],
                },
            )
            continue

        from .event_subscription_service import job_sink_held

        if job_sink_held(connection, subscription):
            # Re-checked at dispatch (rc6): a job sink no one's kind permission ever covered —
            # set up before the check existed — queues nothing until a person re-saves it.
            logger.warning(
                "event_dispatch.job_sink_held",
                extra={"project_id": project_id, "event_id": event_id,
                       "subscription_id": subscription["id"],
                       "sink_ref": subscription["sink_ref"]},
            )
            enqueued.append({"kind": "job", "held": True, "subscription_id": subscription["id"],
                             "sink_ref": subscription["sink_ref"],
                             "reason": "This job sink was set up before BOSS checked the "
                                       "permission its kind needs. Re-save or re-enable it "
                                       "(as someone who holds that permission) to start it "
                                       "again."})
            continue
        task_id = data.get("task_id")
        if subscription["sink_ref"] == "docs.update":
            from ..config import get_settings
            from .docs_agent import enqueue_docs_update_job

            job = enqueue_docs_update_job(
                connection,
                get_settings(),
                project_id=project_id,
                event_id=event_id,
                event_type=event_type,
                event_actor=actor,
                entity_type=entity_type,
                entity_id=entity_id,
                task_id=task_id if isinstance(task_id, str) else None,
                priority=int(config.get("priority", 100)),
                max_attempts=int(config.get("max_attempts", 3)),
                data_boundary=sink_boundary,
                subscription_id=subscription["id"],
            )
            enqueued.append(job)
            continue
        job = enqueue_job(
            connection,
            project_id,
            subscription["sink_ref"],
            {
                "event_id": event_id,
                "event_type": event_type,
                "event_actor": actor,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "subscription_id": subscription["id"],
                "event_data": data,
                "data_boundary": sink_boundary,
            },
            task_id=task_id if isinstance(task_id, str) else None,
            priority=int(config.get("priority", 100)),
            max_attempts=int(config.get("max_attempts", 3)),
            data_boundary=sink_boundary,
            dedupe_key=f"event-dispatch:{dedupe_hash}",
        )
        enqueued.append(job)
    return enqueued
