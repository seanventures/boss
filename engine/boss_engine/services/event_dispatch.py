from __future__ import annotations

import hashlib
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
            from .workflows import start_run

            active = connection.execute(
                text(
                    """
                    SELECT active_version_id FROM workflow_definitions
                    WHERE project_id = :p AND workflow_key = :k
                    """
                ),
                {"p": project_id, "k": subscription["sink_ref"]},
            ).scalar()
            if not active:
                # Said once, with the subscription, rather than raised: one misconfigured
                # subscription must not stop the other sinks for this event.
                logger.warning(
                    "event_dispatch.workflow_missing",
                    extra={"project_id": project_id, "event_id": event_id,
                           "subscription_id": subscription["id"],
                           "workflow_key": subscription["sink_ref"]},
                )
                continue
            try:
                run = start_run(
                    connection,
                    project_id=project_id,
                    actor=str(config.get("actor") or "event-dispatcher"),
                    input_data={"event_type": event_type, "entity_type": entity_type,
                                "entity_id": entity_id, **(data or {})},
                    dedupe_key=f"event:{dedupe_hash}",
                    workflow_version_id=str(active),
                )
                enqueued.append({"kind": "workflow", "run_id": run.get("id"),
                                 "workflow_key": subscription["sink_ref"]})
            except Exception:  # noqa: BLE001 — one sink must not take the others down
                logger.warning(
                    "event_dispatch.workflow_start_failed",
                    extra={"project_id": project_id, "event_id": event_id,
                           "subscription_id": subscription["id"]},
                    exc_info=True,
                )
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
