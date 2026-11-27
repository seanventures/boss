"""THE REPLY TOOLS AND THE REPLY OUTBOX (P9a-6, BOSSAPP-1098; boss-eventing-lld.md §7.2, §9).

A run that a chat conversation started (its finalize_ctx carries `reply_to`, written by the
intake router) answers through three CONTROL-PLANE tools:

  chat_reply(text, final=true)   the answer, in the conversation that started the run
  chat_status(text)              a short status line ("Looking into it…"), edited in place
  chat_react(emoji)              a reaction on the person's message

(the LLD's `chat.reply` / `chat.status` / `chat.react`; tool names carry no dots because model
APIs refuse them). Each call only INSERTS a `chat_outbox` row — it never holds a token, an
endpoint id it could re-target, or a Slack URL:

  * THE TARGET COMES FROM THE RUN (`finalize_ctx.reply_to`), never from arguments: a run can only
    answer the conversation that started it.
  * FENCED: the insert happens under the run's lease (the local worker's `require_active_lease`,
    the run plane's epoch check before the tool runs) and is refused when a newer epoch of the run
    has written or holds the job — a stale replica's reply is never sent.
  * ORDERED AND DEDUPED: each row gets the run's next `op_seq` (unique per run) and a dedupe key
    per tool call (`chat:<run>:<step>:<call>`), so a re-executed call never posts twice and the
    sender sends a run's rows in order.

EDIT IN PLACE (§9.2). The router posts "Looking into it…" the moment it admits the run (the
first sign of life, before the agent has started). That message is the run's LIVE message: a
status or a partial reply edits it (`chat.update`), the final reply edits it into the answer and
closes it; anything after that posts anew. With streaming on (`slack_streaming_enabled`, behind
a flag) a partial answer streams through `chat.startStream` / `appendStream` / `stopStream`
instead, and the first sign of life is an 👀 reaction.

THE OUTBOX (§9.4; notify.deliver_chat_due sends). A NOTIFY on every insert wakes the sender
(`start`) within milliseconds instead of the minute tick; a per-channel token bucket keeps Slack's
≈1 message/s per channel; a 429 is honoured through Retry-After WITHOUT spending an attempt; an
edit superseded by a later edit of the same message is skipped. The bot token is resolved from
the secret store by the sender at send time (chat_channels.deliver) and lives in the API process
for that one call.

A run that ends without a final reply (an agent that just answered in text) has that text posted
as its final reply when it settles (`on_run_settled`), unless its route says `reply: tool`.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id

log = logging.getLogger("boss.chat.reply")

#: The tools, by name (the LLD's chat.reply / chat.status / chat.react).
REPLY, STATUS, REACT = "chat_reply", "chat_status", "chat_react"
TOOL_NAMES = (REPLY, STATUS, REACT)
#: Outbox kinds a run's ops are written as.
KINDS = ("agent_status", "agent_reply", "agent_react", "agent_stream")
DEFAULT_STATUS = "Looking into it…"
#: Slack renders a section of at most 3,000 chars; one message keeps under 4,000.
MAX_CHUNK = 3500
#: Per-channel send rate (Slack chat.postMessage ≈ 1/s per channel, short bursts allowed).
CHANNEL_RATE_PER_SECOND = 1.0
CHANNEL_BURST = 3.0
#: A row the channel rate-limits more often than this is counted as a failed attempt (it
#: then backs off and dead-letters like any failure) instead of waiting forever.
MAX_RATE_LIMITED = 20
#: The run plane sets this to the lease epoch it just checked (run_plane.tool_invoke).
FENCE_EPOCH: contextvars.ContextVar = contextvars.ContextVar("boss_chat_fence_epoch", default=None)
NOTIFY_CHANNEL = "boss_chat_outbox"


class ReplyFenced(RuntimeError):
    """A stale executor tried to reply for a run a newer epoch holds."""


class NotYet(RuntimeError):
    """The row waits on an earlier one (its live message is not posted yet): reschedule, no
    attempt spent."""

    def __init__(self, message: str, delay: float = 0.25):
        super().__init__(message)
        self.delay = delay


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


def streaming_enabled(settings: Any = None) -> bool:
    flag = getattr(settings, "slack_streaming_enabled", None) if settings is not None else None
    if flag is None:
        flag = os.environ.get("BOSS_SLACK_STREAMING", "").strip().lower() in ("1", "true", "on")
    return bool(flag)


# ---------------------------------------------------------------------------------------------
# The run's conversation and its fence
# ---------------------------------------------------------------------------------------------

def _run(connection: Connection, run_id: str) -> Optional[dict]:
    row = connection.execute(text(
        "SELECT id, project_id, status, finalize_ctx, output_text, error, job_id, deployment_id "
        "FROM agent_runs WHERE id = :r"), {"r": run_id}).mappings().first()
    return dict(row) if row else None


def reply_to_of(run: Optional[Mapping[str, Any]]) -> Optional[dict]:
    fctx = _json((run or {}).get("finalize_ctx"), {}) or {}
    target = fctx.get("reply_to")
    if not isinstance(target, dict) or not target.get("chat_endpoint_id") \
            or not target.get("channel"):
        return None
    return target


def current_epoch(connection: Connection, run_id: str) -> int:
    """The lease epoch this executor holds for the run, checked NOW: the run plane's (it fenced
    the call before the tool ran), else the local worker's lease (require_active_lease raises
    LeaseNotFoundError when another worker holds it), else the job's own epoch (a router or a
    settle hook, already inside the run's transaction)."""
    epoch = FENCE_EPOCH.get()
    if epoch is not None:
        return int(epoch)
    try:
        from .agent_run_worker import drive_options

        fence = drive_options().get("fence")
    except Exception:  # noqa: BLE001 — no drive in context
        fence = None
    if callable(fence):
        row = fence(connection)
        if isinstance(row, Mapping) and row.get("fence_epoch") is not None:
            return int(row["fence_epoch"])
    return int(connection.execute(text(
        "SELECT COALESCE(j.fence_epoch, 0) FROM agent_runs r LEFT JOIN jobs j ON j.id = r.job_id "
        "WHERE r.id = :r"), {"r": run_id}).scalar() or 0)


def _open_live(connection: Connection, run_id: str) -> Optional[dict]:
    """The run's live message (the last status/partial reply not closed by a final reply)."""
    row = connection.execute(text(
        "SELECT id, payload FROM chat_outbox WHERE run_id = :r "
        "AND kind IN ('agent_status', 'agent_reply', 'agent_stream') "
        "ORDER BY op_seq DESC LIMIT 1"), {"r": run_id}).mappings().first()
    if row is None:
        return None
    payload = _json(row["payload"], {}) or {}
    if payload.get("final") or not payload.get("live_id"):
        return None
    return {"live_id": payload["live_id"], "stream": bool(payload.get("stream")),
            "full": str(payload.get("full") or "")}


def _insert(connection: Connection, *, run: Mapping[str, Any], target: Mapping[str, Any],
            kind: str, payload: dict, dedupe: str, epoch: int, row_id: Optional[str] = None
            ) -> dict:
    """One fenced, ordered, deduplicated outbox row for the run."""
    run_id = str(run["id"])
    connection.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                       {"k": f"chat_run:{run_id}"})
    existing = connection.execute(text(
        "SELECT id, op_seq FROM chat_outbox WHERE dedupe_key = :d"), {"d": dedupe}).mappings().first()
    if existing is not None:
        return {"outbox_id": existing["id"], "op_seq": existing["op_seq"], "duplicate": True}
    newest = connection.execute(text("""
        SELECT GREATEST(
            COALESCE((SELECT max(fence_epoch) FROM chat_outbox WHERE run_id = :r), 0),
            COALESCE((SELECT j.fence_epoch FROM agent_runs a JOIN jobs j ON j.id = a.job_id
                       WHERE a.id = :r), 0))"""), {"r": run_id}).scalar() or 0
    if int(epoch) < int(newest):
        emit_event(connection, str(run["project_id"]), "chat.reply_fenced", "boss.chat",
                   "agent_run", run_id, {"epoch": int(epoch), "current": int(newest)})
        raise ReplyFenced(f"epoch {epoch} is stale; the run is at {newest}")
    seq = int(connection.execute(text(
        "SELECT COALESCE(max(op_seq), 0) + 1 FROM chat_outbox WHERE run_id = :r"),
        {"r": run_id}).scalar() or 1)
    row_id = row_id or make_id("cob")
    body = {"endpoint_id": target["chat_endpoint_id"], "run_id": run_id,
            "deployment_id": target.get("deployment_id") or run.get("deployment_id"),
            "hops": int(target.get("hops") or 0) + 1, **payload}
    connection.execute(text("""
        INSERT INTO chat_outbox (id, endpoint_id, project_id, kind, dedupe_key, payload, run_id,
               fence_epoch, op_seq, chat_thread_id, channel_key)
        VALUES (:i, :e, :p, :k, :d, CAST(:pl AS JSONB), :r, :ep, :seq, :ct, :ck)"""),
        {"i": row_id, "e": target["chat_endpoint_id"], "p": run["project_id"], "k": kind,
         "d": dedupe, "pl": json.dumps(body, default=str), "r": run_id, "ep": int(epoch),
         "seq": seq, "ct": target.get("chat_thread_id"),
         "ck": f"{target['chat_endpoint_id']}:{target['channel']}"})
    return {"outbox_id": row_id, "op_seq": seq, "duplicate": False}


#: Slack's special tokens that notify people: broadcasts (`<!channel>`, `<!here>`,
#: `<!everyone>`), user groups (`<!subteam^S…>`) and users (`<@U…>` / `<@W…>`).
_PING = re.compile(r"<\s*(!\s*(?:channel|here|everyone|group)\b|!\s*subteam\^[^>|]*|@\s*[UWB][A-Z0-9]*)"
                   r"(?:\|([^>]*))?\s*>", re.I)
_ZWSP = "\u200b"


def inert(words: str) -> str:
    """AGENT TEXT NEVER PINGS ANYONE (review of P9a-6): a prompt-injected reply could otherwise
    @channel a whole workspace or page a user group. Every broadcast, user-group and user
    mention token is rewritten to plain text with a zero-width space after the `@`, which Slack
    renders but never resolves; any other `<!…>` command is defused the same way."""
    def plain(match: "re.Match[str]") -> str:
        token, label = match.group(1).replace(" ", ""), match.group(2)
        if token.lower().startswith("!subteam"):
            name = label or "group"
        elif token.startswith("@"):
            name = label or "user"
        else:
            name = token[1:].lower()
        return "@" + _ZWSP + name.lstrip("@")

    words = _PING.sub(plain, str(words or ""))
    words = re.sub(r"<\s*!", "<" + _ZWSP + "!", words)
    # A bare `@here` / `@channel` / `@everyone` is only text without link_names, but keep it inert.
    return re.sub(r"(?<![\w\u200b])@(here|channel|everyone)\b", "@" + _ZWSP + r"\1", words,
                  flags=re.I)


def _chunks(words: str) -> list[str]:
    words = str(words or "").strip() or "(no answer)"
    out = []
    while words:
        if len(words) <= MAX_CHUNK:
            out.append(words)
            break
        cut = words.rfind("\n", 0, MAX_CHUNK)
        cut = cut if cut > MAX_CHUNK // 2 else MAX_CHUNK
        out.append(words[:cut])
        words = words[cut:].lstrip()
    return out


# ---------------------------------------------------------------------------------------------
# The three ops
# ---------------------------------------------------------------------------------------------

def say(connection: Connection, run_id: str, words: str, *, final: bool, kind: str,
        dedupe: str, epoch: Optional[int] = None, settings: Any = None) -> dict:
    """A status or a reply in the run's conversation: edits the live message when there is one,
    posts otherwise; a final reply closes the live message. With streaming on, a reply streams."""
    run = _run(connection, run_id)
    target = reply_to_of(run)
    if run is None or target is None:
        return {"ok": False, "status": "refused", "error": "no_conversation",
                "message": "This run was not started by a chat conversation; there is nobody "
                           "to reply to."}
    epoch = current_epoch(connection, run_id) if epoch is None else int(epoch)
    words = inert(words)
    live = _open_live(connection, run_id)
    base = {"channel": target["channel"], "thread": target.get("thread_ts")}
    stream = (kind == "agent_reply" and bool(target.get("stream")) and streaming_enabled(settings))
    if stream:
        return _stream(connection, run, target, live, words, final=final, dedupe=dedupe,
                       epoch=epoch, base=base)
    parts = _chunks(words)
    written = []
    for n, part in enumerate(parts):
        last = n == len(parts) - 1
        is_final = final and last
        key = dedupe if n == 0 else f"{dedupe}:{n}"
        if n == 0 and live is not None and not live["stream"]:
            payload = {**base, "op": "update", "ref": live["live_id"], "live_id": live["live_id"],
                       "message": {"text": part}, "final": is_final, "text_kind": kind}
            written.append(_insert(connection, run=run, target=target, kind=kind,
                                   payload=payload, dedupe=key, epoch=epoch))
            continue
        row_id = make_id("cob")
        payload = {**base, "op": "post", "message": {"text": part}, "final": is_final,
                   "live_id": row_id if not is_final else None, "text_kind": kind}
        written.append(_insert(connection, run=run, target=target, kind=kind, payload=payload,
                               dedupe=key, epoch=epoch, row_id=row_id))
    return {"ok": True, "status": "queued", "outbox_ids": [w["outbox_id"] for w in written],
            "op_seq": [w["op_seq"] for w in written],
            "duplicate": all(w["duplicate"] for w in written)}


def _stream(connection: Connection, run: Mapping[str, Any], target: Mapping[str, Any],
            live: Optional[dict], words: str, *, final: bool, dedupe: str, epoch: int,
            base: dict) -> dict:
    """chat.startStream → appendStream → stopStream. `words` is the answer so far; the delta
    against what was streamed is what is appended."""
    words = str(words or "")
    # chat.startStream needs a thread: a top-level DM streams under the person's message.
    base = {**base, "thread": base.get("thread") or target.get("trigger_ts")}
    recipients = {"recipient_user_id": target.get("external_user_id"),
                  "recipient_team_id": target.get("team_id")}
    if live is None or not live["stream"]:
        row_id = make_id("cob")
        op = "stream_start"
        payload = {**base, **recipients, "op": op, "text": words[:12000], "stream": True,
                   "live_id": row_id if not final else None, "full": words, "final": False}
        out = [_insert(connection, run=run, target=target, kind="agent_stream", payload=payload,
                       dedupe=dedupe, epoch=epoch, row_id=row_id)]
        if final:
            out.append(_insert(connection, run=run, target=target, kind="agent_stream",
                               payload={**base, "op": "stream_stop", "ref": row_id,
                                        "stream": True, "final": True, "full": words},
                               dedupe=f"{dedupe}:stop", epoch=epoch))
        return {"ok": True, "status": "queued", "outbox_ids": [o["outbox_id"] for o in out],
                "op_seq": [o["op_seq"] for o in out]}
    prev = live["full"]
    delta = words[len(prev):] if words.startswith(prev) else ("\n" + words if words else "")
    payload = {**base, "op": "stream_stop" if final else "stream_append", "ref": live["live_id"],
               "text": delta[:12000], "stream": True, "final": bool(final),
               "live_id": None if final else live["live_id"], "full": words}
    got = _insert(connection, run=run, target=target, kind="agent_stream", payload=payload,
                  dedupe=dedupe, epoch=epoch)
    return {"ok": True, "status": "queued", "outbox_ids": [got["outbox_id"]],
            "op_seq": [got["op_seq"]]}


def react(connection: Connection, run_id: str, emoji: str, *, dedupe: str,
          epoch: Optional[int] = None) -> dict:
    run = _run(connection, run_id)
    target = reply_to_of(run)
    if run is None or target is None or not target.get("trigger_ts"):
        return {"ok": False, "status": "refused", "error": "no_message",
                "message": "There is no chat message of this run to react to."}
    name = str(emoji or "").strip().strip(":")[:60]
    if not name or not all(ch.isalnum() or ch in "_-+'" for ch in name):
        return {"ok": False, "status": "refused", "error": "bad_emoji",
                "message": "An emoji is a Slack emoji name like eyes or white_check_mark."}
    epoch = current_epoch(connection, run_id) if epoch is None else int(epoch)
    got = _insert(connection, run=run, target=target, kind="agent_react",
                  payload={"op": "react", "channel": target["channel"],
                           "ts": target["trigger_ts"], "emoji": name},
                  dedupe=dedupe, epoch=epoch)
    return {"ok": True, "status": "queued", "outbox_ids": [got["outbox_id"]],
            "op_seq": [got["op_seq"]]}


# ---------------------------------------------------------------------------------------------
# The router's and the settlement's ends
# ---------------------------------------------------------------------------------------------

def bind_conversation(connection: Connection, run_id: str, target: Mapping[str, Any], *,
                      project_id: str, agent_thread_id: Optional[str],
                      trigger_id: Optional[str]) -> Optional[str]:
    """The `chat_threads` row of this conversation, pointing at its latest run."""
    thread = str(target.get("thread_ts") or target.get("conversation_key") or "")
    if not thread:
        return None
    row = connection.execute(text("""
        INSERT INTO chat_threads (id, endpoint_id, project_id, external_channel_id,
               external_thread_id, run_id, conversation_key, agent_thread_id, trigger_id,
               conversation_ref, deployment_id, last_inbound_at)
        VALUES (:i, :e, :p, :c, :t, :r, :k, :at, :tr, CAST(:cref AS JSONB), :dep, now())
        ON CONFLICT (endpoint_id, external_channel_id, external_thread_id) DO UPDATE
           SET run_id = EXCLUDED.run_id,
               conversation_key = COALESCE(EXCLUDED.conversation_key, chat_threads.conversation_key),
               agent_thread_id = COALESCE(EXCLUDED.agent_thread_id, chat_threads.agent_thread_id),
               trigger_id = COALESCE(EXCLUDED.trigger_id, chat_threads.trigger_id),
               conversation_ref = CASE WHEN EXCLUDED.conversation_ref <> '{}'::jsonb
                 THEN EXCLUDED.conversation_ref ELSE chat_threads.conversation_ref END,
               deployment_id = COALESCE(EXCLUDED.deployment_id, chat_threads.deployment_id),
               last_inbound_at = now(),
               updated_at = now()
        RETURNING id"""),
        {"i": make_id("cht"), "e": target["chat_endpoint_id"], "p": project_id,
         "c": target["channel"], "t": thread[:200], "r": run_id,
         "k": target.get("conversation_key"), "at": agent_thread_id, "tr": trigger_id,
         "cref": json.dumps(dict(target.get("conversation_ref") or {}), default=str),
         "dep": target.get("deployment_id")}).scalar()
    return str(row) if row else None


def on_admitted(connection: Connection, run_id: str, *, settings: Any = None) -> Optional[dict]:
    """THE FIRST SIGN OF LIFE (§9.2): the router admitted a conversational run — bind the
    conversation to it and post its status message ("Looking into it…"), or, with streaming on,
    react 👀 on the person's message. Written in the router's transaction; the NOTIFY at commit
    wakes the sender."""
    run = _run(connection, run_id)
    target = reply_to_of(run)
    if run is None or target is None:
        return None
    fctx = _json(run.get("finalize_ctx"), {}) or {}
    ct = bind_conversation(connection, run_id, target, project_id=str(run["project_id"]),
                           agent_thread_id=fctx.get("thread_id"),
                           trigger_id=fctx.get("trigger_id"))
    if ct and not target.get("chat_thread_id"):
        target = {**target, "chat_thread_id": ct}
        connection.execute(text(
            "UPDATE agent_runs SET finalize_ctx = jsonb_set(finalize_ctx, '{reply_to}', "
            "CAST(:t AS JSONB)) WHERE id = :r"), {"t": json.dumps(target, default=str),
                                                  "r": run_id})
    if target.get("status") is False:
        return {"bound": ct}
    epoch = current_epoch(connection, run_id)
    if target.get("stream") and streaming_enabled(settings):
        if target.get("trigger_ts"):
            return react(connection, run_id, "eyes", dedupe=f"chat:{run_id}:admitted",
                         epoch=epoch)
        return {"bound": ct}
    return say(connection, run_id, str(target.get("status") or DEFAULT_STATUS), final=False,
               kind="agent_status", dedupe=f"chat:{run_id}:admitted", epoch=epoch,
               settings=settings)


def on_run_settled(connection: Connection, run_id: str) -> Optional[dict]:
    """A conversational run ended without a final reply: its own answer (or why it could not
    finish) becomes the final reply, edited into its status message. `reply: tool` routes opt
    out (the agent answers only through the tools)."""
    from . import thread_runs

    run = _run(connection, run_id)
    target = reply_to_of(run)
    if run is None or target is None or str(target.get("reply") or "auto") == "tool":
        return None
    if str(run.get("status") or "") not in thread_runs.ENDED:
        return None
    done = connection.execute(text(
        "SELECT 1 FROM chat_outbox WHERE run_id = :r AND payload->>'final' = 'true' LIMIT 1"),
        {"r": run_id}).scalar()
    if done:
        return None
    status = str(run.get("status") or "")
    words = str(run.get("output_text") or "").strip()
    if status != "completed" or not words:
        # Never the run's internal error text: it can name hosts, paths or tools.
        words = words or ("I could not finish this. The details are in BOSS under this "
                          "agent's runs.")
    return say(connection, run_id, words, final=True, kind="agent_reply",
               dedupe=f"chat:{run_id}:settled")


# ---------------------------------------------------------------------------------------------
# The tools (a kernel ToolSet, composed beside the agent's own)
# ---------------------------------------------------------------------------------------------

def _schemas() -> list:
    from ..kernel import idempotency
    from ..kernel.types import ToolSchema

    return [
        ToolSchema(REPLY, "Answer the person in the chat conversation that started this run "
                   "(the same Slack thread or direct message). Your 'Looking into it…' message "
                   "is edited into this answer. Use final=false to show progress; the same "
                   "message is edited again by your next reply.",
                   {"type": "object", "properties": {
                       "text": {"type": "string", "description": "What to say (Slack mrkdwn)."},
                       "final": {"type": "boolean", "description": "true (default): this is "
                                 "the answer; false: progress, edited again later."}},
                    "required": ["text"], "additionalProperties": False},
                   kind="write", permission=None, idempotency=idempotency.KEYED),
        ToolSchema(STATUS, "Show a short status line in the conversation (edits your status "
                   "message in place), e.g. 'Reading last night's job logs…'.",
                   {"type": "object", "properties": {"text": {"type": "string"}},
                    "required": ["text"], "additionalProperties": False},
                   kind="write", permission=None, idempotency=idempotency.KEYED),
        ToolSchema(REACT, "React to the person's message with a Slack emoji name (eyes, "
                   "white_check_mark, …).",
                   {"type": "object", "properties": {"emoji": {"type": "string"}},
                    "required": ["emoji"], "additionalProperties": False},
                   kind="write", permission=None, idempotency=idempotency.KEYED),
    ]


class ChatToolSet:
    """chat_reply / chat_status / chat_react for one conversational run. The target, the token
    and the endpoint are never the model's to choose: every call writes an outbox row for THIS
    run's conversation under THIS executor's fence."""

    def __init__(self, connection: Connection, settings: Any, run: Mapping[str, Any]):
        self.connection, self.settings, self.run_id = connection, settings, str(run["id"])
        self._schemas = _schemas()

    def schemas(self, *, step: int, state: Any) -> list:
        return list(self._schemas)

    def invoke(self, call: Any, *, state: Any, approved: Optional[dict] = None) -> Any:
        from ..kernel.toolset import failed, result_from
        from ..kernel.types import ToolResult

        args = dict(call.arguments or {})
        ident = str(call.id or "") or hashlib.sha256(
            json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:16]
        dedupe = f"chat:{self.run_id}:{int(getattr(state, 'step', 0) or 0)}:{call.name}:{ident}"
        try:
            with self.connection.begin_nested():
                if call.name == REPLY:
                    out = say(self.connection, self.run_id, str(args.get("text") or ""),
                              final=args.get("final") is not False, kind="agent_reply",
                              dedupe=dedupe, settings=self.settings)
                elif call.name == STATUS:
                    out = say(self.connection, self.run_id, str(args.get("text") or "")[:300],
                              final=False, kind="agent_status", dedupe=dedupe,
                              settings=self.settings)
                elif call.name == REACT:
                    out = react(self.connection, self.run_id, str(args.get("emoji") or ""),
                                dedupe=dedupe)
                else:
                    from ..kernel.toolset import unknown

                    return unknown(call)
        except ReplyFenced as stale:
            return ToolResult(call.id, call.name, "refused", {
                "ok": False, "status": "refused", "error": "fenced", "message": str(stale)})
        except Exception as exc:  # noqa: BLE001 — a lease loss propagates; anything else is read
            from .jobs import LeaseNotFoundError

            if isinstance(exc, LeaseNotFoundError):
                raise
            return failed(call, exc)
        if not out.get("ok"):
            return ToolResult(call.id, call.name, "refused", out)
        _kick()
        return result_from(call, out)


def with_chat_tools(tools: Any, connection: Connection, settings: Any,
                    run: Mapping[str, Any]) -> Any:
    """The run's toolset, plus the reply tools when a conversation started it."""
    if reply_to_of(run) is None:
        return tools
    from ..kernel.toolset import Composite

    return Composite(tools, ChatToolSet(connection, settings, run))


# ---------------------------------------------------------------------------------------------
# The sender's side (notify.deliver_chat_due → chat_channels.deliver)
# ---------------------------------------------------------------------------------------------

#: Teams Bot Framework sliding rate windows per conversation and per app/tenant envelope (LLD §7.2).
TEAMS_CONVERSATION_WINDOWS: tuple[tuple[int, float], ...] = (
    (7, 1.0),
    (8, 2.0),
    (60, 30.0),
    (1800, 3600.0),
)
TEAMS_ENVELOPE_WINDOWS: tuple[tuple[int, float], ...] = ((50, 1.0),)


class _Buckets:
    """Per (endpoint, channel) token buckets + Teams multi-window rate shapers, in memory (§7.2)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, list[float]] = {}
        self._windows: dict[str, list[float]] = {}
        self._blocked_until: dict[str, float] = {}

    def wait_for(self, key: str, now: Optional[float] = None) -> float:
        """0 when a send may go now (a token is taken), else the seconds to wait."""
        now = time.monotonic() if now is None else now
        with self._lock:
            tokens, at, blocked = self._state.get(key, [CHANNEL_BURST, now, 0.0])
            if blocked > now:
                return blocked - now
            tokens = min(CHANNEL_BURST, tokens + (now - at) * CHANNEL_RATE_PER_SECOND)
            if tokens >= 1.0:
                self._state[key] = [tokens - 1.0, now, 0.0]
                return 0.0
            self._state[key] = [tokens, now, 0.0]
            return (1.0 - tokens) / CHANNEL_RATE_PER_SECOND

    def wait_for_teams(self, conv_key: str, app_key: str, now: Optional[float] = None) -> float:
        """Enforce Teams per-conversation (7/1s, 8/2s, 60/30s, 1800/h) and app (50/1s) limits."""
        now = time.monotonic() if now is None else now
        with self._lock:
            for k in (conv_key, app_key):
                if k and self._blocked_until.get(k, 0.0) > now:
                    return self._blocked_until[k] - now
            wait = 0.0
            for k, rules in ((conv_key, TEAMS_CONVERSATION_WINDOWS), (app_key, TEAMS_ENVELOPE_WINDOWS)):
                if not k:
                    continue
                max_win = max(w for _, w in rules)
                stamps = [t for t in self._windows.get(k, []) if now - t < max_win]
                self._windows[k] = stamps
                for limit, window in rules:
                    recent = [t for t in stamps if now - t < window]
                    if len(recent) >= limit:
                        oldest = recent[-limit]
                        wait = max(wait, max(0.05, (oldest + window) - now))
            if wait > 0:
                return wait
            for k in (conv_key, app_key):
                if k:
                    self._windows.setdefault(k, []).append(now)
            return 0.0

    def block(self, key: str, seconds: float, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            tokens, at, blocked = self._state.get(key, [0.0, now, 0.0])
            self._state[key] = [0.0, now, max(blocked, now + max(0.0, seconds))]
            self._blocked_until[key] = max(self._blocked_until.get(key, 0.0), now + max(0.0, seconds))

    def reset(self) -> None:
        with self._lock:
            self._state.clear()
            self._windows.clear()
            self._blocked_until.clear()


BUCKETS = _Buckets()
#: Ops that do not spend the channel's message rate (reactions, the status line).
UNMETERED_OPS = ("react", "set_status")


def teams_app_key(row: Mapping[str, Any], payload: Mapping[str, Any]) -> Optional[str]:
    cfg = payload.get("teams_config") if isinstance(payload.get("teams_config"), dict) else {}
    app_id = cfg.get("app_id")
    if app_id:
        return f"teams_app:{app_id}"
    return f"teams_ep:{row['endpoint_id']}" if row.get("endpoint_id") else None


def bucket_key(row: Mapping[str, Any], payload: Mapping[str, Any]) -> Optional[str]:
    """The channel bucket a row spends from: EVERY row that names a channel (or a DM's user) —
    agents' replies, decision cards, link prompts — so together they keep Slack's ≈1/s per
    channel. Reactions and the status line are other rate tiers.

    FOLLOW-UP (noted in review): the buckets are per API process, so N replicas may send up to
    N/s into one channel; Slack's 429 + Retry-After (honoured below, and it blocks the channel
    for this process) is what bounds that today. A shared (Postgres) bucket is the follow-up."""
    if str(payload.get("op") or "") in UNMETERED_OPS:
        return None
    channel = payload.get("channel") or payload.get("user")
    if not channel:
        return None
    return str(row.get("channel_key") or f"{row['endpoint_id']}:{channel}")


def channel_of(row: Mapping[str, Any], payload: Mapping[str, Any]) -> Optional[str]:
    """The channel a 429 blocks, whatever the op (a reaction's 429 slows its channel too)."""
    channel = payload.get("channel") or payload.get("user")
    return str(row.get("channel_key") or f"{row['endpoint_id']}:{channel}") if channel else None


def prepare(connection: Connection, row: Mapping[str, Any], payload: dict) -> Optional[dict]:
    """Resolve what a run's row refers to at send time. Returns the payload to send, or
    {"skip": receipt} when the row has nothing left to do; raises NotYet when it must wait."""
    op = str(payload.get("op") or "")
    ref = payload.get("ref")
    if ref and op in ("update", "stream_append", "stream_stop"):
        if op != "stream_stop":
            later = connection.execute(text("""
                SELECT id FROM chat_outbox WHERE run_id = :r AND op_seq > :s
                   AND status IN ('pending', 'failed') AND payload->>'ref' = :ref
                   AND payload->>'op' = :op ORDER BY op_seq LIMIT 1"""),
                {"r": row["run_id"], "s": int(row.get("op_seq") or 0), "ref": str(ref),
                 "op": op}).scalar() if op == "update" else None
            if later:
                return {"skip": {"superseded_by": later}}
        target = connection.execute(text(
            "SELECT status, receipt FROM chat_outbox WHERE id = :i"), {"i": ref}).mappings().first()
        if target is not None and target["status"] in ("pending", "failed"):
            raise NotYet(f"waits for {ref}")
        receipt = _json((target or {}).get("receipt"), {}) or {}
        ts = receipt.get("ts") or receipt.get("thread")
        if not ts:
            if op == "update":
                # The message it edits never went out: post the words instead.
                payload = {**payload, "op": "post"}
                payload.pop("ref", None)
                return payload
            return {"skip": {"reason": "the stream never started"}}
        if op == "update":
            return {**payload, "thread": ts}
        if op == "stream_append":
            later = [dict(r) for r in connection.execute(text("""
                SELECT id, payload FROM chat_outbox WHERE run_id = :r AND op_seq > :s
                   AND status IN ('pending', 'failed') AND payload->>'ref' = :ref
                   AND payload->>'op' = 'stream_append' ORDER BY op_seq"""),
                {"r": row["run_id"], "s": int(row.get("op_seq") or 0),
                 "ref": str(ref)}).mappings().all()]
            if later:
                # Coalesce the appends that queued up behind this one into one call (§9.2).
                extra = "".join(str((_json(r["payload"], {}) or {}).get("text") or "")
                                for r in later)
                connection.execute(text(
                    "UPDATE chat_outbox SET status = 'sent', sent_at = now(), "
                    "receipt = CAST(:rc AS JSONB) WHERE id = ANY(:ids)"),
                    {"rc": json.dumps({"coalesced_into": row["id"]}),
                     "ids": [r["id"] for r in later]})
                payload = {**payload, "text": str(payload.get("text") or "") + extra}
        return {**payload, "ts": ts}
    return payload


def after_send(connection: Connection, row: Mapping[str, Any], payload: Mapping[str, Any],
               receipt: Mapping[str, Any]) -> None:
    """Receipts back on the conversation (for edits and loop checks), and our bot id learned
    from the first message we post (loop prevention, layer 1)."""
    ts = (receipt or {}).get("ts")
    if row.get("chat_thread_id") and ts:
        connection.execute(text("""
            UPDATE chat_threads SET bot_message_refs = (
                   SELECT COALESCE(jsonb_agg(x), '[]'::jsonb) FROM (
                     SELECT x FROM jsonb_array_elements(bot_message_refs || CAST(:ref AS JSONB)) x
                     ORDER BY 1 DESC LIMIT 50) s),
                   updated_at = now()
             WHERE id = :t AND NOT bot_message_refs @> CAST(:ref AS JSONB)"""),
            {"t": row["chat_thread_id"], "ref": json.dumps([str(ts)])})
    bot_id = (receipt or {}).get("bot_id")
    if bot_id:
        connection.execute(text("""
            UPDATE intake_connections
               SET config = config || jsonb_build_object('bot_id', CAST(:b AS TEXT))
             WHERE chat_endpoint_id = :e AND COALESCE(config->>'bot_id', '') = ''"""),
            {"b": str(bot_id), "e": row["endpoint_id"]})


# ---------------------------------------------------------------------------------------------
# The sender thread: woken by NOTIFY at commit
# ---------------------------------------------------------------------------------------------

_STARTED = threading.Event()
_STOP = threading.Event()
_WAKE = threading.Event()
SERVICE_KEY = "chat_outbox_sender"
TICK_SECONDS = 1.0


def _kick() -> None:
    """Wake this process's sender now (the NOTIFY does it for other processes, at commit)."""
    _WAKE.set()


class _AnyInterest:
    def matches(self, note: dict) -> bool:
        return True


def drain(engine: Any, settings: Any, *, rounds: int = 20) -> dict:
    """Send what is due until nothing more is (a run's rows go one after another)."""
    from ..db import background
    from .notify import deliver_chat_due

    with background():
        total = {"sent": 0, "failed": 0, "dead": 0, "waiting": 0}
        for _ in range(rounds):
            got = deliver_chat_due(engine, settings)
            for key in total:
                total[key] += int(got.get(key) or 0)
            if not got.get("sent") and not got.get("skipped"):
                break
        return total


def start(engine: Any, settings: Any) -> bool:
    """The outbox sender (system_threads.start_all): every API process runs one; rows are
    claimed SKIP LOCKED. Woken by `boss_chat_outbox` NOTIFY at commit, with a 1 s safety tick."""
    if _STARTED.is_set():
        return False
    _STARTED.set()
    _STOP.clear()

    def loop() -> None:
        from . import system_services

        try:
            from .event_stream import Hub, PgListener, libpq_dsn

            hub = Hub()
            hub.listen(_AnyInterest(), lambda _note: _WAKE.set())
            PgListener(libpq_dsn(engine), hub, channel=NOTIFY_CHANNEL, poll=0.5).start()
        except Exception:  # noqa: BLE001 — the tick carries the sender without NOTIFY
            log.warning("chat outbox: NOTIFY listener not started; ticking only", exc_info=True)
        try:
            system_services.register(engine, SERVICE_KEY, started=True)
        except Exception:  # noqa: BLE001
            pass
        while not _STOP.is_set():
            _WAKE.wait(TICK_SECONDS)
            _WAKE.clear()
            try:
                from ..db import background

                with background():
                    drain(engine, settings)
            except Exception:  # noqa: BLE001 — the next tick tries again
                log.warning("chat outbox send failed", exc_info=True)

    threading.Thread(target=loop, daemon=True, name="boss-chat-outbox").start()
    return True


def stop() -> None:
    _STOP.set()
    _STARTED.clear()
    _WAKE.set()


__all__ = ["BUCKETS", "ChatToolSet", "DEFAULT_STATUS", "FENCE_EPOCH", "KINDS", "NOTIFY_CHANNEL",
           "NotYet", "REACT", "REPLY", "ReplyFenced", "STATUS", "TOOL_NAMES", "after_send",
           "bind_conversation", "bucket_key", "channel_of", "inert", "current_epoch", "drain", "on_admitted",
           "on_run_settled", "prepare", "react", "reply_to_of", "say", "start", "stop",
           "streaming_enabled", "with_chat_tools"]
