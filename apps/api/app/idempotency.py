"""ONE PRESS, ONE EFFECT — for every state-changing request, decided in one place.

The same bug kept coming back on a different button:

* 2026-08-22 the operator and the autopilot started the same run at once;
* 2026-08-30 "Open preview happened twice";
* 2026-09-10 duplicate chat actions;
* 2026-10-02 "Stop and run again" pressed twice started two workflows.

Each was fixed for that one endpoint, and the next button had the same hole, because nothing
in the platform said what a repeated request MEANS. This module says it once, for every
POST/PUT/PATCH/DELETE under /v1/: a caller that sends an ``Idempotency-Key`` header gets
exactly one execution per key.

* The first request with a key runs, and its status and body are stored.
* The same key with the same request returns the stored response; the handler does not run.
* The same key while the first is still running is refused with 409 — never run twice.
* The same key with a DIFFERENT request is refused with 422: a key names one intent.
* No header: exactly the behaviour before this existed. The worker, MCP clients and scripts
  that never heard of the header are untouched.

WHAT IS REMEMBERED, AND WHAT IS NOT. A 2xx and a deterministic 4xx are remembered: repeating
them would only repeat the same outcome. A 5xx, a 408/409/425/429, and a handler that raised
are NOT — the key is released so the same press can be retried. The product owner's fear about
idempotency was exactly this: "you'll never trigger a run". A first attempt that died on a
transient error must not pin that failure to the key for a day.

A key that is claimed but never finishes (the API process died mid-request) is taken over once
``IN_PROGRESS_TAKEOVER`` has passed, for the same reason: a crash must not lock the intent out.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text

HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotency-Replayed"
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MAX_KEY_LENGTH = 255
_KEY_SHAPE = re.compile(r"^[\x21-\x7e]{1,255}$")  # visible ASCII, no spaces
RETENTION = "24 hours"
IN_PROGRESS_TAKEOVER = "10 minutes"
# A body larger than this is still served, but not remembered: the key is released instead.
MAX_STORED_BODY_BYTES = 1_048_576

# Statuses that describe the request rather than the moment. Everything else releases the key.
_TRANSIENT_4XX = frozenset({408, 409, 425, 429})

# Bodies that are a stream rather than a document. They cannot be buffered to be remembered.
_STREAMING_CONTENT_TYPES = ("text/event-stream", "application/x-ndjson")


# THE ONLY ROUTES THE HEADER DOES NOT GOVERN, EACH WITH ITS REASON.
#
# Matched against the route-relative path, so a pattern written for the template
# (`/v1/previews/{preview_id}/web/{path:path}`) matches the live path too. The enforcing test
# (test_every_mutating_route_is_idempotent_or_says_why.py) enumerates every mutating route the
# app mounts and fails if one is neither covered nor listed here, and fails if an entry here no
# longer matches any route — an exclusion nobody can point at is a hole waiting for a route.
EXCLUSIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^/v1/delivery-workflows/agreement$"),
     "This authenticated POST is a deterministic read of the current work agreement; the brief "
     "stays out of URLs, and every preview must re-read canonical state without creating replay rows."),
    (re.compile(r"^/v1/worker(/|$)"),
     "The worker lease protocol carries its own fencing: a lease id and attempt number make a "
     "heartbeat or completion idempotent already, and replaying a STORED heartbeat would tell a "
     "worker it still holds a lease the server has since taken back."),
    (re.compile(r"^/v1/previews/[^/]+/web(/|$)"),
     "The preview capability proxy forwards the previewed APP's own traffic. An Idempotency-Key "
     "on those requests belongs to that app's API, not to BOSS, and must reach it untouched."),
)


def exclusion_reason(method: str, route_path: str) -> Optional[str]:
    """Why this request is not governed by the header, or None when it is."""
    if method.upper() not in MUTATING_METHODS or not route_path.startswith("/v1/"):
        return "not a state-changing /v1/ request"
    for pattern, reason in EXCLUSIONS:
        if pattern.search(route_path):
            return reason
    return None


def valid_key(key: str) -> bool:
    return bool(_KEY_SHAPE.match(key))


def request_fingerprint(method: str, route_path: str, query: str, body: bytes) -> str:
    """What "the same request" means: method, path, query and the exact body bytes."""
    digest = hashlib.sha256()
    for part in (method.upper().encode(), route_path.encode(), query.encode()):
        digest.update(part)
        digest.update(b"\x00")
    digest.update(body)
    return digest.hexdigest()


def is_streaming(content_type: str) -> bool:
    lowered = (content_type or "").lower()
    return any(lowered.startswith(kind) for kind in _STREAMING_CONTENT_TYPES)


# Headers a replay must NOT repeat. Length and type are re-derived from the stored body; a
# cookie is a credential issued to the first response's moment, never something to hand out
# again from storage; and hop-by-hop headers describe one connection, not the answer.
_NOT_REPLAYED_HEADERS = frozenset({
    "content-length", "content-type", "set-cookie", "connection", "keep-alive",
    "transfer-encoding", "date", "server", REPLAYED_HEADER.lower(),
})


def replayable_headers(raw_headers) -> list[list[str]]:
    """The route's own headers, as a replay must repeat them.

    Review of 45e7693: a replay sent only the body and its content type, so a second press of a
    download came back without its Content-Disposition — a nameless blob instead of the file the
    first press saved. Found by test_a_replayed_write_looks_like_the_first.py.
    """
    kept = []
    for name, value in raw_headers:
        name_s = name.decode("latin-1") if isinstance(name, bytes) else str(name)
        value_s = value.decode("latin-1") if isinstance(value, bytes) else str(value)
        if name_s.lower() not in _NOT_REPLAYED_HEADERS:
            kept.append([name_s, value_s])
    return kept


def remembers(status_code: int) -> bool:
    """Whether an outcome belongs to the key, or the key should be free to try again."""
    if 200 <= status_code < 300:
        return True
    return 400 <= status_code < 500 and status_code not in _TRANSIENT_4XX


@dataclass(frozen=True)
class Claim:
    """The result of presenting a key: run the handler, replay, or refuse."""

    outcome: str  # "run" | "replay" | "in_progress" | "mismatch"
    status_code: int = 0
    body: bytes = b""
    content_type: Optional[str] = None
    headers: tuple[tuple[str, str], ...] = ()


def claim(connection, *, principal: str, key: str, method: str, path: str,
          fingerprint: str) -> Claim:
    """Take the key for this request, or say what the earlier request with it decided.

    The INSERT ... ON CONFLICT DO NOTHING is the arbiter: of two identical requests racing, the
    database lets exactly one insert, and the other reads the in-progress row and gets 409.
    """
    # Expired rows and abandoned claims for THIS key are cleared first, so a key is reusable
    # after its window and a crashed request does not lock its intent out.
    connection.execute(text(f"""
        DELETE FROM api_idempotency_keys
        WHERE principal = :principal AND idempotency_key = :key
          AND NOT durable_effect
          AND (expires_at < now()
               OR (state = 'in_progress'
                   AND created_at < now() - interval '{IN_PROGRESS_TAKEOVER}'))
    """), {"principal": principal, "key": key})
    # Twice at most: a first attempt that failed transiently releases its key, and the second
    # pass lets this request take it instead of being told to wait for a request that is gone.
    for _ in range(2):
        inserted = connection.execute(text(f"""
            INSERT INTO api_idempotency_keys (
                principal, idempotency_key, method, path, request_hash, state, expires_at
            ) VALUES (
                :principal, :key, :method, :path, :hash, 'in_progress',
                now() + interval '{RETENTION}'
            )
            ON CONFLICT (principal, idempotency_key) DO NOTHING
            RETURNING idempotency_key
        """), {"principal": principal, "key": key, "method": method.upper(), "path": path,
               "hash": fingerprint}).first()
        if inserted is not None:
            # A cheap, bounded sweep so the table does not grow without limit on a busy install.
            connection.execute(text("""
                DELETE FROM api_idempotency_keys WHERE ctid IN (
                    SELECT ctid FROM api_idempotency_keys WHERE expires_at < now()
                    AND NOT durable_effect LIMIT 100)
            """))
            return Claim("run")
        row = connection.execute(text("""
            SELECT request_hash, state, status_code, response_body, content_type,
                   response_headers
            FROM api_idempotency_keys
            WHERE principal = :principal AND idempotency_key = :key
        """), {"principal": principal, "key": key}).mappings().first()
        if row is None:
            continue
        if row["request_hash"] != fingerprint:
            return Claim("mismatch")
        if row["state"] != "completed":
            return Claim("in_progress")
        stored = row["response_headers"] or []
        if isinstance(stored, str):
            stored = json.loads(stored)
        return Claim("replay", status_code=int(row["status_code"]),
                     body=bytes(row["response_body"] or b""), content_type=row["content_type"],
                     headers=tuple((str(n), str(v)) for n, v in stored))
    return Claim("in_progress")


def complete(connection, *, principal: str, key: str, status_code: int, body: bytes,
             content_type: Optional[str], headers: Optional[list[list[str]]] = None) -> None:
    connection.execute(text("""
        UPDATE api_idempotency_keys
        SET state = 'completed', status_code = :status, response_body = :body,
            content_type = :content_type, response_headers = CAST(:headers AS JSONB),
            completed_at = now()
        WHERE principal = :principal AND idempotency_key = :key
    """), {"principal": principal, "key": key, "status": status_code, "body": body,
           "content_type": content_type, "headers": json.dumps(headers or [])})


def release(connection, *, principal: str, key: str) -> None:
    connection.execute(text("""
        DELETE FROM api_idempotency_keys
        WHERE principal = :principal AND idempotency_key = :key AND state = 'in_progress' AND NOT durable_effect
    """), {"principal": principal, "key": key})


def protect(connection, *, principal: str, key: str, fingerprint: str) -> None:
    """Commit with the domain intent; uncertain external effects cannot time out into retries.

    Completed durable outcomes also keep their key: retention is not a new consent to spend.
    Only the voice routes opt in, after their existing admission checks. No network abort or
    provider success is inferred from this bookkeeping flag.
    """
    changed = connection.execute(text("""
        UPDATE api_idempotency_keys SET durable_effect = TRUE
        WHERE principal=:principal AND idempotency_key=:key AND state='in_progress'
          AND request_hash=:fingerprint
        RETURNING idempotency_key
    """), {"principal": principal, "key": key, "fingerprint": fingerprint}).first()
    if changed is None:
        raise RuntimeError("The original stream intent is no longer owned.")
