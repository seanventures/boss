"""Two-way chat adapters: Slack (first), Telegram (second), Discord (a stub) — BOSSAPP-834.

ONE INTERFACE, THREE CHANNELS
-----------------------------
Every adapter answers the same five questions, so services/chat_channels.py never branches on
the channel for anything but wording:

  * ``verify(secret, headers, raw_body, now)`` — is this request really from the channel? Slack
    signs ``v0:<ts>:<raw body>`` with the app's signing secret (HMAC-SHA256, 5-minute window);
    Telegram echoes the webhook secret we registered in ``X-Telegram-Bot-Api-Secret-Token``.
    A refusal raises ``ChatVerifyRefused`` with a reason word; nothing else is read first.
  * ``parse(headers, raw_body)`` — the request as a ``ChatInbound``: a button press (the signed
    action token it carried), a modal submission (the token and the note), a direct "link me"
    message, Slack's URL handshake, or ``ignored``.
  * ``render_decision(card)`` — a decision as the channel's own message: Slack blocks with
    Approve / Reject buttons, a Telegram inline keyboard. Every button carries ONE signed,
    single-use token; nothing else about the decision is trusted from the press.
  * ``send(bot_token, item)`` — deliver one outbox item (post, update, ephemeral, DM, answer)
    and return a receipt; raise with the reason in words. Called by notify.deliver_chat_due.
  * ``open_note_form(bot_token, trigger, token, channel)`` — the comment-required form (a Slack
    modal). Telegram has none: a decision that needs a note is answered in the console.

Secrets (the signing secret, the bot token) are passed IN by the caller, resolved from the secret
store at the moment of use; this module never stores, logs or returns one. HTTP goes through
``TRANSPORT`` (identity.http.fetch — https only, no private hosts) so tests replace one function.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional
from urllib.parse import parse_qs, quote, urlsplit

#: Slack refuses a request older than this (its own guidance: 5 minutes); so do we.
SLACK_TOLERANCE_SECONDS = 300
SLACK_API = "https://slack.com/api"
TELEGRAM_API = "https://api.telegram.org"

#: Slack action ids. A press is recognised by these, never by the button's label.
SLACK_ACTION_PREFIX = "boss_decide_"
SLACK_NOTE_CALLBACK = "boss_decide_note"
SLACK_NOTE_BLOCK = "note"
SLACK_NOTE_INPUT = "note_input"
#: The metadata event type on every message BOSS posts to Slack (loop prevention, layer 2).
SLACK_MARKER = "boss_run"


class ChatVerifyRefused(Exception):
    """The request did not prove it came from the channel. `reason` is one word for the log."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ChatSendFailed(RuntimeError):
    """The channel refused or could not be reached. The message never includes a secret."""


class ChatRateLimited(ChatSendFailed):
    """The channel said slow down (HTTP 429 / `ratelimited`). Not a failure: the outbox waits
    `retry_after` seconds (the channel's Retry-After) and tries again without spending an
    attempt (boss-eventing-lld.md §7.2)."""

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class ChatWritesBlocked(ChatSendFailed):
    """The channel permanently blocked writes to this conversation or user (Teams 403
    ``MessageWritesBlocked``). Stop retrying immediately and mark the target blocked."""

    def __init__(self, message: str, reason: str = "MessageWritesBlocked"):
        super().__init__(message)
        self.reason = reason


def _default_transport(method: str, url: str, headers: Mapping[str, str],
                       body: Optional[bytes], purpose: str) -> tuple[int, Any]:
    from ..identity.http import fetch

    resp = fetch(method, url, headers=dict(headers), data=body, purpose=purpose)
    try:
        data = resp.json()
    except ValueError:
        data = None
    if resp.status == 429:
        # The one header a sender needs is carried in the answer (the transport's shape is
        # (status, data), which every test fake implements).
        retry = {str(k).lower(): v for k, v in (resp.headers or {}).items()}.get("retry-after")
        data = {**(data if isinstance(data, dict) else {}), "ok": False,
                "error": "ratelimited", "retry_after": retry}
    return resp.status, data


#: (method, url, headers, body, purpose) -> (status, parsed JSON or None). Tests replace it.
TRANSPORT: Callable[..., tuple[int, Any]] = _default_transport


@dataclass
class ChatInbound:
    """What a verified request asks for, channel-neutral."""
    kind: str                      # press | note_submitted | link_request | handshake | ignored
    external_user_id: str = ""
    external_name: str = ""
    token: str = ""                # the signed action token a button carried
    action: str = ""               # what the button said it does (checked against the token)
    note: Optional[str] = None
    channel_id: str = ""
    thread_id: str = ""            # Slack message ts / Telegram message id
    trigger: str = ""              # Slack trigger_id (opens a modal) / Telegram callback id
    challenge: str = ""            # Slack url_verification
    raw: dict = field(default_factory=dict)


@dataclass
class DecisionCard:
    """What a decision message shows. Built by chat_channels from the decision row."""
    decision_id: str
    project_id: str
    question: str
    risk_tier: int
    summary: str
    console_url: str
    buttons: list[dict[str, str]]  # [{"action", "label", "token", "style"}]
    console_only_reason: Optional[str] = None


# --------------------------------------------------------------------------------------------- #
# Slack
# --------------------------------------------------------------------------------------------- #
def slack_signature(signing_secret: str, timestamp: str, raw_body: bytes) -> str:
    base = b"v0:" + timestamp.encode() + b":" + raw_body
    return "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()


def _lower(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in (headers or {}).items()}


class SlackChat:
    kind = "slack"
    secret_names = ("signing_secret", "bot_token")
    verify_secret = "signing_secret"
    supports_note_form = True

    def verify(self, secret: str, headers: Mapping[str, str], raw_body: bytes,
               now: Optional[float] = None) -> None:
        h = _lower(headers)
        ts = h.get("x-slack-request-timestamp", "")
        sig = h.get("x-slack-signature", "")
        if not ts or not sig:
            raise ChatVerifyRefused("missing_signature")
        try:
            ts_int = int(ts)
        except ValueError:
            raise ChatVerifyRefused("bad_timestamp") from None
        if abs((now if now is not None else time.time()) - ts_int) > SLACK_TOLERANCE_SECONDS:
            raise ChatVerifyRefused("stale_timestamp")
        if not secret:
            raise ChatVerifyRefused("not_configured")
        if not hmac.compare_digest(slack_signature(secret, ts, raw_body), sig):
            raise ChatVerifyRefused("bad_signature")

    def parse(self, headers: Mapping[str, str], raw_body: bytes) -> ChatInbound:
        h = _lower(headers)
        if h.get("content-type", "").startswith("application/x-www-form-urlencoded"):
            form = parse_qs(raw_body.decode("utf-8", "replace"))
            payload = json.loads((form.get("payload") or ["{}"])[0])
            return self._interaction(payload)
        body = json.loads(raw_body or b"{}")
        if body.get("type") == "url_verification":
            return ChatInbound(kind="handshake", challenge=str(body.get("challenge") or ""))
        if body.get("type") == "event_callback":
            ev = body.get("event") or {}
            if ev.get("bot_id") or ev.get("subtype"):
                return ChatInbound(kind="ignored", raw=body)
            text = str(ev.get("text") or "").strip().lower()
            if ev.get("type") == "message" and ev.get("channel_type") == "im" and (
                    "link" in text.split()):
                return ChatInbound(kind="link_request", external_user_id=str(ev.get("user") or ""),
                                   channel_id=str(ev.get("channel") or ""), raw=body)
            if ev.get("type") == "app_mention":
                return ChatInbound(kind="mention", external_user_id=str(ev.get("user") or ""),
                                   channel_id=str(ev.get("channel") or ""),
                                   thread_id=str(ev.get("thread_ts") or ev.get("ts") or ""),
                                   raw=body)
        return ChatInbound(kind="ignored", raw=body)

    def _interaction(self, payload: dict) -> ChatInbound:
        user = payload.get("user") or {}
        uid = str(user.get("id") or "")
        name = str(user.get("username") or user.get("name") or "")
        if payload.get("type") == "block_actions":
            for act in payload.get("actions") or []:
                action_id = str(act.get("action_id") or "")
                if action_id.startswith(SLACK_ACTION_PREFIX):
                    container = payload.get("container") or {}
                    channel = (payload.get("channel") or {}).get("id") or container.get(
                        "channel_id") or ""
                    return ChatInbound(
                        kind="press", external_user_id=uid, external_name=name,
                        token=str(act.get("value") or ""),
                        action=action_id[len(SLACK_ACTION_PREFIX):],
                        channel_id=str(channel),
                        thread_id=str(container.get("message_ts")
                                      or (payload.get("message") or {}).get("ts") or ""),
                        trigger=str(payload.get("trigger_id") or ""), raw=payload)
            return ChatInbound(kind="ignored", raw=payload)
        if payload.get("type") == "view_submission":
            view = payload.get("view") or {}
            if view.get("callback_id") != SLACK_NOTE_CALLBACK:
                return ChatInbound(kind="ignored", raw=payload)
            try:
                meta = json.loads(view.get("private_metadata") or "{}")
            except ValueError:
                meta = {}
            values = ((view.get("state") or {}).get("values") or {})
            note = ((values.get(SLACK_NOTE_BLOCK) or {}).get(SLACK_NOTE_INPUT) or {}).get("value")
            return ChatInbound(kind="note_submitted", external_user_id=uid, external_name=name,
                               token=str(meta.get("token") or ""),
                               action=str(meta.get("action") or ""),
                               channel_id=str(meta.get("channel") or ""),
                               thread_id=str(meta.get("thread") or ""),
                               note=(note or "").strip() or None, raw=payload)
        return ChatInbound(kind="ignored", raw=payload)

    # -- rendering ------------------------------------------------------------------------------
    def render_decision(self, card: DecisionCard) -> dict[str, Any]:
        head = f"*BOSS needs a decision* · tier {card.risk_tier}\n{card.question}"
        blocks: list[dict[str, Any]] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": head[:2900]}}]
        if card.summary:
            blocks.append({"type": "context", "elements": [
                {"type": "mrkdwn", "text": card.summary[:2900]}]})
        elements: list[dict[str, Any]] = []
        for b in card.buttons:
            button = {"type": "button", "action_id": SLACK_ACTION_PREFIX + b["action"],
                      "text": {"type": "plain_text", "text": b["label"][:70]},
                      "value": b["token"]}
            if b.get("style") in ("primary", "danger"):
                button["style"] = b["style"]
            elements.append(button)
        if card.console_url:
            elements.append({"type": "button", "action_id": "boss_open_console",
                             "text": {"type": "plain_text", "text": "Open in BOSS"},
                             "url": card.console_url})
        if card.console_only_reason:
            blocks.append({"type": "context", "elements": [
                {"type": "mrkdwn", "text": card.console_only_reason[:2900]}]})
        if elements:
            blocks.append({"type": "actions", "block_id": "boss_decision", "elements": elements})
        return {"text": f"BOSS needs a decision: {card.question}"[:3000], "blocks": blocks}

    def render_answered(self, question: str, words: str) -> dict[str, Any]:
        return {"text": words[:3000], "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"{question}"[:2900]}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": words[:2900]}]}]}

    # -- delivery -------------------------------------------------------------------------------
    def _call(self, bot_token: str, method: str, body: dict) -> dict:
        status, data = TRANSPORT(
            "POST", f"{SLACK_API}/{method}",
            {"Content-Type": "application/json; charset=utf-8",
             "Authorization": f"Bearer {bot_token}"},
            json.dumps(body).encode(), "chat.slack")
        if status == 429 or (isinstance(data, dict) and data.get("error") == "ratelimited"):
            retry = (data or {}).get("retry_after") if isinstance(data, dict) else None
            try:
                seconds = float(retry) if retry not in (None, "") else None
            except (TypeError, ValueError):
                seconds = None
            raise ChatRateLimited(f"Slack rate-limited {method}", retry_after=seconds)
        if status >= 300 or not isinstance(data, dict):
            raise ChatSendFailed(f"Slack answered {status} to {method}")
        if not data.get("ok"):
            raise ChatSendFailed(f"Slack refused {method}: {data.get('error') or 'unknown'}")
        return data

    @staticmethod
    def _marker(item: Mapping[str, Any]) -> dict[str, Any]:
        """LOOP PREVENTION, LAYER 2 (boss-eventing-lld.md §7.1): every message BOSS posts says it
        is BOSS's — the run, the deployment, the hop count and the endpoint — so its echo in the
        Events API is recognised as ours and never wakes an agent."""
        meta = item.get("metadata")
        if isinstance(meta, dict) and meta.get("event_type"):
            return dict(meta)
        payload = {k: v for k, v in {"endpoint_id": item.get("endpoint_id"),
                                     "run_id": item.get("run_id"),
                                     "deployment_id": item.get("deployment_id"),
                                     "hops": item.get("hops")}.items() if v not in (None, "")}
        return {"event_type": SLACK_MARKER, "event_payload": payload}

    def send(self, bot_token: str, item: Mapping[str, Any]) -> dict[str, Any]:
        kind = item.get("op")
        if kind == "post":
            body = {"channel": item["channel"], **item["message"],
                    "metadata": self._marker(item)}
            if item.get("thread"):
                body["thread_ts"] = item["thread"]
            data = self._call(bot_token, "chat.postMessage", body)
            return {"channel": data.get("channel") or item["channel"], "thread": data.get("ts"),
                    "ts": data.get("ts"),
                    "bot_id": (data.get("message") or {}).get("bot_id")
                    if isinstance(data.get("message"), dict) else None}
        if kind == "update":
            data = self._call(bot_token, "chat.update",
                              {"channel": item["channel"], "ts": item["thread"],
                               **item["message"], "metadata": self._marker(item)})
            return {"channel": item["channel"], "thread": data.get("ts") or item["thread"],
                    "ts": data.get("ts") or item["thread"]}
        if kind == "ephemeral":
            body = {"channel": item["channel"], "user": item["user"], "text": item["text"]}
            if item.get("thread"):
                body["thread_ts"] = item["thread"]
            self._call(bot_token, "chat.postEphemeral", body)
            return {"channel": item["channel"]}
        if kind == "dm":
            opened = self._call(bot_token, "conversations.open", {"users": item["user"]})
            channel = ((opened.get("channel") or {}).get("id")) or ""
            if not channel:
                raise ChatSendFailed("Slack opened no direct message channel")
            data = self._call(bot_token, "chat.postMessage",
                              {"channel": channel, "text": item["text"],
                               "metadata": self._marker(item)})
            return {"channel": channel, "ts": data.get("ts")}
        if kind == "react":
            try:
                self._call(bot_token, "reactions.add",
                           {"channel": item["channel"], "timestamp": item["ts"],
                            "name": str(item["emoji"]).strip(":")})
            except ChatRateLimited:
                raise
            except ChatSendFailed as refused:
                if "already_reacted" not in str(refused):
                    raise
            return {"channel": item["channel"], "ts": item["ts"]}
        if kind == "set_status":
            # The assistant-thread status line (clears on the next reply or after 2 minutes).
            self._call(bot_token, "assistant.threads.setStatus",
                       {"channel_id": item["channel"], "thread_ts": item["thread"],
                        "status": str(item.get("text") or "")[:100]})
            return {"channel": item["channel"], "thread": item["thread"]}
        if kind == "stream_start":
            body = {"channel": item["channel"], "thread_ts": item["thread"]}
            for key in ("recipient_user_id", "recipient_team_id"):
                if item.get(key):
                    body[key] = item[key]
            if item.get("text"):
                body["markdown_text"] = str(item["text"])[:12000]
            data = self._call(bot_token, "chat.startStream", body)
            return {"channel": item["channel"], "ts": data.get("ts"), "thread": item["thread"]}
        if kind in ("stream_append", "stream_stop"):
            body = {"channel": item["channel"], "ts": item["ts"]}
            if item.get("text"):
                body["markdown_text"] = str(item["text"])[:12000]
            if kind == "stream_stop":
                body["metadata"] = self._marker(item)
            data = self._call(bot_token, "chat.appendStream" if kind == "stream_append"
                              else "chat.stopStream", body)
            return {"channel": item["channel"], "ts": data.get("ts") or item["ts"]}
        raise ChatSendFailed(f"unknown Slack outbox op {kind!r}")

    def open_note_form(self, bot_token: str, trigger: str, *, token: str, action: str,
                       label: str, channel: str, thread: str) -> None:
        """The comment-required modal. Must be opened within 3 s of the press (trigger_id)."""
        view = {
            "type": "modal", "callback_id": SLACK_NOTE_CALLBACK,
            "private_metadata": json.dumps({"token": token, "action": action,
                                            "channel": channel, "thread": thread}),
            "title": {"type": "plain_text", "text": label[:24] or "Decide"},
            "submit": {"type": "plain_text", "text": label[:24] or "Submit"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [{"type": "input", "block_id": SLACK_NOTE_BLOCK,
                        "label": {"type": "plain_text", "text": "Why? (this is the record)"},
                        "element": {"type": "plain_text_input", "action_id": SLACK_NOTE_INPUT,
                                    "multiline": True, "min_length": 3}}]}
        self._call(bot_token, "views.open", {"trigger_id": trigger, "view": view})


# --------------------------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------------------------- #
class TelegramChat:
    kind = "telegram"
    secret_names = ("bot_token", "webhook_secret")
    verify_secret = "webhook_secret"
    supports_note_form = False

    def verify(self, secret: str, headers: Mapping[str, str], raw_body: bytes,
               now: Optional[float] = None) -> None:
        got = _lower(headers).get("x-telegram-bot-api-secret-token", "")
        if not got:
            raise ChatVerifyRefused("missing_signature")
        if not secret:
            raise ChatVerifyRefused("not_configured")
        if not hmac.compare_digest(got.encode(), secret.encode()):
            raise ChatVerifyRefused("bad_signature")

    def parse(self, headers: Mapping[str, str], raw_body: bytes) -> ChatInbound:
        body = json.loads(raw_body or b"{}")
        cb = body.get("callback_query")
        if isinstance(cb, dict):
            sender = cb.get("from") or {}
            msg = cb.get("message") or {}
            return ChatInbound(kind="press", external_user_id=str(sender.get("id") or ""),
                               external_name=str(sender.get("username") or ""),
                               token=str(cb.get("data") or ""),
                               channel_id=str((msg.get("chat") or {}).get("id") or ""),
                               thread_id=str(msg.get("message_id") or ""),
                               trigger=str(cb.get("id") or ""), raw=body)
        msg = body.get("message")
        if isinstance(msg, dict):
            chat = msg.get("chat") or {}
            text = str(msg.get("text") or "").strip().lower()
            if chat.get("type") == "private" and text.split()[:1] in (["/link"], ["/start"]):
                sender = msg.get("from") or {}
                return ChatInbound(kind="link_request", external_user_id=str(sender.get("id") or ""),
                                   external_name=str(sender.get("username") or ""),
                                   channel_id=str(chat.get("id") or ""), raw=body)
        return ChatInbound(kind="ignored", raw=body)

    def render_decision(self, card: DecisionCard) -> dict[str, Any]:
        lines = [f"BOSS needs a decision (tier {card.risk_tier})", card.question]
        if card.summary:
            lines.append(card.summary)
        if card.console_only_reason:
            lines.append(card.console_only_reason)
        keyboard = [[{"text": b["label"][:60], "callback_data": b["token"]}
                     for b in card.buttons]] if card.buttons else []
        if card.console_url:
            keyboard.append([{"text": "Open in BOSS", "url": card.console_url}])
        return {"text": "\n\n".join(lines)[:4000],
                "reply_markup": {"inline_keyboard": keyboard}}

    def render_answered(self, question: str, words: str) -> dict[str, Any]:
        return {"text": f"{question}\n\n{words}"[:4000], "reply_markup": {"inline_keyboard": []}}

    def _call(self, bot_token: str, method: str, body: dict) -> dict:
        status, data = TRANSPORT("POST", f"{TELEGRAM_API}/bot{bot_token}/{method}",
                                 {"Content-Type": "application/json"},
                                 json.dumps(body).encode(), "chat.telegram")
        # Never echo the URL: it carries the bot token.
        if status >= 300 or not isinstance(data, dict) or not data.get("ok"):
            desc = (data or {}).get("description") if isinstance(data, dict) else None
            raise ChatSendFailed(f"Telegram refused {method} ({status}): {desc or 'unknown'}")
        return data

    def send(self, bot_token: str, item: Mapping[str, Any]) -> dict[str, Any]:
        kind = item.get("op")
        if kind == "post":
            msg = item["message"]
            data = self._call(bot_token, "sendMessage", {"chat_id": item["channel"], **msg})
            result = data.get("result") or {}
            return {"channel": str((result.get("chat") or {}).get("id") or item["channel"]),
                    "thread": str(result.get("message_id") or "")}
        if kind == "update":
            self._call(bot_token, "editMessageText",
                       {"chat_id": item["channel"], "message_id": int(item["thread"]),
                        **item["message"]})
            return {"channel": item["channel"], "thread": item["thread"]}
        if kind == "ephemeral":
            # The press's own answer (a toast on the presser's phone): answerCallbackQuery.
            self._call(bot_token, "answerCallbackQuery",
                       {"callback_query_id": item["trigger"], "text": item["text"][:200],
                        "show_alert": True})
            return {"channel": item.get("channel")}
        if kind == "dm":
            self._call(bot_token, "sendMessage", {"chat_id": item["user"], "text": item["text"]})
            return {"channel": item["user"]}
        raise ChatSendFailed(f"unknown Telegram outbox op {kind!r}")

    def open_note_form(self, *args: Any, **kwargs: Any) -> None:
        raise ChatSendFailed("Telegram has no form: a decision that needs a note is answered in BOSS")


# --------------------------------------------------------------------------------------------- #
# Discord — a STUB. The interface is implemented so an endpoint can be recorded and the console
# can show it, but it neither verifies (Ed25519 interaction signatures are not wired yet) nor
# sends: every inbound request is refused and every send fails NotConfigured, never silently.
# --------------------------------------------------------------------------------------------- #
class DiscordChat:
    kind = "discord"
    secret_names: tuple[str, ...] = ()
    verify_secret = ""
    supports_note_form = False
    STUB = True

    def verify(self, secret: str, headers: Mapping[str, str], raw_body: bytes,
               now: Optional[float] = None) -> None:
        raise ChatVerifyRefused("discord_not_implemented")

    def parse(self, headers: Mapping[str, str], raw_body: bytes) -> ChatInbound:
        return ChatInbound(kind="ignored")

    def render_decision(self, card: DecisionCard) -> dict[str, Any]:
        return {"content": f"BOSS needs a decision: {card.question}\n{card.console_url}"}

    def render_answered(self, question: str, words: str) -> dict[str, Any]:
        return {"content": f"{question}\n{words}"}

    def send(self, bot_token: str, item: Mapping[str, Any]) -> dict[str, Any]:
        from .github_adapter import IntegrationConfigurationError

        raise IntegrationConfigurationError("Discord chat is not implemented yet (stub)")

    def open_note_form(self, *args: Any, **kwargs: Any) -> None:
        raise ChatSendFailed("Discord chat is not implemented yet (stub)")


# --------------------------------------------------------------------------------------------- #
# Microsoft Teams (P9b-1, BOSSAPP-1100; LLD §9.3)
# --------------------------------------------------------------------------------------------- #
BOTFRAMEWORK_SCOPE = "https://api.botframework.com/.default"
DEFAULT_TEAMS_SERVICE_URL = "https://smba.trafficmanager.net/amer/"
#: Hosts Bot Framework serves conversations from (public, GCC, GCC-High, DoD clouds).
TEAMS_SERVICE_HOST_SUFFIXES = (
    ".trafficmanager.net", ".botframework.com", ".botframework.us", ".botframework.azure.us",
    ".teams.microsoft.com", ".teams.microsoft.us",
)


def _teams_service_url_allowed(service_url: str, cfg: Mapping[str, Any]) -> bool:
    parts = urlsplit(service_url)
    host = (parts.hostname or "").lower()
    if not host:
        return False
    if parts.scheme == "https" and any(host.endswith(sfx) for sfx in TEAMS_SERVICE_HOST_SUFFIXES):
        return True
    configured = urlsplit(str(cfg.get("service_url") or ""))
    cfg_host = (configured.hostname or "").lower()
    return (
        bool(cfg_host)
        and configured.scheme in ("https", "http")
        and parts.scheme == configured.scheme
        and host == cfg_host
    )


class TeamsChat:
    """Microsoft Teams Bot Framework v3 adapter (single-tenant Azure Bot per D5).

    Tokens are obtained per-send from Entra client credentials via ``TRANSPORT`` and never
    stored, cached in VMs, or logged. Inbound verification uses Bot Framework RS256 OIDC JWTs
    (``TeamsIntakeAdapter.verify``).
    """

    kind = "teams"
    secret_names = ("app_password",)
    verify_secret = ""
    supports_note_form = False

    def verify(self, secret: str, headers: Mapping[str, str], raw_body: bytes,
               now: Optional[float] = None, *, config: Optional[Mapping[str, Any]] = None) -> None:
        from ..connectors.intake.contract import IntakeConnection, RawRequest, VerifyRefused
        from ..connectors.intake.teams.adapter import TeamsIntakeAdapter

        cfg = dict(config or {})
        conn = IntakeConnection("teams_chat", "", "teams", config=cfg)
        req = RawRequest(method="POST", headers=_lower(headers), body=raw_body)
        try:
            TeamsIntakeAdapter().verify(conn, req, now if now is not None else time.time())
        except VerifyRefused as refused:
            raise ChatVerifyRefused(refused.reason) from None

    def parse(self, headers: Mapping[str, str], raw_body: bytes) -> ChatInbound:
        body = json.loads(raw_body or b"{}")
        if not isinstance(body, dict):
            return ChatInbound(kind="ignored")
        sender = body.get("from") or {}
        conv = body.get("conversation") or {}
        uid = str(sender.get("id") or "")
        name = str(sender.get("name") or "")
        conv_id = str(conv.get("id") or "")
        reply_to = str(body.get("replyToId") or body.get("id") or "")
        act_type = str(body.get("type") or "")
        value = body.get("value") if isinstance(body.get("value"), dict) else {}
        if act_type in ("invoke", "message") and value.get("boss_token"):
            return ChatInbound(
                kind="press",
                external_user_id=uid,
                external_name=name,
                token=str(value.get("boss_token") or ""),
                action=str(value.get("boss_action") or ""),
                note=(str(value.get("boss_note") or "").strip() or None),
                channel_id=conv_id,
                thread_id=reply_to,
                trigger=str(body.get("id") or ""),
                raw=body,
            )
        if act_type == "message" and conv.get("conversationType") == "personal":
            text_ = str(body.get("text") or "").strip().lower()
            if "link" in text_.split():
                return ChatInbound(
                    kind="link_request",
                    external_user_id=uid,
                    external_name=name,
                    channel_id=conv_id,
                    thread_id=str(body.get("id") or ""),
                    raw=body,
                )
        return ChatInbound(kind="ignored", raw=body)

    def render_decision(self, card: DecisionCard) -> dict[str, Any]:
        body: list[dict[str, Any]] = [
            {"type": "TextBlock", "text": f"BOSS needs a decision · tier {card.risk_tier}",
             "weight": "Bolder", "wrap": True},
            {"type": "TextBlock", "text": card.question[:2000], "wrap": True},
        ]
        if card.summary:
            body.append({"type": "TextBlock", "text": card.summary[:2000], "isSubtle": True, "wrap": True})
        if card.console_only_reason:
            body.append({"type": "TextBlock", "text": card.console_only_reason[:1000],
                         "color": "Warning", "wrap": True})
        actions: list[dict[str, Any]] = []
        for b in card.buttons:
            act: dict[str, Any] = {
                "type": "Action.Submit",
                "title": b["label"][:70],
                "data": {"boss_action": b["action"], "boss_token": b["token"]},
            }
            if b.get("style") == "primary":
                act["style"] = "positive"
            elif b.get("style") == "danger":
                act["style"] = "destructive"
            actions.append(act)
        if card.console_url:
            actions.append({"type": "Action.OpenUrl", "title": "Open in BOSS", "url": card.console_url})
        adaptive = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.5",
            "body": body,
            "actions": actions,
        }
        return {
            "text": f"BOSS needs a decision: {card.question}"[:3000],
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": adaptive}],
        }

    def render_answered(self, question: str, words: str) -> dict[str, Any]:
        adaptive = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.5",
            "body": [
                {"type": "TextBlock", "text": question[:2000], "weight": "Bolder", "wrap": True},
                {"type": "TextBlock", "text": words[:2000], "color": "Good", "wrap": True},
            ],
        }
        return {
            "text": words[:3000],
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": adaptive}],
        }

    def _access_token(self, app_id: str, tenant_id: str, app_password: str,
                      token_url: Optional[str] = None) -> str:
        from urllib.parse import urlencode

        if not app_id or not app_password or not tenant_id:
            raise ChatSendFailed("Teams endpoint requires app_id, tenant_id, and app_password")
        url = (token_url or f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token").strip()
        form = urlencode({
            "grant_type": "client_credentials",
            "client_id": app_id,
            "client_secret": app_password,
            "scope": BOTFRAMEWORK_SCOPE,
        }).encode("utf-8")
        status, data = TRANSPORT(
            "POST", url,
            {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            form, "chat.teams.token",
        )
        if status == 429:
            retry = (data or {}).get("retry_after") if isinstance(data, dict) else None
            raise ChatRateLimited("Teams token endpoint rate-limited",
                                  retry_after=float(retry) if retry else None)
        if status >= 300 or not isinstance(data, dict) or not data.get("access_token"):
            raise ChatSendFailed(f"Teams token exchange failed ({status})")
        return str(data["access_token"])

    def _call(self, access_token: str, method: str, url: str, body: dict) -> dict:
        status, data = TRANSPORT(
            method, url,
            {"Content-Type": "application/json; charset=utf-8",
             "Authorization": f"Bearer {access_token}"},
            json.dumps(body).encode("utf-8"), "chat.teams",
        )
        if status == 429:
            retry = (data or {}).get("retry_after") if isinstance(data, dict) else None
            try:
                seconds = float(retry) if retry not in (None, "") else None
            except (TypeError, ValueError):
                seconds = None
            raise ChatRateLimited("Teams rate-limited", retry_after=seconds)
        if status == 403:
            raw_err = json.dumps(data) if isinstance(data, (dict, list)) else str(data or "")
            if "MessageWritesBlocked" in raw_err or (
                isinstance(data, dict)
                and str((data.get("error") or {}).get("code") or "") == "MessageWritesBlocked"
            ):
                raise ChatWritesBlocked("Teams 403 MessageWritesBlocked", reason="MessageWritesBlocked")
            raise ChatSendFailed("Teams refused 403")
        if status >= 300:
            raise ChatSendFailed(f"Teams answered {status}")
        return dict(data) if isinstance(data, dict) else {}

    @staticmethod
    def _marker(item: Mapping[str, Any]) -> dict[str, Any]:
        payload = {k: v for k, v in {
            "endpoint_id": item.get("endpoint_id"),
            "run_id": item.get("run_id"),
            "deployment_id": item.get("deployment_id"),
            "hops": item.get("hops"),
        }.items() if v not in (None, "")}
        return {"boss": payload}

    def send(self, bot_token: str, item: Mapping[str, Any]) -> dict[str, Any]:
        return self.send_with_config(bot_token, item, item.get("teams_config") or {})

    def send_with_config(self, app_password: str, item: Mapping[str, Any],
                         config: Mapping[str, Any]) -> dict[str, Any]:
        cfg = dict(config or {})
        conv_ref = item.get("conversation_ref") if isinstance(item.get("conversation_ref"), dict) else {}
        app_id = str(cfg.get("app_id") or "").strip()
        tenant_id = str(cfg.get("tenant_id") or conv_ref.get("tenantId") or "").strip()
        service_url = str(
            item.get("service_url")
            or conv_ref.get("serviceUrl")
            or cfg.get("service_url")
            or DEFAULT_TEAMS_SERVICE_URL
        ).strip().rstrip("/")
        bot_id = str(cfg.get("bot_id") or (conv_ref.get("bot") or {}).get("id") or (f"28:{app_id}" if app_id else "")).strip()
        token_url = str(cfg.get("token_url") or "").strip() or None
        if not _teams_service_url_allowed(service_url, cfg):
            # The bearer token is sent to this URL: only Bot Framework's own hosts (or the one
            # the operator configured on the endpoint) ever receive it.
            raise ChatSendFailed("Teams serviceUrl is not a Bot Framework host; refusing to send")

        access_token = self._access_token(app_id, tenant_id, app_password, token_url=token_url)
        kind = str(item.get("op") or "post")
        raw_channel = str(item.get("channel") or (conv_ref.get("conversation") or {}).get("id") or "").strip()
        raw_thread = str(item.get("thread") or "").strip()
        # Conversation and activity ids carry `:`, `@` and `;` — always one escaped path segment.
        channel = quote(raw_channel, safe="")
        thread = quote(raw_thread, safe="")

        if kind == "dm":
            if not channel and item.get("user"):
                created = self._call(
                    access_token, "POST", f"{service_url}/v3/conversations",
                    {"bot": {"id": bot_id, "name": "BOSS"},
                     "members": [{"id": str(item["user"])}],
                     "tenantId": tenant_id,
                     "isGroup": False},
                )
                raw_channel = str(created.get("id") or "").strip()
                channel = quote(raw_channel, safe="")
            if not channel:
                raise ChatSendFailed("Teams opened no direct message conversation")
            msg = item.get("message") if isinstance(item.get("message"), dict) else {"text": str(item.get("text") or "")}
            body = {"type": "message", **msg, "channelData": self._marker(item)}
            data = self._call(access_token, "POST", f"{service_url}/v3/conversations/{channel}/activities", body)
            act_id = str(data.get("id") or "")
            return {"channel": raw_channel, "thread": act_id or raw_channel, "ts": act_id, "bot_id": bot_id}

        if kind == "ephemeral":
            target_ch = channel
            if not target_ch and item.get("user"):
                return self.send_with_config(app_password, {**item, "op": "dm"}, cfg)
            msg = {"type": "message", "text": str(item.get("text") or ""), "channelData": self._marker(item)}
            url = (f"{service_url}/v3/conversations/{target_ch}/activities/{thread}"
                   if thread else f"{service_url}/v3/conversations/{target_ch}/activities")
            data = self._call(access_token, "POST", url, msg)
            act_id = str(data.get("id") or "")
            return {"channel": raw_channel, "thread": raw_thread or act_id, "ts": act_id, "bot_id": bot_id}

        if kind in ("set_status", "typing"):
            data = self._call(
                access_token, "POST", f"{service_url}/v3/conversations/{channel}/activities",
                {"type": "typing"},
            )
            return {"channel": raw_channel, "thread": raw_thread or str(data.get("id") or "")}

        if kind == "post":
            msg = dict(item.get("message") or {"text": str(item.get("text") or "")})
            body = {"type": "message", **msg, "channelData": self._marker(item)}
            url = (f"{service_url}/v3/conversations/{channel}/activities/{thread}"
                   if thread else f"{service_url}/v3/conversations/{channel}/activities")
            data = self._call(access_token, "POST", url, body)
            act_id = str(data.get("id") or "")
            return {"channel": raw_channel, "thread": act_id or raw_thread, "ts": act_id, "bot_id": bot_id}

        if kind == "update":
            act_id = raw_thread or str(item.get("ts") or "")
            msg = dict(item.get("message") or {"text": str(item.get("text") or "")})
            body = {"type": "message", "id": act_id, **msg, "channelData": self._marker(item)}
            data = self._call(
                access_token, "PUT",
                f"{service_url}/v3/conversations/{channel}/activities/{quote(act_id, safe='')}", body
            )
            return {"channel": raw_channel, "thread": str(data.get("id") or act_id), "ts": str(data.get("id") or act_id)}

        if kind in ("stream_start", "stream_append", "stream_stop"):
            seq = int(item.get("stream_seq") or (1 if kind == "stream_start" else 2))
            stream_id = str(item.get("ts") or item.get("stream_id") or "")
            if kind == "stream_stop":
                body: dict[str, Any] = {
                    "type": "message",
                    "text": str(item.get("full") or item.get("text") or ""),
                    "channelData": {**self._marker(item), "streamType": "final", "streamId": stream_id},
                }
            else:
                body = {
                    "type": "typing",
                    "text": str(item.get("full") or item.get("text") or ""),
                    "channelData": {
                        "streamType": "informative" if kind == "stream_start" else "streaming",
                        "streamSequence": seq,
                        **({"streamId": stream_id} if stream_id else {}),
                    },
                }
            data = self._call(access_token, "POST", f"{service_url}/v3/conversations/{channel}/activities", body)
            new_id = str(data.get("id") or stream_id)
            return {"channel": raw_channel, "ts": new_id, "thread": raw_thread or new_id}

        raise ChatSendFailed(f"unknown Teams outbox op {kind!r}")

    def open_note_form(self, *args: Any, **kwargs: Any) -> None:
        raise ChatSendFailed("Teams note forms are answered in BOSS")


ADAPTERS: dict[str, Any] = {"slack": SlackChat(), "telegram": TelegramChat(),
                            "discord": DiscordChat(), "teams": TeamsChat()}


def adapter_for(kind: str) -> Any:
    try:
        return ADAPTERS[kind]
    except KeyError:
        raise ValueError(f"unknown chat kind {kind!r}") from None


__all__ = ["ADAPTERS", "BOTFRAMEWORK_SCOPE", "ChatInbound", "ChatRateLimited", "ChatSendFailed",
           "ChatVerifyRefused", "ChatWritesBlocked", "DecisionCard", "DiscordChat", "SLACK_MARKER",
           "SlackChat", "TeamsChat", "TelegramChat", "TRANSPORT", "adapter_for", "slack_signature"]
