"""Decide from Slack and phone: two-way chat channels (BOSSAPP-833 / 834).

Before this, chat was OUTBOUND only (slack_adapter.py posted a webhook) and an answer came back
as free text a model read (comms.decide_reply). Nothing bound a reply to the person who sent it,
and nothing stopped a forwarded message from being answered by anyone.

THE FLOW
--------
1. A decision is created (`decision.created`, services/core._after_event) -> one `decision_card`
   row per active chat endpoint of the project, in the chat outbox (notify.enqueue_chat).
2. The outbox sends it (notify.deliver_chat_due -> `deliver`): the card is rendered AT SEND TIME
   from the live decision (an answered decision is not posted), each button carrying a freshly
   minted SIGNED, SINGLE-USE action token (`chat_actions`), and the posted message is bound to
   the decision (`chat_threads`).
3. A press arrives at the endpoint's public route. The adapter verifies the channel's signature
   first (chat_adapters), then `handle_inbound`:
     * the token's HMAC is checked against its row, the row must be this endpoint's, pending and
       unexpired, and the button's own action must match the token's;
     * the presser must be IDENTITY-LINKED (chat_identity_links, verified). An unlinked presser
       is DMed a one-time link: they sign in to the console and confirm (`confirm_link`);
     * a decision above the endpoint's `max_tier` (default 2: tier 3 needs two people and a
       fresh sign-in) is NEVER executed from chat: the answer is the console deep link;
     * an action that needs a note opens the channel's form (a Slack modal); the token is spent
       only on submit;
     * the token is spent by compare-and-swap (pending -> used): a replayed or double press
       refuses with words;
     * DecisionService.transition runs AS THE LINKED PERSON (`decide_as_person`) — the seat,
       the two-person rule and the member rules apply exactly as in the console — and the press
       is recorded (`chat.decision_answered` event, the token row's outcome).
4. The card is updated in place (Approved by …) and the presser gets a private answer, both
   through the outbox.

Secrets: an endpoint's credentials go THROUGH secrets_service.put_secret at creation and are
resolved at the moment of use (secret_backends.resolve_secret_ref). No value is stored in a
column, an event, a log line or a response.
"""
from __future__ import annotations

import base64
import contextvars
import hashlib
import hmac
import json
import logging
import secrets as _secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from ..ports import sql
from .chat_adapters import (ChatInbound, ChatSendFailed, ChatVerifyRefused, DecisionCard,
                            adapter_for)
from .core import emit_event, make_id

log = logging.getLogger(__name__)

KINDS = ("slack", "telegram", "discord")
ACTOR = "boss.chat"
ACTION_TTL = timedelta(hours=72)
LINK_CODE_TTL = timedelta(minutes=30)
#: Above this tier a decision is answered in the console, never from chat (tier 3: two people
#: and a fresh sign-in). An endpoint may LOWER it (`config.max_tier`), never raise it past 2.
DEFAULT_MAX_TIER = 2
#: Only these are offered as buttons; everything else a decision offers is in the console.
CHAT_ACTIONS = ("approve", "reject")
_TOKEN_PREFIX = "cha_"


class ChatRefused(Exception):
    """A chat request refused with words for the person (and a code for the log)."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(code)
        self.code, self.message, self.status = code, message, status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(v: Any, default: Any) -> Any:
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def _base_url(settings: Any) -> str:
    return str(getattr(settings, "public_url", "") or getattr(settings, "app_base_url", "")
               or "").rstrip("/")


# --------------------------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------------------------- #
def _endpoint_view(row: Mapping[str, Any]) -> dict[str, Any]:
    refs = _json(row.get("secret_refs"), {})
    kind = row["kind"]
    return {"id": row["id"], "project_id": row["project_id"], "kind": kind, "name": row["name"],
            "config": _json(row.get("config"), {}), "status": row["status"],
            "secrets_set": sorted(refs.keys()),
            "stub": bool(getattr(adapter_for(kind), "STUB", False)),
            "created_by": row.get("created_by"),
            "created_at": row["created_at"].isoformat() if row.get("created_at") else None}


def endpoint_urls(base: str, endpoint: Mapping[str, Any]) -> dict[str, str]:
    """The URL an operator pastes into the channel's own app settings. One URL serves Slack's
    Interactivity AND Event Subscriptions (a form body is an interaction, JSON an event)."""
    root = f"{base}/v1/webhooks/inbound/chat/{endpoint['id']}"
    if endpoint["kind"] == "slack":
        return {"interactivity_request_url": root, "event_subscriptions_request_url": root}
    return {"webhook_url": root}


def create_endpoint(connection, settings: Any, *, project_id: str, kind: str, name: str,
                    config: Optional[dict], secrets: Mapping[str, str], actor: str) -> dict:
    if kind not in KINDS:
        raise ChatRefused("bad_kind", f"kind must be one of {', '.join(KINDS)}", 422)
    adapter = adapter_for(kind)
    missing = [n for n in adapter.secret_names if not (secrets.get(n) or "").strip()]
    if missing:
        raise ChatRefused("secrets_required", f"{kind} needs: {', '.join(missing)}", 422)
    unknown = sorted(set(secrets) - set(adapter.secret_names))
    if unknown:
        raise ChatRefused("unknown_secret", f"{kind} takes no secret named {', '.join(unknown)}",
                          422)
    cfg = dict(config or {})
    if kind in ("slack", "telegram") and not str(cfg.get("channel") or "").strip():
        raise ChatRefused("channel_required",
                          "config.channel is required: the Slack channel id or Telegram chat id "
                          "decision cards are posted to", 422)
    tier = cfg.get("max_tier", DEFAULT_MAX_TIER)
    try:
        tier = int(tier)
    except (TypeError, ValueError):
        raise ChatRefused("bad_max_tier", "config.max_tier must be a number", 422) from None
    cfg["max_tier"] = max(0, min(tier, DEFAULT_MAX_TIER))
    endpoint_id = make_id("che")
    refs: dict[str, str] = {}
    if adapter.secret_names:
        from .secret_backends import default_secret_backend
        from .secrets_service import put_secret

        backend = default_secret_backend(settings)
        for secret_name in adapter.secret_names:
            stored = put_secret(connection, settings, project_id=project_id,
                                name=f"chat_{endpoint_id}_{secret_name}",
                                value=secrets[secret_name].strip(), secret_kind="api_token",
                                backend=backend, actor=actor)
            refs[secret_name] = stored["secret_ref_id"]
    row = connection.execute(sql(
        "INSERT INTO chat_endpoints (id, project_id, kind, name, config, secret_refs, status, "
        " created_by) VALUES (:id, :p, :k, :n, CAST(:c AS JSONB), CAST(:r AS JSONB), 'active', :by) "
        "RETURNING *"),
        {"id": endpoint_id, "p": project_id, "k": kind, "n": (name or kind).strip()[:200],
         "c": json.dumps(cfg), "r": json.dumps(refs), "by": actor}).mappings().first()
    emit_event(connection, project_id, "chat.endpoint_created", actor, "chat_endpoint",
               endpoint_id, {"kind": kind, "name": row["name"],
                             "secrets_set": sorted(refs.keys())})
    view = _endpoint_view(row)
    view["urls"] = endpoint_urls(_base_url(settings), row)
    return view


def list_endpoints(connection, project_id: str, settings: Any = None) -> list[dict]:
    rows = connection.execute(sql(
        "SELECT * FROM chat_endpoints WHERE project_id = :p ORDER BY created_at"),
        {"p": project_id}).mappings().all()
    base = _base_url(settings) if settings is not None else ""
    return [{**_endpoint_view(r), "urls": endpoint_urls(base, r)} for r in rows]


def set_endpoint_status(connection, project_id: str, endpoint_id: str, status: str,
                        actor: str) -> dict:
    if status not in ("active", "disabled"):
        raise ChatRefused("bad_status", "status is active or disabled", 422)
    row = connection.execute(sql(
        "UPDATE chat_endpoints SET status = :s, updated_at = now() WHERE id = :i AND "
        "project_id = :p RETURNING *"), {"s": status, "i": endpoint_id, "p": project_id}
    ).mappings().first()
    if row is None:
        raise ChatRefused("not_found", "No such chat endpoint in this project", 404)
    emit_event(connection, project_id, "chat.endpoint_" + status, actor, "chat_endpoint",
               endpoint_id, {"kind": row["kind"]})
    return _endpoint_view(row)


def get_endpoint(connection, endpoint_id: str) -> Optional[dict]:
    row = connection.execute(sql("SELECT * FROM chat_endpoints WHERE id = :i"),
                             {"i": endpoint_id}).mappings().first()
    return dict(row) if row else None


def endpoint_secret(connection, settings: Any, endpoint: Mapping[str, Any], name: str) -> str:
    """Resolve one of the endpoint's secrets at the moment of use. Never logged or returned."""
    from .secret_backends import resolve_secret_ref

    ref = _json(endpoint.get("secret_refs"), {}).get(name)
    if not ref:
        return ""
    return resolve_secret_ref(connection, ref, settings, project_id=endpoint["project_id"])


def verify_request(connection, settings: Any, endpoint: Optional[Mapping[str, Any]],
                   headers: Mapping[str, str], raw_body: bytes,
                   now: Optional[float] = None) -> None:
    """THE VERIFIER of every public chat route: the channel's signature, before anything else."""
    if endpoint is None or endpoint.get("status") != "active":
        raise ChatVerifyRefused("unknown_endpoint")
    adapter = adapter_for(endpoint["kind"])
    secret = endpoint_secret(connection, settings, endpoint, adapter.verify_secret) \
        if adapter.verify_secret else ""
    adapter.verify(secret, headers, raw_body, now=now)


# --------------------------------------------------------------------------------------------- #
# Signed single-use action tokens
# --------------------------------------------------------------------------------------------- #
def _signing_key() -> bytes:
    from ..identity.authn_support import server_key

    return hmac.new(server_key(), b"boss.chat.action.v1", hashlib.sha256).digest()


def _sign(action_id: str, endpoint_id: str, decision_id: str, action: str,
          expires_at: datetime) -> str:
    msg = "|".join((action_id, endpoint_id, decision_id, action,
                    str(int(expires_at.timestamp())))).encode()
    digest = hmac.new(_signing_key(), msg, hashlib.sha256).digest()
    # 128 bits, base64url: the whole token stays under Telegram's 64-byte callback_data.
    return base64.urlsafe_b64encode(digest[:16]).decode().rstrip("=")


def mint_action(connection, endpoint: Mapping[str, Any], decision: Mapping[str, Any],
                action: str, now: Optional[datetime] = None) -> str:
    now = now or _now()
    action_id = _TOKEN_PREFIX + _secrets.token_hex(8)
    expires = (now + ACTION_TTL).replace(microsecond=0)
    connection.execute(sql(
        "INSERT INTO chat_actions (id, endpoint_id, project_id, decision_id, action, status, "
        " expires_at) VALUES (:id, :e, :p, :d, :a, 'pending', :x)"),
        {"id": action_id, "e": endpoint["id"], "p": decision["project_id"],
         "d": decision["id"], "a": action, "x": expires})
    return f"{action_id}.{_sign(action_id, endpoint['id'], decision['id'], action, expires)}"


def read_action(connection, endpoint_id: str, token: str,
                now: Optional[datetime] = None) -> dict:
    """The token's row, after its signature and binding are checked. Spends nothing."""
    now = now or _now()
    action_id, _, sig = (token or "").partition(".")
    if not action_id.startswith(_TOKEN_PREFIX) or not sig:
        raise ChatRefused("bad_token", "That button is not one BOSS sent.", 403)
    row = connection.execute(sql("SELECT * FROM chat_actions WHERE id = :i"),
                             {"i": action_id}).mappings().first()
    if row is None:
        raise ChatRefused("bad_token", "That button is not one BOSS sent.", 403)
    expected = _sign(row["id"], row["endpoint_id"], row["decision_id"], row["action"],
                     row["expires_at"])
    if not hmac.compare_digest(expected, sig) or row["endpoint_id"] != endpoint_id:
        raise ChatRefused("bad_token", "That button is not one BOSS sent.", 403)
    row = dict(row)
    if row["status"] == "used":
        raise ChatRefused("already_used", "That button was already used. Each works once.", 409)
    if row["status"] == "expired" or row["expires_at"] <= now:
        raise ChatRefused("expired", "That button has expired. Open the decision in BOSS.", 410)
    return row


def spend_action(connection, action_id: str, *, external_user: str,
                 principal_id: Optional[str], now: Optional[datetime] = None) -> dict:
    """Compare-and-swap pending -> used. Exactly one press wins; every other refuses."""
    now = now or _now()
    row = connection.execute(sql(
        "UPDATE chat_actions SET status = 'used', used_at = :now, used_by_external = :u, "
        " used_by_principal = :pr WHERE id = :i AND status = 'pending' AND expires_at > :now "
        "RETURNING *"),
        {"now": now, "u": external_user, "pr": principal_id, "i": action_id}).mappings().first()
    if row is None:
        raise ChatRefused("already_used", "That button was already used. Each works once.", 409)
    return dict(row)


def _retire_other_actions(connection, decision_id: str, keep: str) -> None:
    connection.execute(sql(
        "UPDATE chat_actions SET status = 'expired' WHERE decision_id = :d AND id <> :k AND "
        "status = 'pending'"), {"d": decision_id, "k": keep})


# --------------------------------------------------------------------------------------------- #
# Identity links
# --------------------------------------------------------------------------------------------- #
def _code_hash(code: str) -> str:
    return hashlib.sha256(("boss.chat.link:" + code.strip().upper()).encode()).hexdigest()


def find_link(connection, endpoint_id: str, external_user_id: str) -> Optional[dict]:
    row = connection.execute(sql(
        "SELECT * FROM chat_identity_links WHERE endpoint_id = :e AND external_user_id = :u "
        "AND revoked_at IS NULL AND verified_at IS NOT NULL"),
        {"e": endpoint_id, "u": external_user_id}).mappings().first()
    return dict(row) if row else None


def start_link(connection, endpoint: Mapping[str, Any], external_user_id: str,
               external_name: str = "", now: Optional[datetime] = None) -> str:
    """A fresh one-time code for this chat user (the previous one stops working)."""
    now = now or _now()
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(_secrets.choice(alphabet) for _ in range(10))
    code = f"{raw[:5]}-{raw[5:]}"
    existing = connection.execute(sql(
        "SELECT id, verified_at FROM chat_identity_links WHERE endpoint_id = :e AND "
        "external_user_id = :u AND revoked_at IS NULL"),
        {"e": endpoint["id"], "u": external_user_id}).mappings().first()
    params = {"h": _code_hash(code), "x": now + LINK_CODE_TTL, "n": external_name[:200]}
    if existing:
        connection.execute(sql(
            "UPDATE chat_identity_links SET link_code_hash = :h, code_expires_at = :x, "
            " external_name = COALESCE(NULLIF(:n, ''), external_name), updated_at = now() "
            "WHERE id = :i"), {**params, "i": existing["id"]})
    else:
        connection.execute(sql(
            "INSERT INTO chat_identity_links (id, endpoint_id, external_user_id, external_name, "
            " link_code_hash, code_expires_at) VALUES (:i, :e, :u, :n, :h, :x)"),
            {**params, "i": make_id("chl"), "e": endpoint["id"], "u": external_user_id})
    emit_event(connection, endpoint["project_id"], "chat.link_started", ACTOR,
               "chat_endpoint", endpoint["id"],
               {"kind": endpoint["kind"], "external_user_id": external_user_id})
    return code


def link_url(settings: Any, code: str) -> str:
    base = _base_url(settings)
    return f"{base}/chat/link?code={code}" if base else ""


def person_actor_for(principal: Any) -> str:
    """The actor string decisions record for a person (identity_guard.actor_for's rule)."""
    ids = getattr(principal, "legacy_member_ids", None) or ()
    return str(ids[0]) if ids else str(principal.id)


def peek_link(connection, code: str, now: Optional[datetime] = None) -> dict:
    """What a code would link (the console's confirm screen), without spending it."""
    now = now or _now()
    row = connection.execute(sql(
        "SELECT l.*, e.kind, e.name AS endpoint_name, e.project_id FROM chat_identity_links l "
        "JOIN chat_endpoints e ON e.id = l.endpoint_id WHERE l.link_code_hash = :h AND "
        "l.revoked_at IS NULL"), {"h": _code_hash(code or "")}).mappings().first()
    if row is None or row["code_expires_at"] is None or row["code_expires_at"] <= now:
        raise ChatRefused("bad_code", "That link code is not valid or has expired. Press a "
                          "button in chat again for a new one.", 404)
    return {"endpoint_id": row["endpoint_id"], "kind": row["kind"],
            "endpoint_name": row["endpoint_name"], "project_id": row["project_id"],
            "external_user_id": row["external_user_id"],
            "external_name": row["external_name"]}


def confirm_link(connection, code: str, principal: Any, now: Optional[datetime] = None) -> dict:
    """The signed-in person confirms: this chat user is me. Spends the code."""
    now = now or _now()
    seen = peek_link(connection, code, now)
    actor = person_actor_for(principal)
    row = connection.execute(sql(
        "UPDATE chat_identity_links SET principal_id = :p, person_actor = :a, verified_at = :now, "
        " link_code_hash = NULL, code_expires_at = NULL, updated_at = now() "
        "WHERE link_code_hash = :h AND revoked_at IS NULL AND code_expires_at > :now RETURNING *"),
        {"p": principal.id, "a": actor, "now": now, "h": _code_hash(code)}).mappings().first()
    if row is None:
        raise ChatRefused("bad_code", "That link code was just used.", 409)
    emit_event(connection, seen["project_id"], "chat.link_confirmed", actor, "chat_endpoint",
               seen["endpoint_id"], {"kind": seen["kind"], "principal_id": principal.id,
                                     "external_user_id": seen["external_user_id"]})
    return {**seen, "principal_id": principal.id, "person_actor": actor,
            "verified_at": now.isoformat()}


def links_of(connection, principal_id: str) -> list[dict]:
    rows = connection.execute(sql(
        "SELECT l.id, l.endpoint_id, l.external_user_id, l.external_name, l.verified_at, "
        " e.kind, e.name AS endpoint_name, e.project_id FROM chat_identity_links l JOIN "
        "chat_endpoints e ON e.id = l.endpoint_id WHERE l.principal_id = :p AND "
        "l.revoked_at IS NULL AND l.verified_at IS NOT NULL ORDER BY l.verified_at DESC"),
        {"p": principal_id}).mappings().all()
    return [{**dict(r), "verified_at": r["verified_at"].isoformat()} for r in rows]


def revoke_link(connection, principal_id: str, link_id: str) -> bool:
    row = connection.execute(sql(
        "UPDATE chat_identity_links l SET revoked_at = now(), updated_at = now() "
        "FROM chat_endpoints e WHERE l.id = :i AND l.principal_id = :p AND l.revoked_at IS NULL "
        "AND e.id = l.endpoint_id RETURNING l.endpoint_id, e.project_id"),
        {"i": link_id, "p": principal_id}).mappings().first()
    if row:
        emit_event(connection, row["project_id"], "chat.link_revoked", principal_id,
                   "chat_endpoint", row["endpoint_id"], {"link_id": link_id})
    return row is not None


# --------------------------------------------------------------------------------------------- #
# Threads
# --------------------------------------------------------------------------------------------- #
def bind_thread(connection, endpoint: Mapping[str, Any], *, channel: str, thread: str,
                decision_id: Optional[str] = None, run_id: Optional[str] = None,
                work_item_id: Optional[str] = None) -> None:
    if not channel or not thread:
        return
    connection.execute(sql(
        "INSERT INTO chat_threads (id, endpoint_id, project_id, external_channel_id, "
        " external_thread_id, decision_id, run_id, work_item_id) VALUES (:i, :e, :p, :c, :t, :d, "
        " :r, :w) ON CONFLICT (endpoint_id, external_channel_id, external_thread_id) DO UPDATE "
        "SET decision_id = COALESCE(EXCLUDED.decision_id, chat_threads.decision_id), "
        " run_id = COALESCE(EXCLUDED.run_id, chat_threads.run_id), "
        " work_item_id = COALESCE(EXCLUDED.work_item_id, chat_threads.work_item_id)"),
        {"i": make_id("cht"), "e": endpoint["id"], "p": endpoint["project_id"], "c": channel,
         "t": thread, "d": decision_id, "r": run_id, "w": work_item_id})


def threads_of_decision(connection, decision_id: str) -> list[dict]:
    return [dict(r) for r in connection.execute(sql(
        "SELECT t.*, e.kind FROM chat_threads t JOIN chat_endpoints e ON e.id = t.endpoint_id "
        "WHERE t.decision_id = :d"), {"d": decision_id}).mappings().all()]


# --------------------------------------------------------------------------------------------- #
# Decision cards
# --------------------------------------------------------------------------------------------- #
def _decision(connection, decision_id: str) -> Optional[dict]:
    row = connection.execute(sql(
        "SELECT id, project_id, question, status, risk_tier, recommendation, options, "
        " default_action, external_ref, task_id FROM decisions WHERE id = :d"),
        {"d": decision_id}).mappings().first()
    return dict(row) if row else None


def _open(decision: Mapping[str, Any]) -> bool:
    """Still waiting on a person — the Approvals inbox's own definition (decision_actions)."""
    from .decision_actions import OPEN_STATUSES

    return str(decision.get("status") or "") in OPEN_STATUSES


def _max_tier(endpoint: Mapping[str, Any]) -> int:
    try:
        return min(int(_json(endpoint.get("config"), {}).get("max_tier", DEFAULT_MAX_TIER)),
                   DEFAULT_MAX_TIER)
    except (TypeError, ValueError):
        return DEFAULT_MAX_TIER


def console_url(settings: Any, decision: Mapping[str, Any]) -> str:
    base = _base_url(settings)
    if not base:
        return ""
    from ..app_links.console import build_console_link

    return build_console_link("decisions", project_id=decision["project_id"], base=base,
                              decision_id=decision["id"])


def _offered(decision: Mapping[str, Any],
             endpoint: Optional[Mapping[str, Any]] = None) -> list[dict[str, Any]]:
    """The buttons chat offers: approve / reject, as the console offers them, each with
    `needs_note`. An APPROVE THAT NEEDS A NOTE is an override of a BOSS safety check (ship with
    unreviewed commits, merge with checks failing; the console sends it forced): never from chat.
    `config.reject_needs_note` makes Reject ask for a reason (the comment form)."""
    from . import decision_actions

    try:
        offered = decision_actions.allowed_actions(dict(decision))
    except Exception:  # noqa: BLE001 — an unreadable card offers only the console
        return []
    cfg = _json((endpoint or {}).get("config"), {})
    out = []
    for a in offered:
        if a.get("action") not in CHAT_ACTIONS or a.get("needs") not in (None, "note"):
            continue
        if a["action"] == "approve" and a.get("needs") == "note":
            continue
        needs_note = a.get("needs") == "note" or (
            a["action"] == "reject" and bool(cfg.get("reject_needs_note")))
        out.append({**a, "needs_note": needs_note})
    return out


def _has_override(decision: Mapping[str, Any]) -> bool:
    from . import decision_actions

    try:
        return any(a.get("action") == "approve" and a.get("needs") == "note"
                   for a in decision_actions.allowed_actions(dict(decision)))
    except Exception:  # noqa: BLE001
        return False


def _console_only(endpoint: Mapping[str, Any], decision: Mapping[str, Any],
                  action: Optional[str] = None) -> Optional[str]:
    """Why this decision (or this action on it) is answered in the console, never from chat."""
    tier = int(decision.get("risk_tier") or 0)
    if tier > _max_tier(endpoint):
        return (f"A tier-{tier} decision is answered in BOSS: it needs a fresh sign-in"
                + (" and two people." if tier >= 3 else "."))
    if action == "approve" and _has_override(decision):
        return ("Approving this overrides a BOSS safety check, so it is answered in BOSS, "
                "with your reason.")
    return None


def build_card(connection, settings: Any, endpoint: Mapping[str, Any],
               decision: Mapping[str, Any], now: Optional[datetime] = None) -> DecisionCard:
    rec = _json(decision.get("recommendation"), {})
    summary = str(rec.get("summary") or rec.get("reason") or rec.get("action") or "")[:500]
    console_only = _console_only(endpoint, decision)
    buttons: list[dict[str, str]] = []
    if not console_only:
        for a in _offered(decision, endpoint):
            style = "primary" if a["action"] == "approve" else "danger"
            buttons.append({"action": a["action"], "label": a.get("label") or a["action"],
                            "style": style,
                            "token": mint_action(connection, endpoint, decision, a["action"],
                                                 now)})
    return DecisionCard(decision_id=decision["id"], project_id=decision["project_id"],
                        question=str(decision.get("question") or ""),
                        risk_tier=int(decision.get("risk_tier") or 0), summary=summary,
                        console_url=console_url(settings, decision), buttons=buttons,
                        console_only_reason=console_only)


def on_decision_created(connection, decision_id: str) -> int:
    """`decision.created` -> one card per active endpoint of the project, in the outbox."""
    from .notify import enqueue_chat

    decision = _decision(connection, decision_id)
    if decision is None or not _open(decision):
        return 0
    endpoints = connection.execute(sql(
        "SELECT id, project_id, kind, config FROM chat_endpoints WHERE project_id = :p AND "
        "status = 'active' AND kind <> 'discord'"), {"p": decision["project_id"]}).mappings().all()
    queued = 0
    for ep in endpoints:
        channel = str(_json(ep["config"], {}).get("channel") or "")
        if not channel:
            continue
        if enqueue_chat(connection, endpoint_id=ep["id"], project_id=ep["project_id"],
                        kind="decision_card",
                        payload={"op": "post", "decision_id": decision_id, "channel": channel},
                        dedupe_key=f"decision_card:{ep['id']}:{decision_id}"):
            queued += 1
    return queued


# --------------------------------------------------------------------------------------------- #
# Outbox delivery (called by notify.deliver_chat_due for each claimed row)
# --------------------------------------------------------------------------------------------- #
def deliver(connection, settings: Any, row: Mapping[str, Any]) -> dict[str, Any]:
    endpoint = get_endpoint(connection, row["endpoint_id"])
    if endpoint is None or endpoint["status"] != "active":
        from .notify import NotConfigured

        raise NotConfigured("The chat endpoint was removed or disabled")
    adapter = adapter_for(endpoint["kind"])
    payload = dict(_json(row.get("payload"), {}))
    if row["kind"] == "decision_card":
        decision = _decision(connection, payload["decision_id"])
        if decision is None or not _open(decision):
            return {"skipped": "the decision was answered before its card was sent"}
        card = build_card(connection, settings, endpoint, decision)
        payload["message"] = adapter.render_decision(card)
    token = endpoint_secret(connection, settings, endpoint, "bot_token") \
        if "bot_token" in adapter.secret_names else ""
    receipt = adapter.send(token, payload)
    if row["kind"] == "decision_card":
        bind_thread(connection, endpoint, channel=str(receipt.get("channel") or ""),
                    thread=str(receipt.get("thread") or ""), decision_id=payload["decision_id"])
    return receipt


def _queue(connection, endpoint: Mapping[str, Any], kind: str, payload: dict,
           dedupe: str) -> Optional[str]:
    from .notify import enqueue_chat

    return enqueue_chat(connection, endpoint_id=endpoint["id"],
                        project_id=endpoint["project_id"], kind=kind, payload=payload,
                        dedupe_key=dedupe)


# --------------------------------------------------------------------------------------------- #
# Deciding as the linked person
# --------------------------------------------------------------------------------------------- #
def _org_of(connection, project_id: str) -> Optional[str]:
    return connection.execute(sql(
        "SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
        "WHERE p.id = :p"), {"p": project_id}).scalar()


def decide_as_person(connection, settings: Any, link: Mapping[str, Any],
                     decision: Mapping[str, Any], action: str, note: Optional[str]) -> dict:
    """DecisionService.transition AS THE LINKED PERSON.

    Under identity (the decision's org is claimed) the person's live principal must be active and
    HOLD decision:approve at the project. The transition then runs with that person as the
    deciding context and as the approval's named human (approvals.MCP_REVIEWER — the same seam an
    MCP confirmation uses for a human verified out of band), so the seat and two-person rules
    apply to them exactly as in the console. Before the claim, today's member rules decide for
    their legacy actor. Raises ChatRefused in words; ApprovalPending passes through.
    """
    from ..identity import approvals, claim, lineage
    from ..identity.principal import AuthContext, AuthMethod
    from ..identity.scope import ResolvedScope
    from .decisions import DecisionService

    principal = None
    if link.get("principal_id"):
        from ..identity.users import load_principal

        principal = load_principal(connection, link["principal_id"])
        if principal is None or not principal.active or not principal.is_person:
            raise ChatRefused("link_inactive", "Your BOSS account is not active. Link again "
                              "after an admin restores it.", 403)
    actor = person_actor_for(principal) if principal is not None else str(
        link.get("person_actor") or "")
    if not actor:
        raise ChatRefused("not_linked", "Link your BOSS account first.", 403)
    org = _org_of(connection, decision["project_id"])
    identity_decides = False
    if principal is not None and org:
        try:
            identity_decides = claim.is_claimed(connection, org)
        except Exception:  # noqa: BLE001 — an install without the identity tables: member rules
            identity_decides = False
    if identity_decides:
        if principal.org_id != org:
            raise ChatRefused("not_permitted", "This decision is in another organisation.", 403)
        from ..identity import authz

        held = authz.effective_permissions(
            principal, ResolvedScope.in_project(org, decision["project_id"]),
            db=connection).permissions
        if "decision:approve" not in held:
            raise ChatRefused("not_permitted", "You may not answer decisions in this project.",
                              403)
    svc = DecisionService(settings)

    def run() -> dict:
        if identity_decides:
            ctx = AuthContext(principal, AuthMethod.SESSION)
            lineage.enter(ctx)
            lineage.mark_enforced(True)
            approvals.MCP_REVIEWER.set(principal.id)
        return svc.transition(connection, decision["id"], action=action, actor=actor, note=note)

    # A copied context: the person's identity never leaks into the rest of this request.
    return contextvars.copy_context().run(run)


def _answer_words(action: str, who: str) -> str:
    return {"approve": f"Approved by {who} from chat.",
            "reject": f"Rejected by {who} from chat."}.get(action, f"{action} by {who} from chat.")


# --------------------------------------------------------------------------------------------- #
# Inbound
# --------------------------------------------------------------------------------------------- #
def handle_inbound(connection, settings: Any, endpoint: Mapping[str, Any], inbound: ChatInbound,
                   now: Optional[datetime] = None) -> dict[str, Any]:
    """A VERIFIED request, already parsed. Returns what to answer the channel with:
    {"status": ..., "words": ..., ...}. Writes happen in `connection` (the caller commits)."""
    if inbound.kind == "handshake":
        return {"status": "handshake", "challenge": inbound.challenge}
    if inbound.kind == "link_request":
        return _link_reply(connection, settings, endpoint, inbound, now)
    if inbound.kind in ("press", "note_submitted"):
        return _press(connection, settings, endpoint, inbound, now)
    return {"status": "ignored"}


def _link_reply(connection, settings, endpoint, inbound: ChatInbound, now) -> dict[str, Any]:
    if not inbound.external_user_id:
        return {"status": "ignored"}
    existing = find_link(connection, endpoint["id"], inbound.external_user_id)
    if existing:
        words = "You are already linked to BOSS. Decisions you answer here are recorded as you."
    else:
        code = start_link(connection, endpoint, inbound.external_user_id,
                          inbound.external_name, now)
        words = _link_words(settings, code)
    _queue(connection, endpoint, "message",
           {"op": "dm", "user": inbound.external_user_id, "text": words},
           f"link_dm:{endpoint['id']}:{inbound.external_user_id}:{make_id('m')}")
    return {"status": "link_sent", "words": words}


def _link_words(settings: Any, code: str) -> str:
    url = link_url(settings, code)
    where = (f"Open {url} , sign in to BOSS, and confirm." if url else
             f"Sign in to BOSS and enter link code {code} under Settings > Chat links.")
    return ("Link your chat account to BOSS before answering decisions here. " + where
            + " The code works once, for 30 minutes.")


def _private(connection, endpoint, inbound: ChatInbound, words: str) -> None:
    """The presser's private answer, through the outbox."""
    item = {"op": "ephemeral", "channel": inbound.channel_id, "user": inbound.external_user_id,
            "text": words}
    if endpoint["kind"] == "telegram":
        item["trigger"] = inbound.trigger
    _queue(connection, endpoint, "response", item,
           f"reply:{endpoint['id']}:{inbound.external_user_id}:{make_id('m')}")


def _press(connection, settings, endpoint, inbound: ChatInbound, now) -> dict[str, Any]:
    now = now or _now()
    try:
        token_row = read_action(connection, endpoint["id"], inbound.token, now)
        if inbound.action and inbound.action != token_row["action"]:
            raise ChatRefused("bad_token", "That button is not one BOSS sent.", 403)
    except ChatRefused as refused:
        _audit_refusal(connection, endpoint, inbound, refused)
        _private(connection, endpoint, inbound, refused.message)
        return {"status": "refused", "code": refused.code, "words": refused.message}
    decision = _decision(connection, token_row["decision_id"])
    if decision is None:
        words = "That decision no longer exists."
        _private(connection, endpoint, inbound, words)
        return {"status": "refused", "code": "gone", "words": words}

    link = find_link(connection, endpoint["id"], inbound.external_user_id)
    if link is None:
        code = start_link(connection, endpoint, inbound.external_user_id, inbound.external_name,
                          now)
        words = _link_words(settings, code)
        _queue(connection, endpoint, "message",
               {"op": "dm", "user": inbound.external_user_id, "text": words},
               f"link_dm:{endpoint['id']}:{inbound.external_user_id}:{make_id('m')}")
        _private(connection, endpoint, inbound,
                 "You are not linked to BOSS yet. I sent you a private link.")
        return {"status": "link_required", "words": words}

    console_only = _console_only(endpoint, decision, token_row["action"])
    if console_only:
        url = console_url(settings, decision)
        words = console_only + (f" Open it: {url}" if url else "")
        _private(connection, endpoint, inbound, words)
        return {"status": "console_required", "words": words, "url": url}

    offered = {a["action"]: a for a in _offered(decision, endpoint)}
    wanted = offered.get(token_row["action"])
    note = inbound.note
    if wanted is not None and wanted["needs_note"] and not note:
        adapter = adapter_for(endpoint["kind"])
        if inbound.kind == "press" and adapter.supports_note_form and inbound.trigger:
            return {"status": "note_required", "token": inbound.token,
                    "action": token_row["action"], "label": wanted.get("label") or "Decide",
                    "trigger": inbound.trigger}
        url = console_url(settings, decision)
        words = "This decision needs a note. Answer it in BOSS" + (f": {url}" if url else ".")
        _private(connection, endpoint, inbound, words)
        return {"status": "console_required", "words": words, "url": url}

    spent = spend_action(connection, token_row["id"], external_user=inbound.external_user_id,
                         principal_id=link.get("principal_id"), now=now)
    return _decide(connection, settings, endpoint, inbound, link, decision, spent, note)


def _audit_refusal(connection, endpoint, inbound: ChatInbound, refused: ChatRefused) -> None:
    emit_event(connection, endpoint["project_id"], "chat.press_refused", ACTOR, "chat_endpoint",
               endpoint["id"], {"code": refused.code, "external_user_id": inbound.external_user_id,
                                "kind": endpoint["kind"]})


def _decide(connection, settings, endpoint, inbound: ChatInbound, link, decision, spent,
            note) -> dict[str, Any]:
    from ..identity.approvals import ApprovalPending, ApprovalRefused
    from .decisions import DecisionNotFoundError, InvalidDecisionTransitionError
    from .members import ProjectMemberNotFoundError, ProjectMemberPermissionError

    action = spent["action"]
    outcome: dict[str, Any]
    # A savepoint: a refused transition rolls back only itself (the spent token and the record
    # of the press stay). A first-of-two approval is KEPT: it raises after recording it.
    nested = connection.begin_nested()
    try:
        row = decide_as_person(connection, settings, link, decision, action,
                               note or f"Answered from {endpoint['kind']} chat")
        nested.commit()
        outcome = {"status": "done", "decision_status": row.get("status")}
    except ApprovalPending as pending:
        nested.commit()
        outcome = {"status": "pending_second", **pending.body()}
    except (ApprovalRefused, ChatRefused) as refused:
        nested.rollback()
        outcome = {"status": "refused", "code": refused.code, "words": refused.message}
    except (InvalidDecisionTransitionError, DecisionNotFoundError) as error:
        nested.rollback()
        outcome = {"status": "refused", "code": "transition_refused", "words": str(error)}
    except (ProjectMemberNotFoundError, ProjectMemberPermissionError) as error:
        nested.rollback()
        outcome = {"status": "refused", "code": "not_permitted", "words": str(error)}
    except BaseException:
        if nested.is_active:
            nested.rollback()
        raise
    who = link.get("person_actor") or link.get("principal_id") or "?"
    connection.execute(sql("UPDATE chat_actions SET outcome = CAST(:o AS JSONB) WHERE id = :i"),
                       {"o": json.dumps(outcome, default=str), "i": spent["id"]})
    emit_event(connection, decision["project_id"], "chat.decision_answered", who, "decision",
               decision["id"],
               {"via": endpoint["kind"], "endpoint_id": endpoint["id"], "action": action,
                "action_token": spent["id"], "external_user_id": inbound.external_user_id,
                "principal_id": link.get("principal_id"), "outcome": outcome["status"],
                "code": outcome.get("code")})
    if outcome["status"] == "done":
        _retire_other_actions(connection, decision["id"], spent["id"])
        words = _answer_words(action, who)
        adapter = adapter_for(endpoint["kind"])
        for t in threads_of_decision(connection, decision["id"]):
            if t["endpoint_id"] != endpoint["id"]:
                continue
            _queue(connection, endpoint, "response",
                   {"op": "update", "channel": t["external_channel_id"],
                    "thread": t["external_thread_id"],
                    "message": adapter.render_answered(str(decision.get("question") or ""),
                                                       words)},
                   f"update:{spent['id']}:{t['id']}")
        if endpoint["kind"] == "telegram":
            _private(connection, endpoint, inbound, words)
        return {**outcome, "words": words}
    if outcome["status"] == "pending_second":
        words = "Your approval is recorded. A tier-3 decision needs a second person."
        _private(connection, endpoint, inbound, words)
        return {**outcome, "words": words}
    _private(connection, endpoint, inbound, outcome["words"])
    return outcome


def open_note_form(connection, settings: Any, endpoint: Mapping[str, Any],
                   result: Mapping[str, Any], inbound: ChatInbound) -> None:
    """Open the comment-required form (Slack: views.open within 3 s of the press)."""
    adapter = adapter_for(endpoint["kind"])
    token = endpoint_secret(connection, settings, endpoint, "bot_token")
    adapter.open_note_form(token, result["trigger"], token=result["token"],
                           action=result["action"], label=str(result.get("label") or "Decide"),
                           channel=inbound.channel_id, thread=inbound.thread_id)


__all__ = ["ACTION_TTL", "ChatRefused", "ChatVerifyRefused", "ChatSendFailed", "KINDS",
           "bind_thread", "build_card", "confirm_link", "create_endpoint", "decide_as_person",
           "deliver", "endpoint_urls", "find_link", "get_endpoint", "handle_inbound",
           "link_url", "links_of", "list_endpoints", "mint_action", "on_decision_created",
           "open_note_form", "peek_link", "read_action", "revoke_link", "set_endpoint_status",
           "spend_action", "start_link", "verify_request"]
