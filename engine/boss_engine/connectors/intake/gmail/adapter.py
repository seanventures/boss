"""Gmail intake adapter (P9c-1, BOSSAPP-1105; EVENTING_LLD.md §3.7, §4.1, §6.2, §13).

Supports:
  * Pub/Sub push OIDC verification: RS256 JWT against Google OIDC JWKS, checking `iss`, `aud`,
    `email` (service account), `email_verified`, `exp`/`nbf` skew. Ack 204.
  * `users.watch` subscription creation and daily renewal (before Gmail's 7-day hard limit),
    plus `users.stop` on unsubscribe.
  * `history.list` incremental catch-up (`poll`), with HTTP 404 (`historyId` expired) triggering
    a bounded 7-day full sync (`q=newer_than:7d`, capped at `MAX_RESYNC_MESSAGES`) whose overlap
    is absorbed by inbox deduplication on message id.
  * RFC 8601 DMARC parsing from `Authentication-Results`, RFC 3834 loop filtering, and own-mailbox
    filtering. Credentials come exclusively from the vault/secret backend.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional
from urllib.parse import quote, urlencode

import jwt

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
    StoredRaw,
    Subscription,
    SubscriptionSpec,
    Verified,
    VerifyRefused,
)

GMAIL_API_BASE = "https://gmail.googleapis.com"
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})
MAX_RESYNC_MESSAGES = 100
RESYNC_QUERY = "newer_than:7d"
JWKS_CACHE_TTL_SECONDS = 3600

_JWKS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


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
TRANSPORT: Callable[..., tuple[int, Any, dict[str, str]]] = _default_transport


def _call_transport(method: str, url: str, headers: Mapping[str, str],
                    body: Optional[bytes], purpose: str) -> tuple[int, Any, dict[str, str]]:
    got = TRANSPORT(method, url, headers, body, purpose)
    if len(got) == 2:
        return int(got[0]), got[1], {}
    return int(got[0]), got[1], {str(k).lower(): str(v) for k, v in (got[2] or {}).items()}


def _default_jwks_loader(conn: IntakeConnection, now: float) -> dict[str, Any]:
    url = str(conn.get("oidc_jwks_url") or GOOGLE_JWKS_URL)
    hit = _JWKS_CACHE.get(url)
    if hit and hit[0] > now:
        return hit[1]
    status, data, _ = _call_transport("GET", url, {"Accept": "application/json"}, None,
                                      "intake.gmail.jwks")
    if status != 200 or not isinstance(data, dict) or not isinstance(data.get("keys"), list):
        raise VerifyRefused("jwks_unavailable", provider="gmail")
    _JWKS_CACHE[url] = (now + JWKS_CACHE_TTL_SECONDS, data)
    return data


#: (conn, now) -> {"keys": [JWK dicts]}. Tests may replace this or TRANSPORT.
JWKS_LOADER: Callable[[IntakeConnection, float], dict[str, Any]] = _default_jwks_loader


def _api_base(conn: IntakeConnection) -> str:
    return str(conn.get("api_base_url") or GMAIL_API_BASE).rstrip("/")


def _mailbox(conn: IntakeConnection) -> str:
    return str(conn.get("mailbox") or "me").strip() or "me"


def _label_ids(conn: IntakeConnection) -> list[str]:
    raw = conn.get("label_ids")
    if isinstance(raw, list):
        vals = [str(v).strip() for v in raw if str(v).strip()]
        return vals or ["INBOX"]
    if isinstance(raw, str) and raw.strip():
        return [p.strip() for p in raw.split(",") if p.strip()] or ["INBOX"]
    return ["INBOX"]


def _auth_headers(conn: IntakeConnection) -> dict[str, str]:
    token = conn.secret("access_token")
    if not token:
        cid = str(conn.get("client_id") or "").strip()
        csec = conn.secret("client_secret")
        rtok = conn.secret("refresh_token")
        if cid and csec and rtok:
            token = _refresh_access_token(cid, csec, rtok)
    if not token:
        raise AuthFailed("Gmail OAuth access_token (or refresh credentials) is not configured",
                         provider="gmail")
    return {"Authorization": f"Bearer {token}", "Accept": "application/json",
            "Content-Type": "application/json"}


def _refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    body = urlencode({
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }).encode("utf-8")
    status, data, _ = _call_transport(
        "POST", "https://oauth2.googleapis.com/token",
        {"Content-Type": "application/x-www-form-urlencoded"}, body, "intake.gmail.token",
    )
    if status != 200 or not isinstance(data, dict) or not data.get("access_token"):
        raise AuthFailed(f"Google OAuth refresh failed ({status})", provider="gmail")
    return str(data["access_token"])


def _check_status(status: int, data: Any, headers: Mapping[str, str], op: str) -> None:
    if status in (401, 403):
        raise AuthFailed(f"Gmail API refused {op} ({status})", provider="gmail")
    if status == 429:
        retry = headers.get("retry-after")
        try:
            wait = float(retry) if retry else None
        except ValueError:
            wait = None
        raise RateLimited(f"Gmail API rate-limited {op}", retry_after=wait, provider="gmail")
    if not 200 <= status < 300:
        raise IntakeError(f"Gmail API {op} answered {status}", provider="gmail")


def _extract_body_text(payload: Mapping[str, Any]) -> str:
    body = payload.get("body") if isinstance(payload.get("body"), Mapping) else {}
    raw_data = body.get("data")
    if isinstance(raw_data, str) and raw_data:
        try:
            pad = "=" * (-len(raw_data) % 4)
            return base64.urlsafe_b64decode(raw_data + pad).decode("utf-8", errors="replace")[:20000]
        except Exception:  # noqa: BLE001
            pass
    for part in payload.get("parts") or []:
        if not isinstance(part, Mapping):
            continue
        mime = str(part.get("mimeType") or "").lower()
        if mime == "text/plain":
            found = _extract_body_text(part)
            if found:
                return found
    for part in payload.get("parts") or []:
        if isinstance(part, Mapping):
            found = _extract_body_text(part)
            if found:
                return found
    return ""


def _parse_gmail_message(conn: IntakeConnection, msg: Mapping[str, Any], *,
                         dedupe_key: str) -> IntakeEvent:
    msg_id = str(msg.get("id") or "")
    thread_id = str(msg.get("threadId") or msg_id)
    payload = msg.get("payload") if isinstance(msg.get("payload"), Mapping) else {}
    headers = normalize_headers(payload.get("headers") or msg.get("headers") or [])
    from_raw = first_header(headers, "from")
    from_display, from_email = parse_from(from_raw)
    subject = first_header(headers, "subject")
    auth_results = headers.get("authentication-results") or []
    dmarc = parse_dmarc(auth_results, from_email)
    auto_sub = is_auto_submitted(headers)
    causality = extract_boss_causality(headers)
    text_body = _extract_body_text(payload) or str(msg.get("snippet") or "")
    internal_ms = msg.get("internalDate")
    time_iso = None
    if internal_ms is not None:
        try:
            time_iso = datetime.fromtimestamp(int(internal_ms) / 1000.0, tz=timezone.utc).isoformat()
        except (TypeError, ValueError):
            time_iso = None
    return IntakeEvent(
        id=msg_id or dedupe_key.rsplit(":", 1)[-1],
        source=f"gmail://{_mailbox(conn)}",
        type="com.google.gmail.message.received",
        subject=thread_id or subject or None,
        time=time_iso,
        data={
            "record_type": "email",
            "record_id": msg_id,
            "thread_id": thread_id,
            "history_id": str(msg.get("historyId") or ""),
            "message_id_header": first_header(headers, "message-id"),
            "subject": subject,
            "text": text_body[:20000],
            "snippet": str(msg.get("snippet") or "")[:1000],
            "from_email": from_email,
            "from_display": from_display,
            "label_ids": list(msg.get("labelIds") or []),
            "dmarc": dmarc,
            "auto_submitted": auto_sub,
            "operation": "created",
        },
        bossconnection=conn.id,
        bossconversation={"thread_id": thread_id, "key": thread_id} if thread_id else None,
        bossactor=ExternalActor(
            kind="user" if from_email else "unknown",
            external_id=from_email,
            display=from_display or from_email,
            email=from_email or None,
        ),
        bosscausality=causality,
        bossdedupe=dedupe_key,
    )


def _decode_pubsub_data(body: Mapping[str, Any]) -> dict[str, Any]:
    msg = body.get("message") if isinstance(body.get("message"), Mapping) else {}
    encoded = msg.get("data")
    if not isinstance(encoded, str) or not encoded:
        return {}
    try:
        pad = "=" * (-len(encoded) % 4)
        decoded = base64.b64decode(encoded + pad).decode("utf-8")
        parsed = json.loads(decoded)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


class GmailIntakeAdapter(IntakeAdapterBase):
    key = "gmail"

    # -- PUSH (Google Cloud Pub/Sub OIDC JWT) -----------------------------------------------------

    def verify(self, conn: IntakeConnection, req: RawRequest, now: float) -> Verified:
        auth = req.header("authorization").strip()
        if not auth or not auth.lower().startswith("bearer "):
            raise VerifyRefused("missing_signature", provider=self.key)
        token = auth.split(" ", 1)[1].strip()
        if not token:
            raise VerifyRefused("missing_signature", provider=self.key)
        audience = str(conn.get("oidc_audience") or "").strip()
        if not audience:
            raise VerifyRefused("not_configured", provider=self.key)
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise VerifyRefused("bad_token", provider=self.key) from None
        kid = str(header.get("kid") or "")
        alg = str(header.get("alg") or "")
        if alg != "RS256":
            raise VerifyRefused("unsupported_alg", provider=self.key)
        jwks = JWKS_LOADER(conn, now)
        jwk_match = next(
            (k for k in (jwks.get("keys") or []) if isinstance(k, dict) and (not kid or k.get("kid") == kid)),
            None,
        )
        if jwk_match is None:
            raise VerifyRefused("unknown_kid", provider=self.key)
        try:
            public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk_match))
            claims = jwt.decode(
                token,
                key=public_key,
                algorithms=["RS256"],
                audience=audience,
                options={"verify_exp": False, "verify_nbf": False, "verify_iat": False},
            )
        except jwt.InvalidAudienceError:
            raise VerifyRefused("bad_audience", provider=self.key) from None
        except jwt.PyJWTError:
            raise VerifyRefused("bad_signature", provider=self.key) from None

        iss = str(claims.get("iss") or "")
        if iss not in GOOGLE_ISSUERS:
            raise VerifyRefused("bad_issuer", provider=self.key)
        exp = claims.get("exp")
        if not isinstance(exp, (int, float)) or float(exp) + 60 < now:
            raise VerifyRefused("token_expired", provider=self.key)
        nbf = claims.get("nbf")
        if isinstance(nbf, (int, float)) and float(nbf) - 60 > now:
            raise VerifyRefused("token_not_yet_valid", provider=self.key)
        if claims.get("email_verified") is not True:
            raise VerifyRefused("unverified_email", provider=self.key)
        expected_sa = str(conn.get("service_account_email") or "").strip().lower()
        got_email = str(claims.get("email") or "").strip().lower()
        if expected_sa and got_email != expected_sa:
            raise VerifyRefused("bad_service_account", provider=self.key)
        return Verified(scheme="google_pubsub_oidc", timestamp=int(claims.get("iat") or now))

    def dedupe_key(self, conn: IntakeConnection, req: RawRequest) -> str:
        try:
            body = json.loads(req.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            body = {}
        msg = body.get("message") if isinstance(body, dict) and isinstance(body.get("message"), dict) else {}
        data = _decode_pubsub_data(body) if isinstance(body, dict) else {}
        hid = str(data.get("historyId") or "")
        addr = str(data.get("emailAddress") or _mailbox(conn)).lower()
        if hid:
            return f"gmail:history:{addr}:{hid}"
        mid = str(msg.get("messageId") or "")
        if mid:
            return f"gmail:pubsub:{mid}"
        return f"gmail:sha256:{req.body_sha256()}"

    # -- PROCESSOR --------------------------------------------------------------------------------

    def parse(self, conn: IntakeConnection, raw: StoredRaw) -> list[IntakeEvent]:
        try:
            body = json.loads(raw.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ParseRefused(f"Gmail body is not JSON: {type(exc).__name__}",
                               provider=self.key) from None
        if not isinstance(body, dict):
            raise ParseRefused("Gmail body is not an object", provider=self.key)
        if body.get("_boss_kind") == "gmail_message" and isinstance(body.get("message"), dict):
            return [_parse_gmail_message(conn, body["message"], dedupe_key=raw.dedupe_key)]
        if isinstance(body.get("message"), dict):
            data = _decode_pubsub_data(body)
            hid = str(data.get("historyId") or "")
            addr = str(data.get("emailAddress") or _mailbox(conn))
            mid = str(body["message"].get("messageId") or hid or "pubsub")
            return [IntakeEvent(
                id=mid,
                source=f"gmail://{addr}",
                type="com.google.gmail.history.notified",
                subject=addr,
                time=str(body["message"].get("publishTime") or "") or None,
                data={"email_address": addr, "history_id": hid,
                      "subscription": str(body.get("subscription") or "")},
                bossconnection=conn.id,
                bossactor=ExternalActor(kind="service", external_id="gmail-push",
                                        display="Gmail Pub/Sub"),
                bossdedupe=raw.dedupe_key,
            )]
        raise ParseRefused("Gmail delivery is neither a fetched message nor a Pub/Sub push",
                           provider=self.key)

    def is_self(self, conn: IntakeConnection, ev: IntakeEvent) -> bool:
        if ev.type == "com.google.gmail.history.notified":
            return False
        own = _mailbox(conn).lower()
        sender = str(ev.bossactor.email or ev.data.get("from_email") or "").strip().lower()
        if own and own != "me" and sender and own == sender:
            return True
        if ev.data.get("auto_submitted") is True:
            return True
        if (ev.bosscausality or {}).get("run_id"):
            return True
        return False

    def conversation_key(self, conn: IntakeConnection, ev: IntakeEvent) -> Optional[str]:
        tid = (ev.bossconversation or {}).get("thread_id") or ev.data.get("thread_id")
        return str(tid) if tid else ev.subject

    def resolve_person(self, conn: IntakeConnection, actor: ExternalActor) -> PersonHint:
        return PersonHint(email=actor.email, display=actor.display)

    # -- SUBSCRIBE / RENEW / UNSUBSCRIBE (users.watch / users.stop) -------------------------------

    def subscribe(self, conn: IntakeConnection, spec: SubscriptionSpec) -> Subscription:
        headers = _auth_headers(conn)
        topic = str(spec.resource or conn.get("pubsub_topic") or "").strip()
        if not topic:
            raise ParseRefused("Gmail watch requires 'pubsub_topic'", provider=self.key)
        mb = quote(_mailbox(conn), safe="@.")
        url = f"{_api_base(conn)}/gmail/v1/users/{mb}/watch"
        payload = {"topicName": topic, "labelIds": _label_ids(conn)}
        status, data, resp_headers = _call_transport(
            "POST", url, headers, json.dumps(payload).encode("utf-8"), "intake.gmail.watch",
        )
        _check_status(status, data, resp_headers, "watch")
        if not isinstance(data, dict) or not data.get("expiration"):
            raise IntakeError("Gmail users.watch returned no expiration", provider=self.key)
        exp_ms = int(data["expiration"])
        exp_iso = datetime.fromtimestamp(exp_ms / 1000.0, tz=timezone.utc).isoformat()
        hid = str(data.get("historyId") or "")
        return Subscription(
            provider_subscription_id=f"gmail-watch:{_mailbox(conn)}:{hid}",
            resource=topic,
            expires_at=exp_iso,
        )

    def renew(self, conn: IntakeConnection, sub: Subscription) -> Subscription:
        return self.subscribe(conn, SubscriptionSpec(resource=sub.resource))

    def unsubscribe(self, conn: IntakeConnection, sub: Subscription) -> None:
        headers = _auth_headers(conn)
        mb = quote(_mailbox(conn), safe="@.")
        url = f"{_api_base(conn)}/gmail/v1/users/{mb}/stop"
        status, data, resp_headers = _call_transport("POST", url, headers, b"{}",
                                                     "intake.gmail.stop")
        if status == 404:
            return
        _check_status(status, data, resp_headers, "stop")

    # -- POLL (history.list + 404 bounded 7-day full sync) ----------------------------------------

    def _fetch_message(self, conn: IntakeConnection, headers: Mapping[str, str],
                       msg_id: str) -> Optional[dict[str, Any]]:
        mb = quote(_mailbox(conn), safe="@.")
        url = f"{_api_base(conn)}/gmail/v1/users/{mb}/messages/{quote(msg_id, safe='')}?format=full"
        status, data, resp_headers = _call_transport("GET", url, headers, None,
                                                     "intake.gmail.message")
        if status == 404:
            return None
        _check_status(status, data, resp_headers, "message")
        return data if isinstance(data, dict) else None

    def _profile_history_id(self, conn: IntakeConnection, headers: Mapping[str, str]) -> Optional[str]:
        mb = quote(_mailbox(conn), safe="@.")
        url = f"{_api_base(conn)}/gmail/v1/users/{mb}/profile"
        status, data, resp_headers = _call_transport("GET", url, headers, None,
                                                     "intake.gmail.profile")
        _check_status(status, data, resp_headers, "profile")
        if isinstance(data, dict) and data.get("historyId"):
            return str(data["historyId"])
        return None

    def _bounded_full_sync(self, conn: IntakeConnection, headers: Mapping[str, str],
                           limit: int) -> tuple[list[str], Optional[str]]:
        """Bounded 7-day full sync (EVENTING_LLD.md §13, P9c-T2) when historyId is 404 or empty."""
        mb = quote(_mailbox(conn), safe="@.")
        cap = max(1, min(int(limit or MAX_RESYNC_MESSAGES), MAX_RESYNC_MESSAGES))
        qs = urlencode({"q": RESYNC_QUERY, "maxResults": cap})
        url = f"{_api_base(conn)}/gmail/v1/users/{mb}/messages?{qs}"
        status, data, resp_headers = _call_transport("GET", url, headers, None,
                                                     "intake.gmail.full_sync")
        _check_status(status, data, resp_headers, "full_sync")
        ids = [str(m["id"]) for m in ((data or {}).get("messages") or [])
               if isinstance(m, dict) and m.get("id")][:cap]
        latest_hid = self._profile_history_id(conn, headers)
        return ids, latest_hid

    def poll(self, conn: IntakeConnection, cursor: Cursor, limit: int) -> PollResult:
        headers = _auth_headers(conn)
        mb = quote(_mailbox(conn), safe="@.")
        cap = max(1, min(int(limit or 50), MAX_RESYNC_MESSAGES))
        message_ids: list[str] = []
        next_hid = cursor.value
        full_synced = False

        if cursor.value:
            qs = urlencode({"startHistoryId": cursor.value, "historyTypes": "messageAdded",
                            "maxResults": cap})
            url = f"{_api_base(conn)}/gmail/v1/users/{mb}/history?{qs}"
            status, data, resp_headers = _call_transport("GET", url, headers, None,
                                                         "intake.gmail.history")
            if status == 404:
                # historyId too old (P9c-T2): bounded 7-day full sync; inbox dedupe absorbs overlap.
                message_ids, latest_hid = self._bounded_full_sync(conn, headers, cap)
                next_hid = latest_hid or cursor.value
                full_synced = True
            else:
                _check_status(status, data, resp_headers, "history")
                if isinstance(data, dict):
                    if data.get("historyId"):
                        next_hid = str(data["historyId"])
                    seen: set[str] = set()
                    for entry in data.get("history") or []:
                        if not isinstance(entry, dict):
                            continue
                        for added in entry.get("messagesAdded") or []:
                            msg = added.get("message") if isinstance(added, dict) else None
                            mid = str((msg or {}).get("id") or "")
                            if mid and mid not in seen:
                                seen.add(mid)
                                message_ids.append(mid)
                                if len(message_ids) >= cap:
                                    break
        else:
            message_ids, latest_hid = self._bounded_full_sync(conn, headers, cap)
            next_hid = latest_hid
            full_synced = True

        deliveries: list[StoredRaw] = []
        events: list[IntakeEvent] = []
        for mid in message_ids[:cap]:
            msg = self._fetch_message(conn, headers, mid)
            if msg is None:
                continue
            dk = f"gmail:{mid}"
            wrapped = {"_boss_kind": "gmail_message", "message": msg,
                       "resync": full_synced}
            raw_bytes = json.dumps(wrapped, sort_keys=True).encode("utf-8")
            deliveries.append(StoredRaw(body=raw_bytes,
                                        headers={"content-type": "application/json"},
                                        dedupe_key=dk))
            events.append(_parse_gmail_message(conn, msg, dedupe_key=dk))
            if not next_hid and msg.get("historyId"):
                next_hid = str(msg["historyId"])

        now_iso = datetime.now(timezone.utc).isoformat()
        return PollResult(
            events=tuple(events),
            deliveries=tuple(deliveries),
            next_cursor=Cursor(stream_key=cursor.stream_key, value=next_hid, cursor_time=now_iso),
            more=False,
        )

    def health(self, conn: IntakeConnection) -> Health:
        try:
            headers = _auth_headers(conn)
            hid = self._profile_history_id(conn, headers)
            return Health(ok=True, detail="Gmail profile reachable",
                          checks={"mailbox": _mailbox(conn), "history_id": hid})
        except Exception as exc:  # noqa: BLE001 — never leak credentials
            return Health(ok=False, detail=f"{type(exc).__name__}: {exc}"[:200])


__all__ = [
    "GMAIL_API_BASE",
    "GOOGLE_ISSUERS",
    "GOOGLE_JWKS_URL",
    "GmailIntakeAdapter",
    "JWKS_LOADER",
    "MAX_RESYNC_MESSAGES",
    "RESYNC_QUERY",
    "TRANSPORT",
]
