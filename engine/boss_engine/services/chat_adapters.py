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
from urllib.parse import parse_qs

#: Slack refuses a request older than this (its own guidance: 5 minutes); so do we.
SLACK_TOLERANCE_SECONDS = 300
SLACK_API = "https://slack.com/api"
TELEGRAM_API = "https://api.telegram.org"

#: Slack action ids. A press is recognised by these, never by the button's label.
SLACK_ACTION_PREFIX = "boss_decide_"
SLACK_NOTE_CALLBACK = "boss_decide_note"
SLACK_NOTE_BLOCK = "note"
SLACK_NOTE_INPUT = "note_input"


class ChatVerifyRefused(Exception):
    """The request did not prove it came from the channel. `reason` is one word for the log."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ChatSendFailed(RuntimeError):
    """The channel refused or could not be reached. The message never includes a secret."""


def _default_transport(method: str, url: str, headers: Mapping[str, str],
                       body: Optional[bytes], purpose: str) -> tuple[int, Any]:
    from ..identity.http import fetch

    resp = fetch(method, url, headers=dict(headers), data=body, purpose=purpose)
    try:
        data = resp.json()
    except ValueError:
        data = None
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
        if status >= 300 or not isinstance(data, dict):
            raise ChatSendFailed(f"Slack answered {status} to {method}")
        if not data.get("ok"):
            raise ChatSendFailed(f"Slack refused {method}: {data.get('error') or 'unknown'}")
        return data

    def send(self, bot_token: str, item: Mapping[str, Any]) -> dict[str, Any]:
        kind = item.get("op")
        if kind == "post":
            body = {"channel": item["channel"], **item["message"]}
            if item.get("thread"):
                body["thread_ts"] = item["thread"]
            data = self._call(bot_token, "chat.postMessage", body)
            return {"channel": data.get("channel") or item["channel"], "thread": data.get("ts")}
        if kind == "update":
            data = self._call(bot_token, "chat.update",
                              {"channel": item["channel"], "ts": item["thread"],
                               **item["message"]})
            return {"channel": item["channel"], "thread": data.get("ts") or item["thread"]}
        if kind == "ephemeral":
            self._call(bot_token, "chat.postEphemeral",
                       {"channel": item["channel"], "user": item["user"], "text": item["text"]})
            return {"channel": item["channel"]}
        if kind == "dm":
            opened = self._call(bot_token, "conversations.open", {"users": item["user"]})
            channel = ((opened.get("channel") or {}).get("id")) or ""
            if not channel:
                raise ChatSendFailed("Slack opened no direct message channel")
            self._call(bot_token, "chat.postMessage", {"channel": channel, "text": item["text"]})
            return {"channel": channel}
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


ADAPTERS: dict[str, Any] = {"slack": SlackChat(), "telegram": TelegramChat(),
                            "discord": DiscordChat()}


def adapter_for(kind: str) -> Any:
    try:
        return ADAPTERS[kind]
    except KeyError:
        raise ValueError(f"unknown chat kind {kind!r}") from None


__all__ = ["ADAPTERS", "ChatInbound", "ChatSendFailed", "ChatVerifyRefused", "DecisionCard",
           "DiscordChat", "SlackChat", "TelegramChat", "TRANSPORT", "adapter_for",
           "slack_signature"]
