"""Microsoft Teams on the intake layer (P9b-1, BOSSAPP-1100; boss-eventing-lld.md §3.7, §4.2, §6.2, §9.3).

Single-tenant Azure Bot (Decision D5):
  * verify     RS256 OIDC JWT in ``Authorization: Bearer <token>`` against Bot Framework
               OpenID configuration + JWKS (cached <= 24h), verifying ``iss``, ``aud == app_id``,
               ``nbf``/``exp`` (+/- 5 min skew), and ``serviceUrl`` matching ``activity.serviceUrl``.
               Zero JWTs or app secrets ever enter ``intake_events.headers`` or logs.
  * dedupe     ``activity.id`` (Teams retries undelivered activities within 15 s).
  * parse      Normalizes Bot Framework Activity (``message``, ``messageReaction``, ``invoke``)
               into CloudEvents ``IntakeEvent`` with ``conversation_ref`` and Entra ``aad_oid`` /
               ``tenant_id`` / guest signals.
  * loop       Bot echo prevention via ``from.id``, ``bot_id``, ``app_id``, and ``channelData.boss``
               causality marker.
  * reply      Delegates outbound / streaming ops to ``TeamsChat``.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

import jwt
from jwt.algorithms import RSAAlgorithm

from ..base import IntakeAdapterBase
from ..contract import (
    ExternalActor,
    IntakeConnection,
    IntakeEvent,
    OutboxOp,
    ParseRefused,
    PersonHint,
    RawRequest,
    Receipt,
    SecretView,
    StoredRaw,
    Verified,
    VerifyRefused,
)

BOTFRAMEWORK_OPENID_URL = "https://login.botframework.com/v1/.well-known/openidconfiguration"
BOTFRAMEWORK_ISSUERS = frozenset({
    "https://api.botframework.com",
    "https://api.botframework.com/",
})
JWKS_TTL_SECONDS = 86400  # 24 hours max cache per LLD §9.3
JWT_LEEWAY_SECONDS = 300  # +/- 5 min clock skew
MAX_TEXT = 40000
_AT_TAG = re.compile(r"<at\b[^>]*>.*?</at>", re.I | re.S)


def _default_oidc_fetch(url: str) -> dict[str, Any]:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("https", "http"):
        raise ValueError(f"unsupported scheme for OIDC URL: {url}")
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


#: Seam for tests (fake Bot Framework OIDC + JWKS server). (url) -> parsed JSON dict.
OIDC_FETCHER: Callable[[str], dict[str, Any]] = _default_oidc_fetch

_JWKS_LOCK = threading.Lock()
_JWKS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def reset_jwks_cache() -> None:
    with _JWKS_LOCK:
        _JWKS_CACHE.clear()


def _jwks_for(oidc_url: str, now: float) -> dict[str, Any]:
    with _JWKS_LOCK:
        entry = _JWKS_CACHE.get(oidc_url)
        if entry is not None and entry[0] > now:
            return entry[1]
    meta = OIDC_FETCHER(oidc_url)
    jwks_uri = str((meta or {}).get("jwks_uri") or "").strip()
    if not jwks_uri or not _oidc_url_allowed(jwks_uri):
        raise ValueError("OIDC metadata missing or disallowed jwks_uri")
    jwks = OIDC_FETCHER(jwks_uri)
    keys_by_kid: dict[str, Any] = {}
    for jwk in (jwks or {}).get("keys") or []:
        if not isinstance(jwk, dict):
            continue
        kid = str(jwk.get("kid") or "")
        if kid:
            keys_by_kid[kid] = jwk
    with _JWKS_LOCK:
        _JWKS_CACHE[oidc_url] = (now + JWKS_TTL_SECONDS, keys_by_kid)
    return keys_by_kid


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _oidc_url_allowed(url: str) -> bool:
    """The OIDC metadata URL is fetched while answering a PUBLIC request: https only, except a
    loopback fake (tests and local dev) may use http."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return True
    return parts.scheme == "http" and (parts.hostname or "") in _LOOPBACK_HOSTS


def _body(raw: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads((raw or b"{}").decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _norm_url(url: str) -> str:
    return str(url or "").strip().rstrip("/").lower()


def _day(ts: str) -> str:
    if ts:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _strip_at(text: str, bot_names: tuple[str, ...]) -> str:
    cleaned = str(text or "")
    for name in bot_names:
        if name:
            cleaned = re.sub(rf"<at\b[^>]*>\s*{re.escape(name)}\s*</at>[\s,:]*", "", cleaned, flags=re.I)
    cleaned = _AT_TAG.sub("", cleaned)
    return re.sub(r"^\s*[,:]+\s*", "", cleaned).strip()


def _mentions_bot(activity: Mapping[str, Any], bot_ids: set[str]) -> bool:
    for ent in activity.get("entities") or []:
        if not isinstance(ent, dict):
            continue
        if str(ent.get("type") or "").lower() == "mention":
            mentioned = ent.get("mentioned") or {}
            if isinstance(mentioned, dict) and str(mentioned.get("id") or "") in bot_ids:
                return True
    return False


class TeamsIntakeAdapter(IntakeAdapterBase):
    key = "teams"

    # -- PUSH -------------------------------------------------------------------------------------

    def verify(self, conn: IntakeConnection, req: RawRequest, now: float) -> Verified:
        auth = req.header("authorization").strip()
        if not auth.lower().startswith("bearer "):
            raise VerifyRefused("missing_signature", provider=self.key)
        token = auth[7:].strip()
        if not token:
            raise VerifyRefused("missing_signature", provider=self.key)
        app_id = str(conn.get("app_id") or "").strip()
        if not app_id:
            raise VerifyRefused("not_configured", provider=self.key)

        try:
            unverified_hdr = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise VerifyRefused("bad_signature", provider=self.key) from None
        kid = str(unverified_hdr.get("kid") or "")
        alg = str(unverified_hdr.get("alg") or "")
        if not kid or alg != "RS256":
            raise VerifyRefused("bad_signature", provider=self.key)

        oidc_url = str(conn.get("oidc_config_url") or BOTFRAMEWORK_OPENID_URL).strip()
        if not _oidc_url_allowed(oidc_url):
            raise VerifyRefused("not_configured", provider=self.key)
        try:
            keys = _jwks_for(oidc_url, now)
            jwk = keys.get(kid)
            if jwk is None:
                # One refresh in case Bot Framework rotated keys
                with _JWKS_LOCK:
                    _JWKS_CACHE.pop(oidc_url, None)
                keys = _jwks_for(oidc_url, now)
                jwk = keys.get(kid)
            if jwk is None:
                raise VerifyRefused("bad_signature", provider=self.key)
            public_key = RSAAlgorithm.from_jwk(json.dumps(jwk))
            claims = jwt.decode(
                token,
                key=public_key,
                algorithms=["RS256"],
                audience=app_id,
                leeway=JWT_LEEWAY_SECONDS,
                options={
                    "require": ["exp", "iss", "aud"],
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_aud": True,
                },
            )
        except VerifyRefused:
            raise
        except jwt.ExpiredSignatureError:
            raise VerifyRefused("stale_timestamp", provider=self.key) from None
        except jwt.ImmatureSignatureError:
            raise VerifyRefused("stale_timestamp", provider=self.key) from None
        except jwt.PyJWTError:
            raise VerifyRefused("bad_signature", provider=self.key) from None
        except Exception:
            raise VerifyRefused("bad_signature", provider=self.key) from None

        allowed_iss = set(BOTFRAMEWORK_ISSUERS)
        extra_iss = str(conn.get("allowed_issuer") or "").strip()
        if extra_iss:
            allowed_iss.add(extra_iss)
        iss = str(claims.get("iss") or "").strip()
        if iss not in allowed_iss and iss.rstrip("/") not in {x.rstrip("/") for x in allowed_iss}:
            raise VerifyRefused("bad_signature", provider=self.key)

        body = _body(req.body)
        # Bot Framework names the claim `serviceurl` (lower case); accept the camel-case spelling too.
        claim_svc = _norm_url(str(claims.get("serviceurl") or claims.get("serviceUrl") or ""))
        act_svc = _norm_url(str(body.get("serviceUrl") or ""))
        # The activity's serviceUrl is where BOSS later sends a bearer token: it must be the one
        # Bot Framework signed. An activity that names a serviceUrl the token does not is refused.
        if act_svc and claim_svc != act_svc:
            raise VerifyRefused("bad_signature", provider=self.key)

        tenant_id = str(conn.get("tenant_id") or "").strip()
        if tenant_id:
            ch_data = body.get("channelData") if isinstance(body.get("channelData"), dict) else {}
            conv = body.get("conversation") if isinstance(body.get("conversation"), dict) else {}
            act_tenant = str(
                (ch_data.get("tenant") or {}).get("id")
                or conv.get("tenantId")
                or ""
            ).strip()
            if act_tenant and act_tenant.lower() != tenant_id.lower():
                raise VerifyRefused("bad_signature", provider=self.key)

        exp = claims.get("exp")
        return Verified(
            scheme="teams_oidc_jwt",
            timestamp=int(exp) if isinstance(exp, (int, float)) else None,
        )

    def dedupe_key(self, conn: IntakeConnection, req: RawRequest) -> str:
        body = _body(req.body)
        act_id = str(body.get("id") or "").strip()[:200]
        if act_id:
            # Channel-message activity ids are millisecond timestamps, unique only within a
            # conversation: the conversation id is part of the key so two channels never collide.
            conv = body.get("conversation") if isinstance(body.get("conversation"), dict) else {}
            conv_id = str(conv.get("id") or "").strip()[:250]
            return f"teams:{conv_id}:{act_id}" if conv_id else f"teams:{act_id}"
        return "teams:sha256:" + req.body_sha256()

    # -- PROCESSOR --------------------------------------------------------------------------------

    def parse(self, conn: IntakeConnection, raw: StoredRaw) -> list[IntakeEvent]:
        body = _body(raw.body)
        if not body:
            raise ParseRefused("Teams body is not a JSON object", provider=self.key)
        act_type = str(body.get("type") or "").strip()
        if act_type not in ("message", "messageReaction", "invoke"):
            return []

        conv = body.get("conversation") if isinstance(body.get("conversation"), dict) else {}
        ch_data = body.get("channelData") if isinstance(body.get("channelData"), dict) else {}
        sender = body.get("from") if isinstance(body.get("from"), dict) else {}
        recipient = body.get("recipient") if isinstance(body.get("recipient"), dict) else {}

        conv_id = str(conv.get("id") or "").strip()
        conv_type = str(
            conv.get("conversationType")
            or ("channel" if (conv.get("isGroup") or conv_id.startswith("19:")) else "personal")
        ).strip()
        tenant_id = str(
            (ch_data.get("tenant") or {}).get("id")
            or conv.get("tenantId")
            or sender.get("tenantId")
            or ""
        ).strip()
        team_id = str((ch_data.get("team") or {}).get("id") or "").strip()
        channel_id = str((ch_data.get("channel") or {}).get("id") or conv_id).strip()
        service_url = str(body.get("serviceUrl") or "").strip()
        act_id = str(body.get("id") or raw.dedupe_key.split(":", 1)[-1]).strip()
        reply_to = str(body.get("replyToId") or "").strip()
        ts = str(body.get("timestamp") or body.get("localTimestamp") or "").strip()

        bot_ids = {
            str(x).strip()
            for x in (
                conn.get("bot_id"),
                conn.get("app_id"),
                recipient.get("id"),
            )
            if x
        }
        bot_names = tuple(
            str(x).strip()
            for x in (recipient.get("name"), "BOSS")
            if x
        )
        raw_text = str(body.get("text") or "")[:MAX_TEXT]
        mentions_bot = _mentions_bot(body, bot_ids)

        if act_type == "invoke":
            ev_type = "com.microsoft.teams.card.action"
        elif act_type == "messageReaction":
            ev_type = "com.microsoft.teams.reaction"
        elif conv_type == "personal":
            ev_type = "com.microsoft.teams.message.personal"
        elif mentions_bot:
            ev_type = "com.microsoft.teams.message.mention"
        elif reply_to and reply_to != act_id:
            ev_type = "com.microsoft.teams.message.thread_reply"
        else:
            ev_type = "com.microsoft.teams.message.channel"

        if conv_type == "personal" and not reply_to:
            conv_key = f"{conv_id}/{_day(ts)}"
            reply_thread = act_id
        else:
            root_id = reply_to or act_id
            conv_key = f"{conv_id}/{root_id}"
            reply_thread = root_id

        sender_id = str(sender.get("id") or "").strip()
        aad_oid = str(sender.get("aadObjectId") or "").strip()
        sender_name = str(sender.get("name") or "").strip()
        sender_role = str(sender.get("role") or "").lower()
        sender_email = str(sender.get("email") or "").strip() or None
        user_type = str(sender.get("userType") or "").strip()
        is_guest_flag = bool(sender.get("isGuest") is True or user_type.lower() == "guest")

        if sender_role == "bot" or (sender_id and sender_id in bot_ids):
            actor = ExternalActor(kind="bot", external_id=sender_id, display=sender_name)
        elif sender_id or aad_oid:
            actor = ExternalActor(
                kind="user",
                external_id=sender_id or aad_oid,
                display=sender_name,
                email=sender_email,
            )
        else:
            actor = ExternalActor(kind="unknown")

        cleaned_text = _strip_at(raw_text, bot_names) if (mentions_bot or conv_type == "personal") else raw_text
        if act_type == "invoke" and not cleaned_text and isinstance(body.get("value"), dict):
            cleaned_text = json.dumps(body["value"])[:MAX_TEXT]

        conversation_ref = {
            "serviceUrl": service_url,
            "conversation": {
                "id": conv_id,
                "conversationType": conv_type,
                "tenantId": tenant_id or None,
            },
            "bot": {
                "id": str(recipient.get("id") or conn.get("bot_id") or ""),
                "name": str(recipient.get("name") or "BOSS"),
            },
            "tenantId": tenant_id or None,
            "channelId": str(body.get("channelId") or "msteams"),
            "lastActivityId": act_id,
        }

        data: dict[str, Any] = {
            "text": cleaned_text,
            "raw_text": raw_text,
            "channel": conv_id,
            "channel_id": channel_id,
            "channel_type": "im" if conv_type == "personal" else conv_type,
            "conversation_type": conv_type,
            "ts": act_id,
            "thread_ts": reply_thread,
            "reply_to_id": reply_to or None,
            "team_id": team_id or None,
            "tenant_id": tenant_id or None,
            "aad_oid": aad_oid or None,
            "user_type": user_type or None,
            "is_guest": is_guest_flag,
            "service_url": service_url or None,
            "bot_id": str(recipient.get("id") or conn.get("bot_id") or "") or None,
            "mentions_bot": mentions_bot,
            "addressed": mentions_bot or conv_type == "personal",
            "value": body.get("value") if isinstance(body.get("value"), dict) else None,
        }

        out = IntakeEvent(
            id=act_id,
            source=f"teams://{tenant_id or 'unknown'}",
            type=ev_type,
            subject=conv_key,
            time=ts or None,
            data=data,
            bossactor=actor,
            bossconnection=conn.id,
            bossdedupe=raw.dedupe_key,
            bossconversation={
                "key": conv_key,
                "channel": conv_id,
                "thread_ts": reply_thread,
                "reply_thread_ts": reply_thread,
                "team_id": team_id or None,
                "tenant_id": tenant_id or None,
                "service_url": service_url or None,
                "conversation_ref": conversation_ref,
            },
        )
        marker = ch_data.get("boss") if isinstance(ch_data.get("boss"), dict) else None
        if marker:
            out.bosscausality = {
                "run_id": marker.get("run_id"),
                "deployment_id": marker.get("deployment_id"),
                "hops": int(marker.get("hops") or 0),
                "endpoint_id": marker.get("endpoint_id"),
            }
        return [out]

    def is_self(self, conn: IntakeConnection, ev: IntakeEvent) -> bool:
        data = ev.data or {}
        ours = {
            str(x)
            for x in (conn.get("bot_id"), conn.get("app_id"), data.get("bot_id"))
            if x
        }
        if ev.bossactor.external_id and ev.bossactor.external_id in ours:
            return True
        causality = ev.bosscausality or {}
        endpoint = str(conn.get("chat_endpoint_id") or "")
        return bool(causality) and bool(endpoint) and causality.get("endpoint_id") == endpoint

    def resolve_person(self, conn: IntakeConnection, actor: ExternalActor) -> PersonHint:
        return PersonHint(email=actor.email, display=actor.display, is_bot=actor.kind == "bot")

    def conversation_key(self, conn: IntakeConnection, ev: IntakeEvent) -> Optional[str]:
        return (ev.bossconversation or {}).get("key") or ev.subject

    # -- REPLY ------------------------------------------------------------------------------------

    def _item(self, op: OutboxOp) -> dict[str, Any]:
        conv = dict(op.conversation or {})
        item: dict[str, Any] = {
            "op": op.op,
            "channel": conv.get("channel"),
            "service_url": conv.get("service_url"),
            "conversation_ref": conv.get("conversation_ref"),
            **dict(op.message or {}),
        }
        if conv.get("thread_ts") and "thread" not in item:
            item["thread"] = conv["thread_ts"]
        if "text" in item and "message" not in item and op.op in ("post", "update"):
            item["message"] = {"text": item.pop("text")}
        return item

    def send(self, conn: IntakeConnection, op: OutboxOp, secrets: SecretView) -> Receipt:
        from ....services.chat_adapters import TeamsChat

        cfg = {
            "app_id": conn.get("app_id"),
            "tenant_id": conn.get("tenant_id"),
            "bot_id": conn.get("bot_id"),
            "service_url": conn.get("service_url"),
            "token_url": conn.get("token_url"),
        }
        app_pw = secrets.get("app_password") or secrets.get("bot_token") or ""
        receipt = TeamsChat().send_with_config(app_pw, self._item(op), cfg)
        return Receipt(ok=True, ref=dict(receipt or {}))

    def send_stream(self, conn: IntakeConnection, op: OutboxOp, secrets: SecretView) -> Receipt:
        if op.op not in ("stream_start", "stream_append", "stream_stop"):
            raise ValueError(f"not a stream op: {op.op}")
        return self.send(conn, op, secrets)


__all__ = [
    "BOTFRAMEWORK_ISSUERS",
    "BOTFRAMEWORK_OPENID_URL",
    "OIDC_FETCHER",
    "TeamsIntakeAdapter",
    "reset_jwks_cache",
]
