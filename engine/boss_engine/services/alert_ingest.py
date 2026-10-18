"""Alerts other tools send BOSS, end to end: authenticate the source, read its shape (an
alert_adapters function), refuse what names another project, and write each event into THE one
alert record (app_alerts.record_external), which groups it into THE one incident model and
queues THE one triage. ``/v1/reliability/alert`` — the July reliability pipeline's door — is a
deprecated alias of this same path (spec §10.5, R1.25): one incident pipeline.

  POST /v1/ingest/{source_id}/{kind}   Bearer <token> (or ?token=, Basic, routing_key), or a
                                       signing source's X-BOSS-Signature
  202 {alert_id, alert_ids, status: accepted | deduplicated, word}; a 4xx with a sentence else.
"""
from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from ..ports import sql
from . import alert_adapters as ada
from . import monitoring as mon

log = logging.getLogger(__name__)

LEGACY_SOURCE_NAME = "Legacy /v1/reliability/alert"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _body_token(kind: str, raw: bytes) -> Optional[str]:
    """A PagerDuty sender authenticates with routing_key in the body; nothing else does."""
    if kind not in ("pagerduty", "events"):
        return None
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return None
    items = body if isinstance(body, list) else [body]
    for ev in items:
        if isinstance(ev, Mapping) and ev.get("routing_key"):
            return str(ev["routing_key"])
    return None


def names_another_project(raw: bytes, project_id: str) -> bool:
    """A body that names a project other than its token's (top level or any alert's labels)."""
    try:
        body = json.loads(raw or b"null")
    except ValueError:
        return False
    named: list[str] = []
    items = body if isinstance(body, list) else [body]
    for b in items:
        if not isinstance(b, Mapping):
            continue
        for k in ("project_id", "project"):
            if isinstance(b.get(k), str):
                named.append(b[k])
        for holder in [b.get("labels"), b.get("commonLabels"),
                       *[a.get("labels") for a in (b.get("alerts") or [])
                         if isinstance(a, Mapping)]]:
            if isinstance(holder, Mapping) and isinstance(holder.get("project"), str):
                named.append(holder["project"])
    return any(n and n != project_id for n in named)


# --------------------------------------------------------------------------- #
# SNS (CloudWatch): signature check and subscription confirmation
# --------------------------------------------------------------------------- #
_SNS_FIELDS = {"Notification": ("Message", "MessageId", "Subject", "Timestamp", "TopicArn",
                                "Type"),
               "SubscriptionConfirmation": ("Message", "MessageId", "SubscribeURL", "Timestamp",
                                            "Token", "TopicArn", "Type")}


def _sns_host_ok(url: str) -> bool:
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and host.startswith("sns.") and host.endswith(
        ".amazonaws.com")


def verify_sns(envelope: Mapping[str, Any], *, fetch_cert: Any = None) -> bool:
    """AWS's SNS signature (SignatureVersion 1 SHA1 or 2 SHA256) over the canonical string."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    kind = envelope.get("Type")
    fields = _SNS_FIELDS.get(kind or "")
    cert_url = str(envelope.get("SigningCertURL") or envelope.get("SigningCertUrl") or "")
    if not fields or not _sns_host_ok(cert_url):
        return False
    canonical = "".join(f"{k}\n{envelope[k]}\n" for k in fields if envelope.get(k) is not None)
    try:
        pem = (fetch_cert or _fetch_cert)(cert_url)
        cert = x509.load_pem_x509_certificate(pem)
        algo = hashes.SHA256() if str(envelope.get("SignatureVersion")) == "2" else hashes.SHA1()
        cert.public_key().verify(base64.b64decode(envelope.get("Signature") or ""),
                                 canonical.encode(), padding.PKCS1v15(), algo)
        return True
    except Exception:  # noqa: BLE001 — anything wrong is "not signed by AWS"
        return False


def _fetch_cert(url: str) -> bytes:
    from ..identity.http import fetch

    return fetch("GET", url, purpose="monitoring.sns_cert").body


def _confirm_subscription(envelope: Mapping[str, Any]) -> str:
    """The one network call an adapter path makes: confirming a pending SNS subscription."""
    from ..identity.http import fetch

    url = str(envelope.get("SubscribeURL") or "")
    if not _sns_host_ok(url):
        raise mon.IngestRefused(400, "The SNS SubscribeURL is not an AWS SNS address")
    resp = fetch("GET", url, purpose="monitoring.sns_confirm")
    if resp.status >= 300:
        raise mon.IngestRefused(502, f"AWS answered {resp.status} to the confirmation")
    return "Subscription confirmed"


# --------------------------------------------------------------------------- #
# The path
# --------------------------------------------------------------------------- #
def receive(engine, settings: Any, *, source_id: Optional[str], kind: str,
            headers: Mapping[str, str], query: Mapping[str, str], raw: bytes,
            now: Optional[datetime] = None, source: Optional[dict] = None,
            sns_cert: Any = None) -> dict[str, Any]:
    from ..db import begin
    from . import app_alerts

    now = now or _now()
    if kind not in ada.ADAPTERS:
        raise mon.IngestRefused(404, f"Unknown kind {kind!r}: use one of "
                                     f"{', '.join(sorted(ada.ADAPTERS))}")
    adapter, kinds = ada.ADAPTERS[kind]
    try:
        if source is None:
            with begin(engine) as c:
                source = mon.authenticate(c, headers=headers, raw=raw, kinds=kinds,
                                          source_id=source_id, query=query,
                                          body_token=_body_token(kind, raw), now=now)
        if names_another_project(raw, source["project_id"]):
            raise mon.IngestRefused(403, "The body names another project. The project comes "
                                         "from the source's token, never from the payload.",
                                    source["id"])
        if kind == "cloudwatch":
            env = ada.sns_envelope(raw)
            if not verify_sns(env, fetch_cert=sns_cert):
                raise mon.IngestRefused(401, "The SNS message is not signed by AWS",
                                        source["id"])
            if env["Type"] == "SubscriptionConfirmation":
                word = _confirm_subscription(env)
                with begin(engine) as c:
                    mon.accepted(c, source["id"], now)
                return {"alert_id": None, "alert_ids": [], "status": "accepted", "word": word}
        try:
            events = adapter(headers, raw, source)
        except ada.AdapterRefused as refused:
            raise mon.IngestRefused(refused.status, refused.word, source["id"]) from None
        out = []
        with begin(engine) as c:
            if kind == "heartbeat":
                out.append(app_alerts.record_heartbeat(c, source, now))
            for ev in events:
                if ev.action == "ping":
                    continue
                out.append(app_alerts.record_external(c, source, ev, now))
            mon.accepted(c, source["id"], now)
    except mon.IngestRefused as exc:
        mon.record_refusal(engine, exc, now)
        raise
    if not out:
        return {"alert_id": None, "alert_ids": [], "status": "accepted",
                "word": "Nothing to change (no alert in it)"}
    status = "accepted" if any(o["status"] == "accepted" for o in out) else "deduplicated"
    ids = [o.get("alert_id") for o in out if o.get("alert_id")]
    return {"alert_id": ids[0] if ids else None, "alert_ids": ids, "status": status,
            "word": "; ".join(dict.fromkeys(o["word"] for o in out if o.get("word")))}


def test_event(connection, source: dict, now: datetime) -> dict[str, Any]:
    """The console's Test: a firing then resolved event through the same record, so the person
    sees the source work end to end without waiting for a real alert."""
    from . import app_alerts

    if source["kind"] in ("otlp", "remote_write"):
        return {"word": "Metrics sources are checked by the live check: send data and watch "
                        "the sheet"}
    if source["kind"] == "heartbeat":
        return app_alerts.record_heartbeat(connection, source, now)
    ref = f"boss-test-{int(now.timestamp())}"
    fire = ada.NormalizedEvent(action="trigger", external_ref=ref, alertname="BossTestAlert",
                               summary="A test from BOSS: this source works",
                               severity="info", tool=source["kind"],
                               labels={"test": "true"}, timestamp=now.timestamp())
    got = app_alerts.record_external(connection, source, fire, now)
    app_alerts.record_external(connection, source, ada.NormalizedEvent(
        action="resolve", external_ref=ref, alertname="BossTestAlert", tool=source["kind"],
        timestamp=now.timestamp() + 1), now)
    return {"alert_id": got.get("alert_id"), "word": "A test alert fired and resolved; see it "
                                                     "in Observability › Alerts"}


def legacy_source(connection, project_id: str) -> dict[str, Any]:
    """The source the deprecated shared-secret door writes through (its project is the
    install's default, never the payload's)."""
    row = connection.execute(sql(
        "SELECT * FROM signal_sources WHERE project_id = :p AND name = :n AND kind = 'generic'"),
        {"p": project_id, "n": LEGACY_SOURCE_NAME}).mappings().first()
    if row:
        return dict(row)
    made = mon.create_source(connection, project_id, kind="generic", name=LEGACY_SOURCE_NAME,
                             actor="boss")
    return dict(connection.execute(sql("SELECT * FROM signal_sources WHERE id = :i"),
                                   {"i": made["source"]["id"]}).mappings().first())


def legacy_kind(raw: bytes) -> tuple[str, bytes]:
    """The deprecated door's shapes, read as the adapters they are: Alertmanager / Grafana
    ({alerts: [...]}), PagerDuty Events v2 ({event_action}), PagerDuty v3 webhooks and the
    generic {title, service, severity, description} (as a PagerDuty-shaped trigger)."""
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise mon.IngestRefused(400, "The body is not JSON") from None
    if not isinstance(body, Mapping):
        raise mon.IngestRefused(400, "The body must be a JSON object")
    if isinstance(body.get("alerts"), list):
        return "alertmanager", raw
    if body.get("event_action"):
        return "events", raw
    if isinstance(body.get("event"), Mapping):
        data = body["event"].get("data") or {}
        svc = data.get("service") or {}
        title = data.get("title") or "alert"
        status = str(data.get("status") or "triggered")
        event = {"event_action": "resolve" if status == "resolved" else "trigger",
                 "dedup_key": str(data.get("id") or title),
                 "payload": {"summary": title, "source": svc.get("summary") or svc.get("name")
                             or "", "severity": "critical" if (data.get("priority") or {}).get(
                                 "summary") in ("P1", "P2") else "warning"}}
        return "events", json.dumps(event).encode()
    title = body.get("title") or body.get("alertname")
    if title:
        event = {"event_action": "trigger", "dedup_key": str(body.get("dedup_key") or title),
                 "payload": {"summary": str(title), "source": str(body.get("service") or ""),
                             "severity": str(body.get("severity") or "warning"),
                             "custom_details": {"description": body.get("description") or ""}},
                 "labels": {"service": str(body.get("service") or "")}}
        return "events", json.dumps(event).encode()
    raise mon.IngestRefused(400, "Unrecognised alert shape: send Alertmanager, Grafana, "
                                 "PagerDuty Events v2 or {title, service, severity}")


__all__ = ["LEGACY_SOURCE_NAME", "legacy_kind", "legacy_source", "names_another_project",
           "receive", "test_event", "verify_sns"]
