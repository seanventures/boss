"""Slack on the intake layer (P9a-3, BOSSAPP-1095; boss-eventing-lld.md §3.7, §5.2, §7.1, §9).

ONE SLACK APP PER INSTALL (§15 D1). A Slack connection is bound to the install's Slack CHAT
ENDPOINT (`chat_endpoints`, services/chat_channels.py): its signing secret verifies the Events API,
its bot token answers through the reply outbox, its identity links say who a Slack user is. The
connection only stores what the endpoint does not (an app-level token for Socket Mode, a default
DM agent); the endpoint's secrets resolve through the connection at the moment of use.

verify     `v0=` HMAC-SHA256 over `v0:<X-Slack-Request-Timestamp>:<raw body>`, 5-minute window,
           constant time (the chat route's verifier, chat_adapters.SlackChat.verify)
handshake  `url_verification` answers the challenge and stores nothing
dedupe     `event_id` (unique across workspaces); Slack's redeliveries carry the same id and
           X-Slack-Retry-Num 1..3, so a redelivery is a duplicate at the inbox, never a second run
types      com.slack.message.app_mention   an @mention of the app in a channel
           com.slack.message.im            a direct message to the app
           com.slack.message.thread_reply  a reply inside a thread (no mention needed: the router
                                           continues a thread the agent already owns)
           com.slack.message.channel       any other human message in a channel the app is in
                                           (only `channel_all` routes take these)
conversation  channel + thread_ts (a top-level mention starts the thread at its own ts); a direct
           message outside a thread is one conversation per DM per day (§15 D11)
self       loop layer 1 (§7.1): our bot user / bot id, and edits/deletes (`message_changed`,
           `message_deleted`, `message_replied`) never wake anyone. Every message BOSS posts
           carries `metadata {event_type: boss_run, event_payload: {run_id, deployment_id, hops,
           endpoint_id}}`; our own marker is self, another install's marker is causality (layer 2,
           the hop counter). Other bots are kept as `bot` actors: a route must allow them.
mentions   a channel `message` that @mentions our bot is dropped: Slack also sends the same words
           as an `app_mention`, and one message must never start two runs.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

from ..base import IntakeAdapterBase
from ..contract import (
    ExternalActor,
    IntakeConnection,
    IntakeEvent,
    OutboxOp,
    ParseRefused,
    PersonHint,
    RawRequest,
    RawResponse,
    Receipt,
    SecretView,
    StoredRaw,
    Verified,
    VerifyRefused,
)

RETRY_NUM = "x-slack-retry-num"
RETRY_REASON = "x-slack-retry-reason"
#: The metadata event type every message BOSS posts to Slack carries (loop prevention, layer 2).
MARKER = "boss_run"
#: Message subtypes that are edits/housekeeping, never a person speaking (loop layer 1).
EDIT_SUBTYPES = ("message_changed", "message_deleted", "message_replied")
#: Subtypes that are still a person (or a bot) saying something.
SPOKEN_SUBTYPES = ("", "thread_broadcast", "file_share", "me_message", "bot_message")
_MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
MAX_TEXT = 40000


def _body(raw: bytes) -> dict:
    try:
        body = json.loads((raw or b"{}").decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


def strip_bot_mention(text: str, bot_user_id: str) -> str:
    """The person's words without the leading `<@BOT>` that addressed us."""
    words = text or ""
    if bot_user_id:
        words = re.sub(rf"^\s*<@{re.escape(bot_user_id)}(?:\|[^>]*)?>[\s,:]*", "", words)
    else:
        words = re.sub(r"^\s*<@[UW][A-Z0-9]+(?:\|[^>]*)?>[\s,:]*", "", words)
    return words.strip()


def _day(ts: str) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def marker_of(event: dict) -> Optional[dict]:
    """The `boss_run` metadata a message carries, if any."""
    meta = event.get("metadata")
    if isinstance(meta, dict) and meta.get("event_type") == MARKER:
        payload = meta.get("event_payload")
        return payload if isinstance(payload, dict) else {}
    return None


class SlackIntakeAdapter(IntakeAdapterBase):
    key = "slack"

    # -- PUSH -------------------------------------------------------------------------------------

    def verify(self, conn: IntakeConnection, req: RawRequest, now: float) -> Verified:
        from ....services.chat_adapters import ChatVerifyRefused, SlackChat

        secret = conn.secret("signing_secret") or ""
        try:
            SlackChat().verify(secret, req.headers, req.body, now=now)
        except ChatVerifyRefused as refused:
            raise VerifyRefused(refused.reason, provider=self.key) from None
        retry = None
        num = req.header(RETRY_NUM).strip()
        if num:
            retry = {"num": int(num) if num.isdigit() else 1,
                     "reason": req.header(RETRY_REASON).strip()[:60] or None}
        ts = req.header("x-slack-request-timestamp").strip()
        return Verified(scheme="slack_v0", timestamp=int(ts) if ts.isdigit() else None,
                        retry=retry)

    def handshake(self, conn: IntakeConnection, req: RawRequest) -> Optional[RawResponse]:
        if len(req.body) > 4096 or b"url_verification" not in req.body:
            return None
        body = _body(req.body)
        if body.get("type") != "url_verification":
            return None
        return RawResponse(status=200, body={"challenge": str(body.get("challenge") or "")})

    def dedupe_key(self, conn: IntakeConnection, req: RawRequest) -> str:
        event_id = str(_body(req.body).get("event_id") or "").strip()[:200]
        if event_id:
            return f"slack:{event_id}"
        return "slack:sha256:" + req.body_sha256()

    # -- PROCESSOR --------------------------------------------------------------------------------

    def parse(self, conn: IntakeConnection, raw: StoredRaw) -> list[IntakeEvent]:
        body = _body(raw.body)
        if not body:
            raise ParseRefused("Slack body is not a JSON object", provider=self.key)
        if body.get("type") != "event_callback":
            return []        # app_rate_limited and the like: nothing a person said
        ev = body.get("event") if isinstance(body.get("event"), dict) else {}
        etype = str(ev.get("type") or "")
        if etype not in ("app_mention", "message"):
            return []
        subtype = str(ev.get("subtype") or "")
        if subtype in EDIT_SUBTYPES or subtype not in SPOKEN_SUBTYPES:
            return []        # an edit, a delete, a join: loop layer 1 / not a message
        team = str(body.get("team_id") or ev.get("team") or "")
        own_team = str(conn.get("team_id") or "")
        if own_team and team and team != own_team and not body.get("is_ext_shared_channel"):
            return []        # another workspace's event on our URL
        bot_user = str(conn.get("bot_user_id") or "") or next(
            (str(a.get("user_id") or "") for a in (body.get("authorizations") or [])
             if isinstance(a, dict) and a.get("is_bot")), "")
        channel = str(ev.get("channel") or "")
        channel_type = str(ev.get("channel_type") or ("im" if channel.startswith("D") else ""))
        ts = str(ev.get("ts") or ev.get("event_ts") or "")
        thread_ts = str(ev.get("thread_ts") or "")
        raw_text = str(ev.get("text") or "")[:MAX_TEXT]
        mentioned = sorted(set(_MENTION.findall(raw_text)))
        mentions_bot = bool(bot_user) and bot_user in mentioned
        if etype == "message" and channel_type != "im" and mentions_bot:
            return []        # the app_mention event carries these words; one message, one run
        if etype == "app_mention":
            type_ = "com.slack.message.app_mention"
        elif channel_type == "im":
            type_ = "com.slack.message.im"
        elif thread_ts and thread_ts != ts:
            type_ = "com.slack.message.thread_reply"
        else:
            type_ = "com.slack.message.channel"
        if channel_type == "im" and not thread_ts:
            key, reply_thread = f"{channel}/{_day(ts)}", None
        else:
            key, reply_thread = f"{channel}/{thread_ts or ts}", (thread_ts or ts)
        bot_id = str(ev.get("bot_id") or "")
        user = str(ev.get("user") or "")
        if bot_id or subtype == "bot_message":
            actor = ExternalActor(kind="bot", external_id=bot_id or user,
                                  display=str(ev.get("username") or
                                              (ev.get("bot_profile") or {}).get("name") or ""))
        elif user:
            actor = ExternalActor(kind="user", external_id=user,
                                  display=str(ev.get("user_name") or ""))
        else:
            actor = ExternalActor(kind="unknown")
        text_ = strip_bot_mention(raw_text, bot_user) if (etype == "app_mention" or mentions_bot
                                                          or channel_type == "im") else raw_text
        data: dict[str, Any] = {
            "text": text_, "raw_text": raw_text, "channel": channel, "channel_type": channel_type,
            "ts": ts, "thread_ts": thread_ts or None, "team_id": team or None,
            "user_team_id": str(ev.get("user_team") or ev.get("source_team") or team) or None,
            "user": user or None, "bot_id": bot_id or None, "subtype": subtype or None,
            "bot_user_id": bot_user or None, "mentions": mentioned,
            "mentions_bot": etype == "app_mention" or mentions_bot,
            "addressed": etype == "app_mention" or channel_type == "im",
            "files": len(ev.get("files") or []) if isinstance(ev.get("files"), list) else 0,
        }
        out = IntakeEvent(
            id=str(body.get("event_id") or raw.dedupe_key.split(":", 1)[-1]),
            source=f"slack://{team or 'unknown'}", type=type_, subject=key,
            time=(datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
                  if ts.replace(".", "", 1).isdigit() else None),
            data=data, bossactor=actor, bossconnection=conn.id, bossdedupe=raw.dedupe_key,
            bossconversation={"key": key, "channel": channel, "thread_ts": thread_ts or ts,
                              "reply_thread_ts": reply_thread, "team_id": team or None})
        marker = marker_of(ev)
        if marker is not None:
            out.bosscausality = {"run_id": marker.get("run_id"),
                                 "deployment_id": marker.get("deployment_id"),
                                 "hops": int(marker.get("hops") or 0),
                                 "endpoint_id": marker.get("endpoint_id")}
        return [out]

    def is_self(self, conn: IntakeConnection, ev: IntakeEvent) -> bool:
        data = ev.data or {}
        ours_user = str(conn.get("bot_user_id") or data.get("bot_user_id") or "")
        ours_bot = str(conn.get("bot_id") or "")
        if ours_user and data.get("user") == ours_user:
            return True
        if ours_bot and data.get("bot_id") == ours_bot:
            return True
        causality = ev.bosscausality or {}
        endpoint = str(conn.get("chat_endpoint_id") or "")
        return bool(causality) and bool(endpoint) and causality.get("endpoint_id") == endpoint

    def resolve_person(self, conn: IntakeConnection, actor: ExternalActor) -> PersonHint:
        return PersonHint(email=actor.email, display=actor.display, is_bot=actor.kind == "bot")

    def conversation_key(self, conn: IntakeConnection, ev: IntakeEvent) -> Optional[str]:
        return (ev.bossconversation or {}).get("key") or ev.subject

    # -- REPLY (the outbox sender only; never a run) ----------------------------------------------

    def _item(self, op: OutboxOp) -> dict:
        conv = dict(op.conversation or {})
        item = {"op": op.op, "channel": conv.get("channel"), **dict(op.message or {})}
        if conv.get("thread_ts") and "thread" not in item:
            item["thread"] = conv["thread_ts"]
        if "text" in item and "message" not in item and op.op in ("post", "update"):
            item["message"] = {"text": item.pop("text")}
        return item

    def send(self, conn: IntakeConnection, op: OutboxOp, secrets: SecretView) -> Receipt:
        from ....services.chat_adapters import SlackChat

        receipt = SlackChat().send(secrets.get("bot_token") or "", self._item(op))
        return Receipt(ok=True, ref=dict(receipt or {}))

    def send_stream(self, conn: IntakeConnection, op: OutboxOp, secrets: SecretView) -> Receipt:
        if op.op not in ("stream_start", "stream_append", "stream_stop"):
            raise ValueError(f"not a stream op: {op.op}")
        return self.send(conn, op, secrets)


__all__ = ["EDIT_SUBTYPES", "MARKER", "SlackIntakeAdapter", "marker_of", "strip_bot_mention"]
