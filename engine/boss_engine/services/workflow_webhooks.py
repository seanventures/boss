"""Starting a workflow because another system said so: signed inbound webhooks (#512).

WHY THIS EXISTS
---------------
A workflow could start on demand, on a schedule, or on one of BOSS's own events. A form
submission, a payment provider, a CI system or a shop could not start one at all: there was no
door for a system outside BOSS that has no account here. This is that door, built on the same
start path a schedule and an event subscription use (`workflows.start_run`, preflighted by
`workflow_readiness.refuse_unless_ready`).

HOW A DELIVERY IS TRUSTED
-------------------------
Every trigger has two things the sender needs:

* an unguessable URL: ``/v1/webhooks/inbound/workflow/<path_token>``. The token finds the
  trigger; it is not what authenticates the request.
* a signing secret, shown ONCE when the trigger is made and once on each rotation. It lives in
  the secret backend (`secrets_service.put_secret`), never in a column, an event or a log.

The sender signs ``<timestamp>.<raw body>`` with HMAC-SHA256 and sends::

    X-BOSS-Timestamp: <unix seconds>
    X-BOSS-Signature: v1=<hex digest>
    X-BOSS-Delivery:  <its own id for this delivery>   (optional; the dedupe key)

The timestamp is inside the signature, so a captured request cannot be replayed once it is
older than `TIMESTAMP_TOLERANCE_SECONDS`; within that window the dedupe key stops it starting a
second run. Without a delivery id the dedupe key is the hash of the signed timestamp and body,
so an identical replay is still one run.

EVERY REFUSAL IS VISIBLE
------------------------
A delivery that reaches a trigger is recorded whatever happens to it: `started`, `duplicate`, or
`refused` with its reason (disabled, too_large, unsupported_content_type, missing_signature,
stale_timestamp, bad_signature, invalid_json, not_ready, rate_limited, start_failed). The
owner reads them on the trigger. Refusals are recorded at most `REFUSED_RECORD_LIMIT` a minute
per trigger; past that they are COUNTED on the trigger (`refused_unrecorded`) rather than
itemised, so someone hammering the URL with bad signatures cannot fill the table — and still
shows up.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id

#: The public path a sender POSTs to. Under /v1/webhooks/inbound/ because that prefix is already
#: the one place signed machine callbacks are exempt from transport auth.
INBOUND_PATH_PREFIX = "/v1/webhooks/inbound/workflow/"

SIGNATURE_HEADER = "x-boss-signature"
TIMESTAMP_HEADER = "x-boss-timestamp"
DELIVERY_HEADER = "x-boss-delivery"
SIGNATURE_SCHEME = "v1"

#: How far a delivery's timestamp may be from now, either way, in seconds.
TIMESTAMP_TOLERANCE_SECONDS = 300
#: The largest body a trigger accepts. A workflow's input is a form, not a file upload.
MAX_BODY_BYTES = 256 * 1024
#: Runs one trigger may start in a minute. A loop in the sender is not a reason to spend.
START_LIMIT_PER_MINUTE = 60
#: Refused deliveries itemised per trigger per minute; the rest are counted.
REFUSED_RECORD_LIMIT = 30
MAX_DELIVERY_ID_LENGTH = 200
RECENT_DELIVERIES = 50

REFUSAL_REASONS = (
    "disabled", "too_large", "unsupported_content_type", "missing_signature",
    "stale_timestamp", "bad_signature", "invalid_json", "not_ready", "rate_limited",
    "start_failed", "workflow_missing",
)

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}$")


class WebhookTriggerError(ValueError):
    """Something about the trigger request is wrong. The message is shown to the operator."""


class WebhookTriggerNotFound(LookupError):
    """No trigger with that id in this project (never says whether one exists elsewhere)."""


# ── Signing ───────────────────────────────────────────────────────────────────


def sign(secret: str, timestamp: int | str, raw_body: bytes) -> str:
    """The header value a sender puts in X-BOSS-Signature. Used by tests and the docs' example."""
    message = str(timestamp).encode("ascii") + b"." + raw_body
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_SCHEME}={digest}"


def _signature_matches(secret: str, timestamp: str, raw_body: bytes, header: str) -> bool:
    expected = sign(secret, timestamp, raw_body)
    # A header may carry several signatures (comma-separated) while a sender rotates; any match.
    for candidate in (part.strip() for part in header.split(",")):
        if candidate and hmac.compare_digest(candidate, expected):
            return True
    return False


def _new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def _store_secret(connection: Connection, project_id: str, trigger_id: str, value: str,
                  actor: str) -> dict[str, Any]:
    """Write the signing secret THROUGH to the secret backend; keep only the reference.

    The backend key is derived from the trigger id, so a rotation overwrites the old value in
    place: the old secret stops verifying the moment the new one is stored.
    """
    from ..config import get_settings
    from .secret_backends import default_secret_backend
    from .secrets_service import put_secret

    settings = get_settings()
    return put_secret(
        connection, settings, project_id=project_id, name=f"workflow_webhook_{trigger_id}",
        value=value, secret_kind="api_token", backend=default_secret_backend(settings),
        actor=actor)


def _read_secret(connection: Connection, project_id: str, secret_ref_id: str) -> str:
    from ..config import get_settings
    from .secret_backends import resolve_secret_ref

    return resolve_secret_ref(connection, secret_ref_id, get_settings(), project_id=project_id)


# ── Management (owner side) ───────────────────────────────────────────────────


def inbound_path(path_token: str) -> str:
    return f"{INBOUND_PATH_PREFIX}{path_token}"


def _describe(row: Mapping[str, Any]) -> dict[str, Any]:
    """A trigger for a screen. Never the secret: only whether it is set and its last four."""
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "workflow_key": row["workflow_key"],
        "name": row["name"],
        "path": inbound_path(row["path_token"]),
        "enabled": bool(row["enabled"]),
        "secret_last4": row.get("secret_last4"),
        "secret_rotated_at": row.get("secret_rotated_at"),
        "refused_unrecorded": int(row.get("refused_unrecorded") or 0),
        "last_delivery_at": row.get("last_delivery_at"),
        "created_at": row.get("created_at"),
        "actor": row["actor"],
    }


def _row(connection: Connection, project_id: str, trigger_id: str,
         *, lock: bool = False) -> dict[str, Any]:
    found = connection.execute(
        text("SELECT * FROM workflow_webhook_triggers WHERE id = :i AND project_id = :p"
             + (" FOR UPDATE" if lock else "")),
        {"i": trigger_id, "p": project_id},
    ).mappings().first()
    if found is None:
        raise WebhookTriggerNotFound(f"no webhook trigger '{trigger_id}'")
    return dict(found)


def create(connection: Connection, *, project_id: str, workflow_key: str, name: str,
           actor: str) -> dict[str, Any]:
    """Make a trigger. The ONLY response besides `rotate` that carries the secret."""
    workflow_key = str(workflow_key or "").strip()
    if not workflow_key:
        raise WebhookTriggerError("workflow_key is required.")
    exists = connection.execute(
        text("SELECT 1 FROM workflow_definitions WHERE project_id = :p AND workflow_key = :k"),
        {"p": project_id, "k": workflow_key},
    ).scalar()
    if not exists:
        raise WebhookTriggerError(f"no workflow '{workflow_key}' in this project.")
    name = str(name or "").strip() or "Webhook"
    if not _NAME.match(name):
        raise WebhookTriggerError(
            "name must be 1-80 letters, numbers, spaces, dots, dashes or underscores.")
    taken = connection.execute(
        text("SELECT 1 FROM workflow_webhook_triggers WHERE project_id = :p AND name = :n"),
        {"p": project_id, "n": name},
    ).scalar()
    if taken:
        raise WebhookTriggerError(f"a webhook trigger named '{name}' already exists.")

    trigger_id = make_id("wht")
    secret = _new_secret()
    stored = _store_secret(connection, project_id, trigger_id, secret, actor)
    connection.execute(
        text("""
            INSERT INTO workflow_webhook_triggers
                (id, project_id, workflow_key, name, path_token, secret_ref_id, secret_last4,
                 actor, enabled)
            VALUES (:id, :p, :k, :n, :tok, :ref, :last4, :actor, TRUE)
        """),
        {"id": trigger_id, "p": project_id, "k": workflow_key, "n": name,
         "tok": secrets.token_urlsafe(24), "ref": stored["secret_ref_id"],
         "last4": secret[-4:], "actor": actor},
    )
    emit_event(connection, project_id, "workflow.webhook_created", actor,
               "workflow_webhook_trigger", trigger_id,
               {"workflow_key": workflow_key, "name": name})
    return {**_describe(_row(connection, project_id, trigger_id)), "secret": secret}


def list_triggers(connection: Connection, project_id: str,
                  workflow_key: Optional[str] = None) -> list[dict[str, Any]]:
    rows = connection.execute(
        text("""
            SELECT * FROM workflow_webhook_triggers
            WHERE project_id = :p AND (CAST(:k AS TEXT) IS NULL OR workflow_key = :k)
            ORDER BY created_at
        """),
        {"p": project_id, "k": workflow_key or None},
    ).mappings().all()
    return [_describe(r) for r in rows]


def rotate(connection: Connection, *, project_id: str, trigger_id: str,
           actor: str) -> dict[str, Any]:
    """A new secret, returned once. The old one stops verifying immediately."""
    row = _row(connection, project_id, trigger_id, lock=True)
    secret = _new_secret()
    stored = _store_secret(connection, project_id, trigger_id, secret, actor)
    connection.execute(
        text("""
            UPDATE workflow_webhook_triggers
            SET secret_ref_id = :ref, secret_last4 = :last4, secret_rotated_at = now(),
                updated_at = now()
            WHERE id = :i AND project_id = :p
        """),
        {"ref": stored["secret_ref_id"], "last4": secret[-4:], "i": row["id"], "p": project_id},
    )
    emit_event(connection, project_id, "workflow.webhook_secret_rotated", actor,
               "workflow_webhook_trigger", trigger_id, {"name": row["name"]})
    return {**_describe(_row(connection, project_id, trigger_id)), "secret": secret}


def set_enabled(connection: Connection, *, project_id: str, trigger_id: str, enabled: bool,
                actor: str) -> dict[str, Any]:
    row = _row(connection, project_id, trigger_id, lock=True)
    connection.execute(
        text("UPDATE workflow_webhook_triggers SET enabled = :e, updated_at = now() "
             "WHERE id = :i AND project_id = :p"),
        {"e": bool(enabled), "i": row["id"], "p": project_id},
    )
    emit_event(connection, project_id,
               "workflow.webhook_enabled" if enabled else "workflow.webhook_disabled", actor,
               "workflow_webhook_trigger", trigger_id, {"name": row["name"]})
    return _describe(_row(connection, project_id, trigger_id))


def deliveries(connection: Connection, *, project_id: str, trigger_id: str,
               limit: int = RECENT_DELIVERIES) -> dict[str, Any]:
    row = _row(connection, project_id, trigger_id)
    items = connection.execute(
        text("""
            SELECT id, delivery_id, status, reason, run_id, body_bytes, received_at
            FROM workflow_webhook_deliveries
            WHERE trigger_id = :t AND project_id = :p
            ORDER BY received_at DESC, id DESC
            LIMIT :n
        """),
        {"t": trigger_id, "p": project_id, "n": max(1, min(int(limit), 200))},
    ).mappings().all()
    return {"trigger": _describe(row), "deliveries": [dict(i) for i in items]}


# ── The inbound door ──────────────────────────────────────────────────────────


def _record(connection: Connection, trigger: Mapping[str, Any], *, status: str,
            reason: Optional[str] = None, delivery_id: Optional[str] = None,
            dedupe_key: Optional[str] = None, run_id: Optional[str] = None,
            raw_body: bytes = b"") -> Optional[str]:
    """One delivery row. A refusal past the per-minute limit is counted, not itemised."""
    if status == "refused":
        recent = connection.execute(
            text("""
                SELECT count(*) FROM workflow_webhook_deliveries
                WHERE trigger_id = :t AND status = 'refused'
                  AND received_at > now() - interval '1 minute'
            """),
            {"t": trigger["id"]},
        ).scalar() or 0
        if recent >= REFUSED_RECORD_LIMIT:
            connection.execute(
                text("UPDATE workflow_webhook_triggers "
                     "SET refused_unrecorded = refused_unrecorded + 1 WHERE id = :t"),
                {"t": trigger["id"]},
            )
            return None
    record_id = make_id("whd")
    connection.execute(
        text("""
            INSERT INTO workflow_webhook_deliveries
                (id, trigger_id, project_id, delivery_id, dedupe_key, status, reason, run_id,
                 body_bytes, body_sha256)
            VALUES (:id, :t, :p, :d, :k, :s, :r, :run, :n, :h)
            ON CONFLICT DO NOTHING
        """),
        {"id": record_id, "t": trigger["id"], "p": trigger["project_id"], "d": delivery_id,
         "k": dedupe_key, "s": status, "r": reason, "run": run_id, "n": len(raw_body),
         "h": hashlib.sha256(raw_body).hexdigest() if raw_body else None},
    )
    connection.execute(
        text("UPDATE workflow_webhook_triggers SET last_delivery_at = now() WHERE id = :t"),
        {"t": trigger["id"]},
    )
    return record_id


def _refuse(connection: Connection, trigger: Mapping[str, Any], reason: str, *,
            raw_body: bytes = b"", delivery_id: Optional[str] = None,
            detail: Optional[str] = None, extra: Optional[dict] = None) -> dict[str, Any]:
    _record(connection, trigger, status="refused", reason=reason, delivery_id=delivery_id,
            raw_body=raw_body)
    return {"status": "refused", "reason": reason, "detail": detail or reason.replace("_", " "),
            **(extra or {})}


def receive(connection: Connection, path_token: str, raw_body: bytes,
            headers: Mapping[str, str], *, now: Optional[float] = None,
            body_too_large: bool = False) -> dict[str, Any]:
    """Verify one inbound delivery and, if it is good, start the trigger's workflow.

    `headers` are lower-cased. `body_too_large` is the route saying it stopped reading at
    `MAX_BODY_BYTES`, so the refusal is recorded without the body ever being held whole.

    Returns ``{"status": "not_found" | "refused" | "started" | "duplicate", ...}``. Never raises
    for anything a sender controls.
    """
    token = str(path_token or "").strip()
    trigger = None
    if token and len(token) <= 128:
        trigger = connection.execute(
            text("SELECT * FROM workflow_webhook_triggers WHERE path_token = :t FOR UPDATE"),
            {"t": token},
        ).mappings().first()
    if trigger is None:
        # Nobody to show it to; and saying more than "not found" would help a guesser.
        return {"status": "not_found"}
    trigger = dict(trigger)

    delivery_id = str(headers.get(DELIVERY_HEADER) or "").strip()[:MAX_DELIVERY_ID_LENGTH] or None
    if body_too_large or len(raw_body) > MAX_BODY_BYTES:
        return _refuse(connection, trigger, "too_large", delivery_id=delivery_id,
                       detail=f"body is larger than {MAX_BODY_BYTES} bytes")
    if not trigger["enabled"]:
        return _refuse(connection, trigger, "disabled", raw_body=raw_body,
                       delivery_id=delivery_id, detail="this webhook trigger is disabled")
    content_type = str(headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type != "application/json":
        return _refuse(connection, trigger, "unsupported_content_type", raw_body=raw_body,
                       delivery_id=delivery_id, detail="Content-Type must be application/json")

    signature = str(headers.get(SIGNATURE_HEADER) or "").strip()
    timestamp = str(headers.get(TIMESTAMP_HEADER) or "").strip()
    if not signature or not timestamp:
        return _refuse(connection, trigger, "missing_signature", raw_body=raw_body,
                       delivery_id=delivery_id,
                       detail="X-BOSS-Signature and X-BOSS-Timestamp are required")
    try:
        sent_at = int(timestamp)
    except ValueError:
        return _refuse(connection, trigger, "stale_timestamp", raw_body=raw_body,
                       delivery_id=delivery_id, detail="X-BOSS-Timestamp must be unix seconds")
    current = time.time() if now is None else now
    if abs(current - sent_at) > TIMESTAMP_TOLERANCE_SECONDS:
        return _refuse(connection, trigger, "stale_timestamp", raw_body=raw_body,
                       delivery_id=delivery_id,
                       detail=f"timestamp is more than {TIMESTAMP_TOLERANCE_SECONDS}s from now")
    try:
        secret = _read_secret(connection, trigger["project_id"], trigger["secret_ref_id"])
    except Exception:  # noqa: BLE001 — an unreadable secret verifies nothing; say so, never 500
        return _refuse(connection, trigger, "bad_signature", raw_body=raw_body,
                       delivery_id=delivery_id,
                       detail="the signing secret could not be read; rotate it")
    if not _signature_matches(secret, timestamp, raw_body, signature):
        return _refuse(connection, trigger, "bad_signature", raw_body=raw_body,
                       delivery_id=delivery_id, detail="signature does not match")

    try:
        payload = json.loads(raw_body)
    except (ValueError, UnicodeDecodeError):
        payload = None
    if not isinstance(payload, dict):
        return _refuse(connection, trigger, "invalid_json", raw_body=raw_body,
                       delivery_id=delivery_id, detail="body must be a JSON object")

    dedupe_key = (f"id:{delivery_id}" if delivery_id else
                  "sig:" + hashlib.sha256(timestamp.encode() + b"." + raw_body).hexdigest())
    run_dedupe = f"webhook:{trigger['id']}:{hashlib.sha256(dedupe_key.encode()).hexdigest()}"

    already = connection.execute(
        text("""
            SELECT run_id FROM workflow_webhook_deliveries
            WHERE trigger_id = :t AND dedupe_key = :k AND status = 'started'
        """),
        {"t": trigger["id"], "k": dedupe_key},
    ).scalar()
    if already:
        _record(connection, trigger, status="duplicate", delivery_id=delivery_id,
                dedupe_key=dedupe_key, run_id=already, raw_body=raw_body)
        return {"status": "duplicate", "run_id": already, "delivery_id": delivery_id}

    started_recently = connection.execute(
        text("""
            SELECT count(*) FROM workflow_webhook_deliveries
            WHERE trigger_id = :t AND status = 'started'
              AND received_at > now() - interval '1 minute'
        """),
        {"t": trigger["id"]},
    ).scalar() or 0
    if started_recently >= START_LIMIT_PER_MINUTE:
        return _refuse(connection, trigger, "rate_limited", raw_body=raw_body,
                       delivery_id=delivery_id,
                       detail=f"more than {START_LIMIT_PER_MINUTE} runs a minute")

    return _start(connection, trigger, payload, raw_body=raw_body, delivery_id=delivery_id,
                  dedupe_key=dedupe_key, run_dedupe=run_dedupe)


def _start(connection: Connection, trigger: Mapping[str, Any], payload: dict[str, Any], *,
           raw_body: bytes, delivery_id: Optional[str], dedupe_key: str,
           run_dedupe: str) -> dict[str, Any]:
    """The same start an event subscription makes: current version, preflight, deduped run."""
    from .workflow_readiness import (
        WorkflowNotReadyError, record_refused, record_started_anyway, refuse_unless_ready,
    )
    from .workflows import start_run

    project_id = trigger["project_id"]
    active = connection.execute(
        text("SELECT active_version_id FROM workflow_definitions "
             "WHERE project_id = :p AND workflow_key = :k"),
        {"p": project_id, "k": trigger["workflow_key"]},
    ).scalar()
    if not active:
        return _refuse(connection, trigger, "workflow_missing", raw_body=raw_body,
                       delivery_id=delivery_id,
                       detail=f"'{trigger['workflow_key']}' has no published version to run")
    # THE PAYLOAD IS THE RUN'S INPUT, beside where it came from — the event door's binding,
    # with the trigger in place of the event type.
    input_data = {"trigger": "webhook", "webhook_trigger_id": trigger["id"],
                  "delivery_id": delivery_id, **payload}
    # A savepoint, so a start that fails part-way leaves the refusal row and nothing else.
    savepoint = connection.begin_nested()
    try:
        readiness = refuse_unless_ready(connection, None, project_id, version_id=str(active),
                                        input_data=input_data)
        run = start_run(connection, project_id=project_id, actor=trigger["actor"],
                        input_data=input_data, dedupe_key=run_dedupe,
                        workflow_version_id=str(active))
        savepoint.commit()
    except WorkflowNotReadyError as refused:
        savepoint.rollback()
        record = record_refused(
            connection, project_id, trigger["actor"], door="workflow_webhook_trigger",
            door_id=trigger["id"], door_name=f"The webhook trigger {trigger['name']}",
            workflow_key=trigger["workflow_key"], readiness=refused.readiness)
        return _refuse(connection, trigger, "not_ready", raw_body=raw_body,
                       delivery_id=delivery_id,
                       detail="the workflow cannot run in this project yet",
                       extra={"missing": [m["key"] for m in record["missing"]]})
    except Exception as error:  # noqa: BLE001 — recorded and answered, never silent
        savepoint.rollback()
        emit_event(connection, project_id, "workflow.webhook_start_failed", trigger["actor"],
                   "workflow_webhook_trigger", trigger["id"],
                   {"error": type(error).__name__, "delivery_id": delivery_id})
        return _refuse(connection, trigger, "start_failed", raw_body=raw_body,
                       delivery_id=delivery_id, detail="the run could not be started")

    run_id = str(run.get("id") or "")
    duplicate = bool(run.get("duplicate"))
    if not duplicate:
        record_started_anyway(connection, project_id, trigger["actor"], run_id, readiness)
    _record(connection, trigger, status="duplicate" if duplicate else "started",
            delivery_id=delivery_id, dedupe_key=dedupe_key, run_id=run_id, raw_body=raw_body)
    emit_event(connection, project_id, "workflow.webhook_delivered", trigger["actor"],
               "workflow_webhook_trigger", trigger["id"],
               {"run_id": run_id, "delivery_id": delivery_id, "duplicate": duplicate})
    return {"status": "duplicate" if duplicate else "started", "run_id": run_id,
            "delivery_id": delivery_id}
