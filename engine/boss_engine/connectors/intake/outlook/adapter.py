"""Microsoft Outlook / Graph intake adapter (P9c-1, BOSSAPP-1105; EVENTING_LLD.md §3.7, §4.1, §6.2).

Supports:
  * Push validation handshake: `POST ?validationToken=<token>` echoed as `text/plain` 200 (<= 10 s).
  * Change notification verification: constant-time `clientState` check across every notification.
  * Lifecycle notifications: `reauthorizationRequired`, `subscriptionRemoved`, `missed`.
  * Subscription management: `subscribe` / `renew` / `unsubscribe` against Graph `/v1.0/subscriptions`
    (capped at Graph's 10,080 min limit; default 4,230 min = ~3 days).
  * Delta catch-up (`poll`): Graph message delta (`/messages/delta`), paginating `@odata.nextLink`,
    checkpointing `@odata.deltaLink`, and raising `CursorExpired` on HTTP 410 (`syncStateNotFound`).
  * DMARC parsing from `internetMessageHeaders` (`Authentication-Results`), plus RFC 3834 and
    own-mailbox loop prevention.
"""
from __future__ import annotations

import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional
from urllib.parse import quote

from .._mail import (
    extract_boss_causality,
    first_header,
    is_auto_submitted,
    normalize_headers,
    parse_dmarc,
    parse_from,
)
from ..base import IntakeAdapterBase
from ..contract import (
    AuthFailed,
    Cursor,
    CursorExpired,
    ExternalActor,
    Health,
    IntakeConnection,
    IntakeError,
    IntakeEvent,
    ParseRefused,
    PersonHint,
    PollResult,
    RateLimited,
    RawRequest,
    RawResponse,
    StoredRaw,
    Subscription,
    SubscriptionSpec,
    Verified,
    VerifyRefused,
)

GRAPH_BASE = "https://graph.microsoft.com"
#: Microsoft Graph Outlook message subscription max lifetime without resource data: 10,080 min
#: (7 days). Default 4,230 min (~3 days) leaves ample renewal headroom.
DEFAULT_LIFETIME_SECONDS = 4230 * 60
MAX_LIFETIME_SECONDS = 10080 * 60
MIN_LIFETIME_SECONDS = 45 * 60
LIFECYCLE_EVENTS = {
    "reauthorizationRequired": "reauthorization_required",
    "subscriptionRemoved": "removed",
    "missed": "missed",
}


class SubscriptionLost(IntakeError):
    """Provider says the subscription no longer exists (404/410) and must be recreated."""


def _default_transport(method: str, url: str, headers: Mapping[str, str],
                       body: Optional[bytes], purpose: str) -> tuple[int, Any, dict[str, str]]:
    from ....identity.http import fetch

    resp = fetch(method, url, headers=dict(headers), data=body, purpose=purpose)
    try:
        data = resp.json()
    except ValueError:
        data = None
    return resp.status, data, {str(k).lower(): str(v) for k, v in (resp.headers or {}).items()}


#: (method, url, headers, body, purpose) -> (status, parsed_json_or_none, response_headers).
#: Tests replace this seam with a local fake Graph server.
TRANSPORT: Callable[..., tuple[int, Any, dict[str, str]]] = _default_transport


def _call_transport(method: str, url: str, headers: Mapping[str, str],
                    body: Optional[bytes], purpose: str) -> tuple[int, Any, dict[str, str]]:
    got = TRANSPORT(method, url, headers, body, purpose)
    if len(got) == 2:
        return int(got[0]), got[1], {}
    return int(got[0]), got[1], {str(k).lower(): str(v) for k, v in (got[2] or {}).items()}


def _graph_base(conn: IntakeConnection) -> str:
    return str(conn.get("graph_base_url") or GRAPH_BASE).rstrip("/")


def _mailbox(conn: IntakeConnection) -> str:
    mb = str(conn.get("mailbox") or "").strip()
    if not mb:
        raise ParseRefused("Outlook connection requires 'mailbox' in config", provider="outlook")
    return mb


def _resource(conn: IntakeConnection, override: str = "") -> str:
    if override:
        return override
    cfg_res = str(conn.get("resource") or "").strip()
    if cfg_res:
        return cfg_res
    mb = quote(_mailbox(conn), safe="@.")
    return f"users/{mb}/mailFolders('Inbox')/messages"


def _auth_headers(conn: IntakeConnection) -> dict[str, str]:
    token = conn.secret("access_token")
    if not token:
        tenant = str(conn.get("tenant_id") or "").strip()
        cid = str(conn.get("client_id") or "").strip()
        csec = conn.secret("client_secret")
        if tenant and cid and csec:
            token = _exchange_client_credentials(tenant, cid, csec)
    if not token:
        raise AuthFailed("Outlook Graph access_token (or client credentials) is not configured",
                         provider="outlook")
    return {"Authorization": f"Bearer {token}", "Accept": "application/json",
            "Content-Type": "application/json"}


def _exchange_client_credentials(tenant_id: str, client_id: str, client_secret: str) -> str:
    from urllib.parse import urlencode

    url = f"https://login.microsoftonline.com/{quote(tenant_id, safe='')}/oauth2/v2.0/token"
    body = urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default",
    }).encode("utf-8")
    status, data, _ = _call_transport(
        "POST", url, {"Content-Type": "application/x-www-form-urlencoded"}, body,
        "intake.outlook.token",
    )
    if status != 200 or not isinstance(data, dict) or not data.get("access_token"):
        raise AuthFailed(f"Entra token exchange failed ({status})", provider="outlook")
    return str(data["access_token"])


def _check_status(status: int, data: Any, headers: Mapping[str, str], op: str) -> None:
    if status in (401, 403):
        raise AuthFailed(f"Microsoft Graph refused {op} ({status})", provider="outlook")
    if status == 429:
        retry = headers.get("retry-after")
        try:
            wait = float(retry) if retry else None
        except ValueError:
            wait = None
        raise RateLimited(f"Microsoft Graph rate-limited {op}", retry_after=wait,
                          provider="outlook")
    if status == 410:
        raise CursorExpired(f"Microsoft Graph delta token expired during {op} (410)",
                            provider="outlook")
    if status == 404 and op in ("renew", "unsubscribe"):
        raise SubscriptionLost(f"Microsoft Graph subscription not found during {op} (404)",
                               provider="outlook")
    if not 200 <= status < 300:
        err = ""
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            err = str(data["error"].get("code") or "")
        raise IntakeError(f"Microsoft Graph {op} answered {status}" + (f" ({err})" if err else ""),
                          provider="outlook")


def _parse_message_event(conn: IntakeConnection, msg: Mapping[str, Any], *,
                         change_type: str, dedupe_key: str) -> IntakeEvent:
    msg_id = str(msg.get("id") or "")
    conv_id = str(msg.get("conversationId") or msg_id)
    subject = str(msg.get("subject") or "")
    body_obj = msg.get("body") if isinstance(msg.get("body"), Mapping) else {}
    body_text = str(body_obj.get("content") or msg.get("bodyPreview") or "")[:20000]
    sender_obj = (msg.get("from") or msg.get("sender") or {})
    email_addr_obj = sender_obj.get("emailAddress") if isinstance(sender_obj, Mapping) else {}
    if isinstance(email_addr_obj, Mapping) and email_addr_obj.get("address"):
        from_display = str(email_addr_obj.get("name") or "")
        from_email = str(email_addr_obj.get("address") or "").strip().lower()
    else:
        from_display, from_email = parse_from("")
    headers = normalize_headers(msg.get("internetMessageHeaders") or [])
    if not from_email and first_header(headers, "from"):
        from_display, from_email = parse_from(first_header(headers, "from"))
    auth_results = headers.get("authentication-results") or []
    dmarc = parse_dmarc(auth_results, from_email)
    auto_sub = is_auto_submitted(headers)
    causality = extract_boss_causality(headers)
    op = "updated" if change_type == "updated" else "created"
    ev = IntakeEvent(
        id=msg_id or dedupe_key.rsplit(":", 1)[-1],
        source=f"outlook://{conn.get('mailbox') or conn.id}",
        type=f"com.microsoft.outlook.message.{op}",
        subject=conv_id or subject or None,
        time=str(msg.get("receivedDateTime") or msg.get("lastModifiedDateTime") or "") or None,
        data={
            "record_type": "email",
            "record_id": msg_id,
            "conversation_id": conv_id,
            "internet_message_id": str(msg.get("internetMessageId") or ""),
            "subject": subject,
            "text": body_text,
            "body_preview": str(msg.get("bodyPreview") or "")[:1000],
            "from_email": from_email,
            "from_display": from_display,
            "dmarc": dmarc,
            "auto_submitted": auto_sub,
            "change_type": op,
            "operation": op,
        },
        bossconnection=conn.id,
        bossconversation={"conversation_id": conv_id, "key": conv_id} if conv_id else None,
        bossactor=ExternalActor(
            kind="user" if from_email else "unknown",
            external_id=from_email,
            display=from_display or from_email,
            email=from_email or None,
        ),
        bosscausality=causality,
        bossdedupe=dedupe_key,
    )
    return ev


class OutlookIntakeAdapter(IntakeAdapterBase):
    key = "outlook"

    # -- PUSH -------------------------------------------------------------------------------------

    def verify(self, conn: IntakeConnection, req: RawRequest, now: float) -> Verified:
        # Graph subscription validation handshake: POST ?validationToken=... with empty body.
        validation_token = str((req.query or {}).get("validationToken") or "").strip()
        if validation_token:
            if not conn.secret("client_state"):
                raise VerifyRefused("not_configured", provider=self.key)
            return Verified(scheme="graph_validation_token")
        expected = conn.secret("client_state") or ""
        previous = conn.secret("client_state_previous") or ""
        if not expected:
            raise VerifyRefused("not_configured", provider=self.key)
        try:
            payload = json.loads(req.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            raise VerifyRefused("bad_payload", provider=self.key) from None
        items = payload.get("value") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not items:
            raise VerifyRefused("missing_notifications", provider=self.key)
        for item in items:
            if not isinstance(item, dict):
                raise VerifyRefused("bad_notification", provider=self.key)
            given = str(item.get("clientState") or "")
            if not given:
                raise VerifyRefused("missing_client_state", provider=self.key)
            ok = hmac.compare_digest(given, expected) or (
                bool(previous) and hmac.compare_digest(given, previous)
            )
            if not ok:
                raise VerifyRefused("bad_client_state", provider=self.key)
        return Verified(scheme="graph_client_state")

    def handshake(self, conn: IntakeConnection, req: RawRequest) -> Optional[RawResponse]:
        validation_token = str((req.query or {}).get("validationToken") or "")
        if validation_token:
            return RawResponse(status=200, body=validation_token, media_type="text/plain")
        return None

    def dedupe_key(self, conn: IntakeConnection, req: RawRequest) -> str:
        try:
            payload = json.loads(req.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            payload = {}
        items = payload.get("value") if isinstance(payload, dict) else None
        if isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict):
            item = items[0]
            if item.get("lifecycleEvent"):
                sub_id = str(item.get("subscriptionId") or "")
                lc = str(item.get("lifecycleEvent") or "")
                return f"outlook:lifecycle:{sub_id}:{lc}:{req.body_sha256()[:16]}"
            rd = item.get("resourceData") if isinstance(item.get("resourceData"), dict) else {}
            rid = str(rd.get("id") or item.get("resource") or "")
            ct = str(item.get("changeType") or "created").lower()
            if rid:
                return f"outlook:{rid}:{ct}"
        return f"outlook:sha256:{req.body_sha256()}"

    # -- PROCESSOR --------------------------------------------------------------------------------

    def parse(self, conn: IntakeConnection, raw: StoredRaw) -> list[IntakeEvent]:
        try:
            body = json.loads(raw.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ParseRefused(f"Outlook body is not JSON: {type(exc).__name__}",
                               provider=self.key) from None
        if not isinstance(body, dict):
            raise ParseRefused("Outlook body is not an object", provider=self.key)
        if body.get("_boss_kind") == "outlook_message" and isinstance(body.get("message"), dict):
            ct = str(body.get("changeType") or "created").lower()
            return [_parse_message_event(conn, body["message"], change_type=ct,
                                         dedupe_key=raw.dedupe_key)]
        items = body.get("value")
        if not isinstance(items, list):
            raise ParseRefused("Outlook notification missing 'value' list", provider=self.key)
        out: list[IntakeEvent] = []
        mailbox = str(conn.get("mailbox") or conn.id)
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            lc = str(item.get("lifecycleEvent") or "")
            if lc:
                suffix = LIFECYCLE_EVENTS.get(lc, lc.lower())
                sub_id = str(item.get("subscriptionId") or "")
                out.append(IntakeEvent(
                    id=f"lifecycle:{sub_id}:{lc}:{idx}",
                    source=f"outlook://{mailbox}",
                    type=f"com.microsoft.outlook.subscription.{suffix}",
                    subject=sub_id or mailbox,
                    data={
                        "lifecycle_event": lc,
                        "subscription_id": sub_id,
                        "resource": str(item.get("resource") or ""),
                        "tenant_id": str(item.get("tenantId") or ""),
                    },
                    bossconnection=conn.id,
                    bossactor=ExternalActor(kind="service", external_id="microsoft-graph",
                                            display="Microsoft Graph"),
                    bossdedupe=raw.dedupe_key,
                ))
                continue
            rd = item.get("resourceData") if isinstance(item.get("resourceData"), dict) else {}
            msg_id = str(rd.get("id") or "")
            ct = str(item.get("changeType") or "created").lower()
            if isinstance(item.get("message"), dict):
                out.append(_parse_message_event(conn, item["message"], change_type=ct,
                                                dedupe_key=f"outlook:{msg_id}:{ct}"))
                continue
            out.append(IntakeEvent(
                id=msg_id or f"notif-{idx}",
                source=f"outlook://{mailbox}",
                type=f"com.microsoft.outlook.message.{ct}",
                subject=msg_id or None,
                data={
                    "record_type": "email",
                    "record_id": msg_id,
                    "resource": str(item.get("resource") or ""),
                    "subscription_id": str(item.get("subscriptionId") or ""),
                    "change_type": ct,
                    "operation": ct,
                    "needs_fetch": True,
                },
                bossconnection=conn.id,
                bossactor=ExternalActor(kind="unknown"),
                bossdedupe=f"outlook:{msg_id}:{ct}" if msg_id else raw.dedupe_key,
            ))
        return out

    def enrich(self, conn: IntakeConnection, ev: IntakeEvent) -> IntakeEvent:
        """Fetch full message headers/body for push notifications that carried only resourceData.id."""
        if not ev.data.get("needs_fetch") or not ev.data.get("record_id"):
            return ev
        try:
            headers = _auth_headers(conn)
        except AuthFailed:
            return ev
        mb = quote(_mailbox(conn), safe="@.")
        mid = quote(str(ev.data["record_id"]), safe="")
        url = (f"{_graph_base(conn)}/v1.0/users/{mb}/messages/{mid}"
               "?$select=id,conversationId,subject,from,body,bodyPreview,"
               "receivedDateTime,internetMessageId,internetMessageHeaders")
        status, data, resp_headers = _call_transport("GET", url, headers, None,
                                                     "intake.outlook.enrich")
        if status == 200 and isinstance(data, dict):
            return _parse_message_event(
                conn, data, change_type=str(ev.data.get("change_type") or "created"),
                dedupe_key=ev.bossdedupe,
            )
        return ev

    def is_self(self, conn: IntakeConnection, ev: IntakeEvent) -> bool:
        if ev.type.startswith("com.microsoft.outlook.subscription."):
            return False
        own = str(conn.get("mailbox") or "").strip().lower()
        sender = str(ev.bossactor.email or ev.data.get("from_email") or "").strip().lower()
        if own and sender and own == sender:
            return True
        if ev.data.get("auto_submitted") is True:
            return True
        if (ev.bosscausality or {}).get("run_id"):
            return True
        return False

    def conversation_key(self, conn: IntakeConnection, ev: IntakeEvent) -> Optional[str]:
        conv = (ev.bossconversation or {}).get("conversation_id") or ev.data.get("conversation_id")
        return str(conv) if conv else ev.subject

    def resolve_person(self, conn: IntakeConnection, actor: ExternalActor) -> PersonHint:
        return PersonHint(email=actor.email, display=actor.display)

    # -- SUBSCRIBE / RENEW / UNSUBSCRIBE ----------------------------------------------------------

    def subscribe(self, conn: IntakeConnection, spec: SubscriptionSpec) -> Subscription:
        headers = _auth_headers(conn)
        lifetime = int(spec.lifetime_seconds or DEFAULT_LIFETIME_SECONDS)
        lifetime = max(MIN_LIFETIME_SECONDS, min(MAX_LIFETIME_SECONDS, lifetime))
        exp_dt = datetime.now(timezone.utc) + timedelta(seconds=lifetime)
        client_state = spec.client_state or conn.secret("client_state") or ""
        if not client_state:
            raise AuthFailed("Outlook subscription requires client_state secret", provider=self.key)
        payload = {
            "changeType": "created,updated",
            "notificationUrl": spec.notification_url,
            "lifecycleNotificationUrl": spec.notification_url,
            "resource": _resource(conn, spec.resource),
            "expirationDateTime": exp_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "clientState": client_state[:128],
        }
        url = f"{_graph_base(conn)}/v1.0/subscriptions"
        status, data, resp_headers = _call_transport(
            "POST", url, headers, json.dumps(payload).encode("utf-8"),
            "intake.outlook.subscribe",
        )
        _check_status(status, data, resp_headers, "subscribe")
        if not isinstance(data, dict) or not data.get("id"):
            raise IntakeError("Microsoft Graph subscribe returned no subscription id",
                              provider=self.key)
        return Subscription(
            provider_subscription_id=str(data["id"]),
            resource=str(data.get("resource") or payload["resource"]),
            expires_at=str(data.get("expirationDateTime") or payload["expirationDateTime"]),
        )

    def renew(self, conn: IntakeConnection, sub: Subscription) -> Subscription:
        headers = _auth_headers(conn)
        exp_dt = datetime.now(timezone.utc) + timedelta(seconds=DEFAULT_LIFETIME_SECONDS)
        payload = {"expirationDateTime": exp_dt.strftime("%Y-%m-%dT%H:%M:%SZ")}
        url = f"{_graph_base(conn)}/v1.0/subscriptions/{quote(sub.provider_subscription_id, safe='')}"
        status, data, resp_headers = _call_transport(
            "PATCH", url, headers, json.dumps(payload).encode("utf-8"),
            "intake.outlook.renew",
        )
        _check_status(status, data, resp_headers, "renew")
        expires = (data or {}).get("expirationDateTime") if isinstance(data, dict) else None
        return Subscription(
            provider_subscription_id=sub.provider_subscription_id,
            resource=sub.resource,
            expires_at=str(expires or payload["expirationDateTime"]),
        )

    def unsubscribe(self, conn: IntakeConnection, sub: Subscription) -> None:
        headers = _auth_headers(conn)
        url = f"{_graph_base(conn)}/v1.0/subscriptions/{quote(sub.provider_subscription_id, safe='')}"
        status, data, resp_headers = _call_transport("DELETE", url, headers, None,
                                                     "intake.outlook.unsubscribe")
        if status == 404:
            return
        _check_status(status, data, resp_headers, "unsubscribe")

    # -- POLL (DELTA CATCH-UP) --------------------------------------------------------------------

    def poll(self, conn: IntakeConnection, cursor: Cursor, limit: int) -> PollResult:
        headers = _auth_headers(conn)
        base = _graph_base(conn)
        mb = quote(_mailbox(conn), safe="@.")
        cap = max(1, min(int(limit or 50), 200))
        url = (cursor.value if cursor.value and cursor.value.startswith(("http://", "https://"))
               else f"{base}/v1.0/users/{mb}/mailFolders('Inbox')/messages/delta?$top={cap}")
        deliveries: list[StoredRaw] = []
        events: list[IntakeEvent] = []
        delta_link = cursor.value
        more = False
        pages = 0
        while url and pages < 10:
            pages += 1
            status, data, resp_headers = _call_transport("GET", url, headers, None,
                                                         "intake.outlook.delta")
            _check_status(status, data, resp_headers, "delta")
            if not isinstance(data, dict):
                break
            for msg in data.get("value") or []:
                if not isinstance(msg, dict) or not msg.get("id"):
                    continue
                if msg.get("@removed"):
                    continue
                mid = str(msg["id"])
                dk = f"outlook:{mid}:created"
                wrapped = {"_boss_kind": "outlook_message", "changeType": "created", "message": msg}
                raw_bytes = json.dumps(wrapped, sort_keys=True).encode("utf-8")
                stored = StoredRaw(body=raw_bytes, headers={"content-type": "application/json"},
                                   dedupe_key=dk)
                deliveries.append(stored)
                events.append(_parse_message_event(conn, msg, change_type="created", dedupe_key=dk))
                if len(deliveries) >= cap:
                    more = True
                    break
            next_link = data.get("@odata.nextLink")
            if data.get("@odata.deltaLink"):
                delta_link = str(data["@odata.deltaLink"])
                url = ""
            elif next_link and not more:
                url = str(next_link)
            else:
                if next_link:
                    more = True
                url = ""
        now_iso = datetime.now(timezone.utc).isoformat()
        return PollResult(
            events=tuple(events),
            deliveries=tuple(deliveries),
            next_cursor=Cursor(stream_key=cursor.stream_key, value=delta_link,
                               cursor_time=now_iso),
            more=more,
        )

    def health(self, conn: IntakeConnection) -> Health:
        try:
            headers = _auth_headers(conn)
            mb = quote(_mailbox(conn), safe="@.")
            url = f"{_graph_base(conn)}/v1.0/users/{mb}/mailFolders('Inbox')?$select=id,displayName"
            status, data, resp_headers = _call_transport("GET", url, headers, None,
                                                         "intake.outlook.health")
            _check_status(status, data, resp_headers, "health")
            return Health(ok=True, detail="Graph mailbox reachable",
                          checks={"mailbox": _mailbox(conn)})
        except Exception as exc:  # noqa: BLE001 — never leak credentials
            return Health(ok=False, detail=f"{type(exc).__name__}: {exc}"[:200])


__all__ = [
    "DEFAULT_LIFETIME_SECONDS",
    "GRAPH_BASE",
    "MAX_LIFETIME_SECONDS",
    "OutlookIntakeAdapter",
    "SubscriptionLost",
    "TRANSPORT",
]
