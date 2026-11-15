"""The generic signed webhook (P9a): two signature schemes, one envelope.

STANDARD WEBHOOKS (https://www.standardwebhooks.com/)
    webhook-id:        <sender's delivery id>          (the dedupe key)
    webhook-timestamp: <unix seconds>                  (5-minute window)
    webhook-signature: v1,<base64 HMAC-SHA256(key, "<id>.<timestamp>.<body>")> [v1,<sig2> ...]
    secret: whsec_<base64 key>  — several signatures while a sender rotates; any one matches.

BOSS v1 (the scheme BOSS's own inbound workflow/agent webhooks already use, workflow_webhooks.py)
    X-BOSS-Timestamp: <unix seconds>
    X-BOSS-Signature: v1=<hex HMAC-SHA256(secret, "<timestamp>.<body>")>[, v1=<hex2>]
    X-BOSS-Delivery:  <sender's id>                    (optional; else sha256 of "<ts>.<body>")

The body is JSON. A CloudEvents 1.0 body (specversion + id + source + type) is carried through
as the envelope; any other object becomes `com.boss.webhook.received` with the body as `data`
(a top-level `type`/`subject`/`id`/`time` the sender chose is honoured). Verification reads the
raw bytes first; nothing is parsed before the signature matches.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from typing import Any, Optional

from ..base import IntakeAdapterBase
from ..contract import (
    TIMESTAMP_TOLERANCE_SECONDS,
    ExternalActor,
    IntakeConnection,
    IntakeEvent,
    ParseRefused,
    RawRequest,
    RawResponse,
    StoredRaw,
    Verified,
    VerifyRefused,
)

SW_ID = "webhook-id"
SW_TIMESTAMP = "webhook-timestamp"
SW_SIGNATURE = "webhook-signature"
BOSS_SIGNATURE = "x-boss-signature"
BOSS_TIMESTAMP = "x-boss-timestamp"
BOSS_DELIVERY = "x-boss-delivery"
SECRET_PREFIX = "whsec_"
DEFAULT_TYPE = "com.boss.webhook.received"
MAX_DELIVERY_ID = 200
_TYPE_OK = set("abcdefghijklmnopqrstuvwxyz0123456789._-")


class SecretNotBase64(ValueError):
    """A Standard Webhooks secret must be `whsec_` + standard base64 (the reference libraries
    base64-decode the key; a secret they cannot decode never verifies)."""


def _key_bytes(secret: str) -> bytes:
    """Standard Webhooks: the key is the strictly base64-decoded payload after `whsec_` (the
    prefix is optional in the reference implementations)."""
    raw = secret[len(SECRET_PREFIX):] if secret.startswith(SECRET_PREFIX) else secret
    try:
        return base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True)
    except (binascii.Error, ValueError) as error:
        raise SecretNotBase64(str(error)) from None


def standard_webhooks_signature(secret: str, msg_id: str, timestamp: str, body: bytes) -> str:
    """The `v1,<sig>` a Standard Webhooks sender puts in webhook-signature (tests, docs)."""
    signed = f"{msg_id}.{timestamp}.".encode("utf-8") + body
    digest = hmac.new(_key_bytes(secret), signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode("ascii")


def boss_v1_signature(secret: str, timestamp: int | str, body: bytes) -> str:
    """`v1=<hex>` over "<timestamp>.<body>" (workflow_webhooks.sign, kept identical)."""
    message = str(timestamp).encode("ascii") + b"." + body
    return "v1=" + hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _window(stamp: str, now: float) -> int:
    try:
        sent = int(stamp)
    except ValueError:
        raise VerifyRefused("bad_timestamp", provider="webhook") from None
    if abs(now - sent) > TIMESTAMP_TOLERANCE_SECONDS:
        raise VerifyRefused("stale_timestamp", provider="webhook")
    return sent


class WebhookIntakeAdapter(IntakeAdapterBase):
    key = "webhook"

    # -- PUSH -------------------------------------------------------------------------------------

    def _secrets(self, conn: IntakeConnection, now: float) -> list[str]:
        """The current signing secret, plus the previous one inside its rotation grace."""
        out = [conn.secret("signing_secret") or ""]
        until = str(conn.get("signing_secret_previous_until") or "")
        if until:
            try:
                grace_ok = datetime.fromisoformat(until).timestamp() > now
            except ValueError:
                grace_ok = False
            previous = conn.secret("signing_secret_previous") if grace_ok else None
            if previous:
                out.append(previous)
        return [s for s in out if s]

    def verify(self, conn: IntakeConnection, req: RawRequest, now: float) -> Verified:
        sw_sig = req.header(SW_SIGNATURE)
        boss_sig = req.header(BOSS_SIGNATURE)
        if not sw_sig and not boss_sig:
            raise VerifyRefused("missing_signature", provider=self.key)
        secrets = self._secrets(conn, now)
        if not secrets:
            raise VerifyRefused("not_configured", provider=self.key)
        if sw_sig:
            msg_id, stamp = req.header(SW_ID), req.header(SW_TIMESTAMP)
            if not msg_id or not stamp:
                raise VerifyRefused("missing_signature", provider=self.key)
            sent = _window(stamp, now)
            expected = []
            for secret in secrets:
                try:
                    expected.append(standard_webhooks_signature(secret, msg_id, stamp, req.body))
                except SecretNotBase64:
                    continue
            if not expected:
                raise VerifyRefused("not_configured", provider=self.key)
            for candidate in sw_sig.split():
                if candidate.startswith("v1,") and any(
                        hmac.compare_digest(candidate, e) for e in expected):
                    return Verified(scheme="standard_webhooks", timestamp=sent)
            raise VerifyRefused("bad_signature", provider=self.key)
        stamp = req.header(BOSS_TIMESTAMP)
        if not stamp:
            raise VerifyRefused("missing_signature", provider=self.key)
        sent = _window(stamp, now)
        expected = [boss_v1_signature(secret, stamp, req.body) for secret in secrets]
        for candidate in (p.strip() for p in boss_sig.split(",")):
            if candidate and any(hmac.compare_digest(candidate, e) for e in expected):
                return Verified(scheme="boss_v1", timestamp=sent)
        raise VerifyRefused("bad_signature", provider=self.key)

    def handshake(self, conn: IntakeConnection, req: RawRequest) -> Optional[RawResponse]:
        """CloudEvents HTTP webhook abuse protection: an OPTIONS with WebHook-Request-Origin is
        answered with WebHook-Allowed-Origin (only origins the connection allows), no row."""
        if req.method.upper() != "OPTIONS":
            return None
        origin = req.header("webhook-request-origin").strip()
        if not origin:
            return RawResponse(status=400, body={"error": "WebHook-Request-Origin is required"})
        allowed = [o.strip().lower() for o in str(conn.get("allowed_origins") or "").split(",")
                   if o.strip()]
        if allowed and origin.lower() not in allowed:
            return RawResponse(status=403, body={"error": "origin not allowed"})
        return RawResponse(status=200, body=None,
                           headers={"WebHook-Allowed-Origin": origin,
                                    "WebHook-Allowed-Rate": "120"})

    def dedupe_key(self, conn: IntakeConnection, req: RawRequest) -> str:
        delivery = (req.header(SW_ID) or req.header(BOSS_DELIVERY)).strip()[:MAX_DELIVERY_ID]
        if delivery:
            return f"webhook:{delivery}"
        stamp = req.header(SW_TIMESTAMP) or req.header(BOSS_TIMESTAMP)
        return "webhook:" + hashlib.sha256(stamp.encode() + b"." + req.body).hexdigest()

    # -- PROCESSOR --------------------------------------------------------------------------------

    def parse(self, conn: IntakeConnection, raw: StoredRaw) -> list[IntakeEvent]:
        try:
            body = json.loads(raw.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError) as error:
            raise ParseRefused(f"webhook body is not JSON: {type(error).__name__}",
                               provider=self.key) from None
        if not isinstance(body, dict):
            body = {"body": body}
        delivery = (str(raw.headers.get(SW_ID) or raw.headers.get(BOSS_DELIVERY) or "").strip()
                    or raw.dedupe_key.split(":", 1)[-1])
        is_ce = (str(body.get("specversion") or "").startswith("1.") and body.get("id")
                 and body.get("source") and body.get("type"))
        if is_ce:
            data = body.get("data")
            event = IntakeEvent(
                id=str(body["id"])[:200], source=str(body["source"])[:500],
                type=_clean_type(str(body["type"])), subject=_opt_str(body.get("subject")),
                time=_opt_str(body.get("time")),
                data=data if isinstance(data, dict) else {"value": data},
                datacontenttype=str(body.get("datacontenttype") or "application/json"))
        else:
            event = IntakeEvent(
                id=str(body.get("id") or delivery)[:200], source=conn.source_uri,
                type=_clean_type(str(body.get("type") or DEFAULT_TYPE)),
                subject=_opt_str(body.get("subject")), time=_opt_str(body.get("time")),
                data=body)
        if not event.time:
            stamp = raw.headers.get(SW_TIMESTAMP) or raw.headers.get(BOSS_TIMESTAMP)
            try:
                event.time = datetime.fromtimestamp(int(stamp), tz=timezone.utc).isoformat() \
                    if stamp else None
            except (ValueError, OverflowError, OSError):
                event.time = None
        actor = body.get("actor") if isinstance(body.get("actor"), dict) else {}
        event.bossactor = ExternalActor(
            kind=str(actor.get("kind") or "service") if actor else "service",
            external_id=str(actor.get("id") or actor.get("external_id") or ""),
            display=str(actor.get("display") or actor.get("name") or ""),
            email=_opt_str(actor.get("email")))
        causality = body.get("boss") if isinstance(body.get("boss"), dict) else None
        if causality and any(k in causality for k in ("run_id", "deployment_id", "hops")):
            event.bosscausality = {"run_id": causality.get("run_id"),
                                   "deployment_id": causality.get("deployment_id"),
                                   "hops": int(causality.get("hops") or 0)}
        event.bossconnection = conn.id
        event.bossdedupe = raw.dedupe_key
        return [event]

    def conversation_key(self, conn: IntakeConnection, ev: IntakeEvent) -> Optional[str]:
        return f"subject:{ev.subject}" if ev.subject else None


def _opt_str(value: Any) -> Optional[str]:
    return None if value in (None, "") else str(value)[:1000]


def _clean_type(value: str) -> str:
    cleaned = "".join(ch for ch in value.strip().lower() if ch in _TYPE_OK)[:200]
    return cleaned or DEFAULT_TYPE


def now_seconds() -> int:
    return int(time.time())


__all__ = ["SecretNotBase64", "WebhookIntakeAdapter", "boss_v1_signature",
           "standard_webhooks_signature"]
