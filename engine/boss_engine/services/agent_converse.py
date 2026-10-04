"""Talking with agents: a 1:1 talk, "plan it with <agent>", and the room (spec §0.2 A).

Three shapes of one thing. A conversation is a person and 1-5 seated agents; every agent reply
is a real model call made with that agent's composed prompt (its persona is the prompt, §5.2),
governed like every other call (budget reservation, receipt, llm_calls row), and recorded as an
act (§4.3) with the conversation as its thread. Nothing here starts work: a plan ends in a
StartProposal card, and only the operator's press on it starts a run, through the same start
door the composer uses (`start_from_proposal`, given the API's start function).

    talk  1 seat. Answers in character, can cite its own recent work as [act:<id>].
    plan  1 seat. Scopes the work with the operator, then `propose_start` turns the talk into a
          reviewable card: workflow, directive, inputs, acceptance, who runs each step.
    room  2-5 seats. Each addressed seat speaks once, in seat order, and sees the round so far;
          rounds=2 lets them answer each other. `summarize_room` has the lead seat write the
          summary, decisions (with who was for and against) and next actions, any of which can
          carry a start card or open a decision.

STREAMING. The adapters BOSS has return a whole answer per call, so a seat's reply is revealed
in word groups as soon as it exists (`REVEAL_SECONDS` in total), after a `turn_start` that the
console draws as the seat thinking. An adapter that grows a `stream_text_response` streams for
real with no change here.

WHAT THIS MODULE LEANS ON: agent_persona.compose_agent_prompt builds the system prompt (the
same compiler as the dock, work and panel prompts); agent_acts.open_act / close_act write the
acts, so every reply is in the agent's Work feed, lineage and presence; agent_identity.resolve
maps titles and aliases to role keys.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional

from sqlalchemy import text

from . import agent_acts, agent_identity, agent_persona
from .agent_world_types import AI_DISCLOSURE, ROOM_PRESETS
from .core import emit_event, make_id

log = logging.getLogger("boss.agent_converse")

KINDS = ("talk", "plan", "room")
SEATS = {"talk": (1, 1), "plan": (1, 1), "room": (2, 5)}
MAX_MESSAGE_CHARS = 8000
#: The transcript an agent is shown: the newest turns, each trimmed. A room of five over a long
#: afternoon must not become an unaffordable prompt.
TRANSCRIPT_TURNS = 30
TRANSCRIPT_TURN_CHARS = 1500
#: Seconds over which a whole reply is revealed once the model has answered. Tests set 0.
REVEAL_SECONDS = 1.1
#: What one agent reply reserves against the budget before it is made; settled to the actual.
REPLY_RESERVE_MICROS = 60_000
#: A summary or a proposal reads the whole transcript and writes structure: more.
SYNTHESIS_RESERVE_MICROS = 150_000
BUDGET_CATEGORY = "agent_conversation"
#: THE FLOOR. One stream speaks in a conversation at a time. The request that is allowed to
#: speak claims the floor in the same transaction that checked it was free (so two sends a
#: moment apart cannot both pass), renews the claim as each seat begins, and gives it back when
#: it ends, however it ends. A claim not renewed for this long belongs to a stream that died
#: unseen (a killed worker): it lapses, and the next turn sweeps what that stream left.
STALE_STREAMING_SECONDS = 600
#: What a turn cut off mid-answer says (the operator pressed Stop or closed the room).
STOPPED = {"kind": "stopped", "headline": "Stopped.", "action": "Ask again when ready.",
           "retry": True}

_CITE = re.compile(r"\[act:(act_[A-Za-z0-9_]+)\]")
_MENTION = re.compile(r"@([a-z0-9_]+)")
NEEDS_MODEL_MESSAGE = "Needs a model. Set one in Settings."


class ConverseError(Exception):
    """A refusal the API turns into an HTTP status with a code the console can act on."""

    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}


def _invalid(message: str) -> ConverseError:
    return ConverseError(422, "invalid", message)


# ── Seams (patched in tests; swapped at integration) ─────────────────────────────────────────


def _settings(settings: Any = None) -> Any:
    if settings is not None:
        return settings
    from ..config import get_settings

    return get_settings()


def model_ready(connection, settings: Any, project_id: str) -> bool:
    """Whether this project can ask a model anything — the check a run's checklist makes."""
    from .workflow_drafting import model_connected

    try:
        return model_connected(connection, settings, project_id)
    except Exception:  # noqa: BLE001 — an unreadable setup is not a connected model
        return False


def make_adapter(connection, settings: Any, project_id: str):
    """The project's chat model: the same resolution the console chat, meetings and panels use."""
    from .claude_cli_adapter import make_agent_chat_adapter
    from .setup_service import chat_engine_for

    resolved = settings
    try:
        chosen = chat_engine_for(connection, settings, project_id)
    except Exception:  # noqa: BLE001 — no project choice means the install default
        chosen = ""
    if chosen:
        resolved = settings.model_copy(update={"chat_llm_engine": chosen})
    return make_agent_chat_adapter(resolved)


def _engine_name(connection, settings: Any, project_id: str) -> str:
    """The name telemetry and provider-capacity records know this engine by."""
    from .setup_service import chat_engine_for

    try:
        return chat_engine_for(connection, settings, project_id) or str(
            getattr(settings, "chat_llm_engine", "") or getattr(settings, "llm_engine", "")
            or "native")
    except Exception:  # noqa: BLE001
        return "native"


def _resolve(key_or_title: str) -> Optional[str]:
    """One namespace (agent_identity.resolve): a role key, seat title, alias or stage role →
    the role_key. A custom agent's own key is not in the shipped table and passes through."""
    value = str(key_or_title or "").strip()
    if not value:
        return None
    return agent_identity.resolve(value) or value.lower()


def _compose(role: dict, *, mode: str, act: Optional[dict], memory: list[dict]) -> str:
    """The agent's own prompt, L0..L5 (agent_persona.compose_agent_prompt): the same persona
    compiler the dock, the work prompt and every panel seat use."""
    blocks = agent_persona.compose_agent_prompt(role, mode=mode, act=act, memory=memory)
    composed = "\n\n".join(str(b.get("text") or "") for b in blocks if b.get("text"))
    # The disclosure is fixed whoever composed the rest (§0.1.3): never trust a composer to
    # have kept it.
    return composed if AI_DISCLOSURE in composed else f"{composed}\n\n{AI_DISCLOSURE}"


# ── Reading ──────────────────────────────────────────────────────────────────────────────────


def _jsonish(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def _roles(connection, project_id: str, keys: Optional[list[str]] = None) -> dict[str, dict]:
    clause, params = "", {"p": project_id}
    if keys is not None:
        if not keys:
            return {}
        clause = " AND role_key = ANY(:keys)"
        params["keys"] = list(keys)
    rows = connection.execute(
        text("SELECT role_key, display_name, spec, agent_id, fingerprint, identity_version, "
             f"lifecycle FROM roles WHERE project_id = :p{clause}"),
        params,
    ).mappings().all()
    out = {}
    for r in rows:
        role = dict(r)
        role["spec"] = _jsonish(role.get("spec"), {})
        role["project_id"] = project_id
        out[role["role_key"]] = role
    return out


def _seat_info(role: dict) -> dict:
    persona = (role.get("spec") or {}).get("persona") or {}
    tone = ((role.get("spec") or {}).get("comms_contract") or {}).get("tone")
    return {
        "role_key": role["role_key"],
        "name": role.get("display_name") or role["role_key"],
        "callsign": persona.get("callsign"),
        "tagline": persona.get("tagline") or (str(tone).split(",")[0].strip().capitalize()
                                              if tone else None),
        "agent_id": role.get("agent_id"),
        "lifecycle": role.get("lifecycle") or "active",
    }


def _speaker(role: Optional[dict], role_key: Optional[str]) -> str:
    if not role:
        return str(role_key or "An agent")
    persona = (role.get("spec") or {}).get("persona") or {}
    title = role.get("display_name") or role["role_key"]
    return f"{persona['callsign']} ({title})" if persona.get("callsign") else title


def _turn_dict(row: Any) -> dict:
    turn = dict(row)
    for key, default in (("addressed_to", []), ("payload", {}), ("cited_acts", [])):
        turn[key] = _jsonish(turn.get(key), default)
    turn["cost_usd"] = float(turn.get("cost_usd") or 0)
    turn["created_at"] = _iso(turn.get("created_at"))
    return turn


def _turns(connection, conversation_id: str) -> list[dict]:
    rows = connection.execute(
        text("SELECT * FROM agent_conversation_turns WHERE conversation_id = :c ORDER BY seq"),
        {"c": conversation_id},
    ).mappings().all()
    return [_turn_dict(r) for r in rows]


def _row(connection, project_id: str, conversation_id: str, *, lock: bool = False) -> dict:
    row = connection.execute(
        text("SELECT * FROM agent_conversations WHERE conversation_id = :c AND project_id = :p"
             + (" FOR UPDATE" if lock else "")),
        {"c": conversation_id, "p": project_id},
    ).mappings().first()
    if not row:
        # Another project's id reads exactly like a missing one: 404, never 403.
        raise ConverseError(404, "not_found", "No such conversation.")
    conv = dict(row)
    conv["seats"] = _jsonish(conv.get("seats"), [])
    conv["context"] = _jsonish(conv.get("context"), {})
    conv["summary"] = _jsonish(conv.get("summary"), None)
    return conv


def _shape(connection, conv: dict, *, with_turns: bool) -> dict:
    roles = _roles(connection, conv["project_id"], conv["seats"])
    count = connection.execute(
        text("SELECT count(*) FROM agent_conversation_turns WHERE conversation_id = :c"),
        {"c": conv["conversation_id"]},
    ).scalar() or 0
    out = {
        "conversation_id": conv["conversation_id"], "project_id": conv["project_id"],
        "kind": conv["kind"], "title": conv["title"], "actor": conv["actor"],
        "seats": conv["seats"], "lead_role_key": conv.get("lead_role_key") or conv["seats"][0],
        "preset": conv.get("preset"), "status": conv["status"], "context": conv["context"],
        "summary": conv.get("summary"), "agent_thread_id": conv.get("agent_thread_id"),
        "root_act_id": conv.get("root_act_id"), "started_run_id": conv.get("started_run_id"),
        "turn_count": int(count), "created_at": _iso(conv.get("created_at")),
        "updated_at": _iso(conv.get("updated_at")),
        # Not in the frozen Conversation shape: who each seat is, so a surface can name a seat
        # before the identity resolver has loaded (and the room can show who left, if retired).
        "seat_info": [_seat_info(roles[k]) if k in roles else
                      {"role_key": k, "name": k, "lifecycle": "retired"} for k in conv["seats"]],
    }
    if with_turns:
        out["turns"] = _turns(connection, conv["conversation_id"])
    return out


def get_conversation(connection, project_id: str, conversation_id: str) -> dict:
    return _shape(connection, _row(connection, project_id, conversation_id), with_turns=True)


def list_conversations(connection, project_id: str, *, actor: Optional[str] = None,
                       kind: Optional[str] = None, limit: int = 20) -> list[dict]:
    clauses, params = ["project_id = :p"], {"p": project_id, "lim": max(1, min(int(limit), 100))}
    if actor:
        clauses.append("actor = :a")
        params["a"] = actor
    if kind:
        if kind not in KINDS:
            raise _invalid(f"kind must be one of {', '.join(KINDS)}")
        clauses.append("kind = :k")
        params["k"] = kind
    rows = connection.execute(
        text(f"SELECT * FROM agent_conversations WHERE {' AND '.join(clauses)} "
             "ORDER BY updated_at DESC LIMIT :lim"),
        params,
    ).mappings().all()
    out = []
    for r in rows:
        conv = dict(r)
        conv["seats"] = _jsonish(conv.get("seats"), [])
        conv["context"] = _jsonish(conv.get("context"), {})
        conv["summary"] = None
        out.append(_shape(connection, conv, with_turns=False))
    return out


def presets(connection, project_id: str) -> dict:
    """The room presets, each saying which of its seats this project lacks, and the agents a
    room can seat (active ones: a paused or retired agent cannot be seated)."""
    roles = _roles(connection, project_id)
    items = [{**p, "missing": [s for s in p["seats"] if s not in roles]} for p in ROOM_PRESETS]
    agents = sorted((_seat_info(r) for r in roles.values()
                     if (r.get("lifecycle") or "active") == "active"),
                    key=lambda a: a["name"].lower())
    return {"items": items, "agents": agents}


# ── Creating ─────────────────────────────────────────────────────────────────────────────────


def _clean_context(connection, project_id: str, context: Optional[dict]) -> dict:
    raw = context if isinstance(context, dict) else {}
    out: dict[str, Any] = {"from": str(raw.get("from") or "chip")[:24]}
    act_id = str(raw.get("act_id") or raw.get("actId") or "").strip()
    if act_id and connection.execute(
            text("SELECT 1 FROM agent_acts WHERE act_id = :a AND project_id = :p"),
            {"a": act_id, "p": project_id}).first():
        out["act_id"] = act_id
    run_id = str(raw.get("workflow_run_id") or raw.get("workflowRunId") or "").strip()
    if run_id and connection.execute(
            text("SELECT 1 FROM workflow_runs WHERE id = :r AND project_id = :p"),
            {"r": run_id, "p": project_id}).first():
        out["workflow_run_id"] = run_id
        step = str(raw.get("step_id") or raw.get("stepId") or "").strip()
        if step:
            out["step_id"] = step[:120]
    return out


def create_conversation(connection, *, project_id: str, actor: str, kind: str,
                        seats: list[str], lead_role_key: Optional[str] = None,
                        preset: Optional[str] = None, title: str = "",
                        context: Optional[dict] = None) -> dict:
    """No model call. 422 on a bad seat count, or an agent this project lacks or has retired."""
    if kind not in KINDS:
        raise _invalid(f"kind must be one of {', '.join(KINDS)}")
    preset_row = next((p for p in ROOM_PRESETS if p["key"] == preset), None) if preset else None
    if preset and not preset_row:
        raise _invalid(f"no room preset '{preset}'")
    wanted = list(seats or []) or (list(preset_row["seats"]) if preset_row else [])
    resolved: list[str] = []
    for seat in wanted:
        key = _resolve(str(seat))
        if key and key not in resolved:
            resolved.append(key)
    low, high = SEATS[kind]
    if not low <= len(resolved) <= high:
        raise _invalid(f"a {kind} seats exactly 1 agent" if low == high
                       else f"a room seats {low} to {high} agents")
    roles = _roles(connection, project_id, resolved)
    missing = [k for k in resolved if k not in roles]
    if missing:
        raise _invalid(f"this project has no agent {', '.join(missing)}")
    unavailable = [k for k in resolved if (roles[k].get("lifecycle") or "active") != "active"]
    if unavailable:
        name = roles[unavailable[0]].get("display_name") or unavailable[0]
        raise _invalid(f"{name} is {roles[unavailable[0]].get('lifecycle')}")
    lead = _resolve(lead_role_key) if lead_role_key else resolved[0]
    if lead not in resolved:
        raise _invalid("the lead must be one of the seats")
    names = [roles[k].get("display_name") or k for k in resolved]
    default_title = {"talk": f"Talk with {names[0]}", "plan": f"Plan with {names[0]}",
                     "room": preset_row["label"] if preset_row else " · ".join(names)}[kind]
    conversation_id = make_id("cnv")
    connection.execute(
        text("""
            INSERT INTO agent_conversations (conversation_id, project_id, kind, title, actor,
              seats, lead_role_key, preset, context)
            VALUES (:c, :p, :k, :t, :a, CAST(:s AS JSONB), :lead, :preset, CAST(:ctx AS JSONB))
        """),
        {"c": conversation_id, "p": project_id, "k": kind,
         "t": (str(title or "").strip()[:120] or default_title), "a": actor,
         "s": json.dumps(resolved), "lead": lead, "preset": preset_row["key"] if preset_row
         else None, "ctx": json.dumps(_clean_context(connection, project_id, context))},
    )
    emit_event(connection, project_id, "conversation.opened", actor, "conversation",
               conversation_id, {"kind": kind, "seats": resolved, "preset": preset})
    return get_conversation(connection, project_id, conversation_id)


def _set_status(connection, project_id: str, conversation_id: str, status: str, actor: str
                ) -> dict:
    if _row(connection, project_id, conversation_id, lock=True)["actor"] != actor:
        raise ConverseError(404, "not_found", "No such conversation.")
    connection.execute(
        text("UPDATE agent_conversations SET status = :s, updated_at = now() "
             "WHERE conversation_id = :c"),
        {"s": status, "c": conversation_id},
    )
    emit_event(connection, project_id, f"conversation.{status}", actor, "conversation",
               conversation_id, {})
    return get_conversation(connection, project_id, conversation_id)


def close_conversation(connection, *, project_id: str, conversation_id: str, actor: str) -> dict:
    return _set_status(connection, project_id, conversation_id, "closed", actor)


# ── Planning a turn (eager: refusals happen before the stream opens) ─────────────────────────


@dataclass
class _Speaker:
    role_key: str
    how: str                       # 'asked_by_person' | 'seated'
    reply_to_turn_id: Optional[str] = None
    reply_to_role: Optional[str] = None
    round: int = 1


@dataclass
class _Plan:
    project_id: str
    conversation_id: str
    actor: str
    mode: str                      # 'reply' | 'summary' | 'proposal'
    speakers: list[_Speaker]
    person_turn_id: Optional[str]
    rounds: int
    reserve_micros: int
    #: This stream's hold on the floor (see STALE_STREAMING_SECONDS).
    claim: Optional[str] = None


def _guard(connection, settings: Any, project_id: str, conv: dict, n_calls: int,
           per_call: int) -> str:
    """Refuse, or claim the floor and return the claim. Call with the conversation row locked
    (`_row(..., lock=True)`) and in the transaction that records what the stream is for."""
    if conv["status"] == "closed":
        raise ConverseError(409, "closed", "This conversation is closed.")
    if not model_ready(connection, settings, project_id):
        raise ConverseError(409, "needs_model", NEEDS_MODEL_MESSAGE)
    held = connection.execute(
        text("SELECT streaming_until > now() FROM agent_conversations "
             "WHERE conversation_id = :c"), {"c": conv["conversation_id"]}).scalar()
    if held:
        raise ConverseError(409, "busy", "An agent is still answering.")
    _sweep(connection, settings, project_id, conv["conversation_id"])
    from .budget import BudgetGovernor

    quote = BudgetGovernor(settings).quote(connection, project_id, BUDGET_CATEGORY,
                                           n_calls * per_call, "agent conversation")
    if not quote.get("allowed", True):
        raise ConverseError(409, "budget", "The budget refused this conversation.",
                            href="/?area=budget")
    claim = make_id("flr")
    connection.execute(
        text("UPDATE agent_conversations SET streaming_claim = :t, "
             "streaming_until = now() + make_interval(secs => :s) WHERE conversation_id = :c"),
        {"t": claim, "s": STALE_STREAMING_SECONDS, "c": conv["conversation_id"]})
    return claim


def _settle_cut_off(connection, settings: Any, project_id: str, turn_id: str,
                    act_id: Optional[str], reservation_id: Optional[str], failure: dict) -> None:
    """A turn that will never finish: say why, give back its budget, close its act."""
    from .budget import BudgetGovernor

    connection.execute(
        text("UPDATE agent_conversation_turns SET status = 'error', kind = 'error', "
             "payload = CAST(:p AS JSONB) WHERE turn_id = :t AND status = 'streaming'"),
        {"p": json.dumps(failure), "t": turn_id})
    if reservation_id:
        BudgetGovernor(settings).release(connection, project_id, reservation_id,
                                         "conversation turn did not finish")
    if act_id:
        agent_acts.close_act(connection, act_id, status="error")


def _sweep(connection, settings: Any, project_id: str, conversation_id: str) -> None:
    """With the floor free, a turn still 'streaming' belongs to a stream that died unseen."""
    rows = connection.execute(
        text("SELECT turn_id, act_id, payload FROM agent_conversation_turns "
             "WHERE conversation_id = :c AND status = 'streaming'"),
        {"c": conversation_id}).mappings().all()
    for r in rows:
        held = _jsonish(r["payload"], {}).get("reservation_id")
        _settle_cut_off(connection, settings, project_id, r["turn_id"], r["act_id"], held,
                        STOPPED)


def _hold_floor(engine, plan: "_Plan") -> bool:
    """Renew this stream's claim. False when it lost the floor (it lapsed and was retaken)."""
    from ..db import begin

    if not plan.claim:
        return True
    with begin(engine) as c:
        return c.execute(
            text("UPDATE agent_conversations SET streaming_until = now() + "
                 "make_interval(secs => :s) WHERE conversation_id = :c AND streaming_claim = :t"),
            {"s": STALE_STREAMING_SECONDS, "c": plan.conversation_id, "t": plan.claim},
        ).rowcount == 1


def _yield_floor(connection, plan: "_Plan") -> None:
    if plan.claim:
        connection.execute(
            text("UPDATE agent_conversations SET streaming_claim = NULL, streaming_until = NULL "
                 "WHERE conversation_id = :c AND streaming_claim = :t"),
            {"c": plan.conversation_id, "t": plan.claim})


def _next_seq(connection, conversation_id: str) -> int:
    return int(connection.execute(
        text("SELECT COALESCE(MAX(seq), 0) + 1 FROM agent_conversation_turns "
             "WHERE conversation_id = :c"), {"c": conversation_id}).scalar() or 1)


def _insert_turn(connection, conversation_id: str, **fields: Any) -> dict:
    turn_id = make_id("trn")
    seq = _next_seq(connection, conversation_id)
    connection.execute(
        text("""
            INSERT INTO agent_conversation_turns (turn_id, conversation_id, seq, speaker_kind,
              role_key, actor, addressed_to, reply_to_turn_id, kind, content, payload, status,
              act_id, fingerprint)
            VALUES (:t, :c, :seq, :sk, :rk, :actor, CAST(:to AS JSONB), :reply, :kind, :content,
                    CAST(:payload AS JSONB), :status, :act, :fp)
        """),
        {"t": turn_id, "c": conversation_id, "seq": seq,
         "sk": fields.get("speaker_kind", "agent"), "rk": fields.get("role_key"),
         "actor": fields.get("actor"), "to": json.dumps(fields.get("addressed_to") or []),
         "reply": fields.get("reply_to_turn_id"), "kind": fields.get("kind", "message"),
         "content": fields.get("content", ""), "payload": json.dumps(fields.get("payload") or {}),
         "status": fields.get("status", "complete"), "act": fields.get("act_id"),
         "fp": fields.get("fingerprint")},
    )
    connection.execute(
        text("UPDATE agent_conversations SET updated_at = now() WHERE conversation_id = :c"),
        {"c": conversation_id})
    return {"turn_id": turn_id, "seq": seq}


def _addressed(conv: dict, message: str, to: Optional[list[str]]) -> list[str]:
    """Who this message is for: `to`, else every @mention that names a seat, else everyone."""
    named: list[str] = []
    for raw in list(to or []) + _MENTION.findall(message.lower()):
        key = _resolve(raw)
        if key in conv["seats"] and key not in named:
            named.append(key)
    return named


def take_turn(engine, *, project_id: str, conversation_id: str, actor: str, message: str,
              to: Optional[list[str]] = None, rounds: int = 1, settings: Any = None
              ) -> Iterator[dict]:
    """Record the person's turn, then stream each addressed seat's reply in seat order.

    Refusals (closed, busy, no model, budget, bad input) raise ConverseError HERE, before any
    event: this returns the stream only once the turn is known to be allowed.
    """
    from ..db import begin

    settings = _settings(settings)
    said = str(message or "").strip()
    if len(said) > MAX_MESSAGE_CHARS:
        raise _invalid(f"a message is at most {MAX_MESSAGE_CHARS} characters")
    rounds = 2 if int(rounds or 1) >= 2 else 1
    with begin(engine) as c:
        conv = _row(c, project_id, conversation_id, lock=True)
        if conv["actor"] != actor:
            raise ConverseError(404, "not_found", "No such conversation.")
        existing = c.execute(
            text("SELECT count(*) FROM agent_conversation_turns WHERE conversation_id = :c"),
            {"c": conversation_id}).scalar() or 0
        greeting = not said
        if greeting and (conv["kind"] == "room" or existing):
            raise _invalid("say something first")
        addressed = _addressed(conv, said, to) if conv["kind"] == "room" else []
        if conv["kind"] == "room":
            order = addressed or list(conv["seats"])
            speakers = [_Speaker(k, "asked_by_person" if addressed else "seated") for k in order]
            if rounds == 2 and len(order) > 1:
                speakers += [_Speaker(k, "seated", round=2) for k in order]
        else:
            speakers = [_Speaker(conv["seats"][0], "asked_by_person")]
            rounds = 1
        claim = _guard(c, settings, project_id, conv, len(speakers), REPLY_RESERVE_MICROS)
        person_turn_id = None
        if not greeting:
            person_turn_id = _insert_turn(
                c, conversation_id, speaker_kind="person", actor=actor, addressed_to=addressed,
                kind="question" if said.rstrip().endswith("?") else "message",
                content=said)["turn_id"]
            first_said = not c.execute(text(
                "SELECT 1 FROM agent_conversation_turns WHERE conversation_id = :c "
                "AND speaker_kind = 'person' AND turn_id <> :t"),
                {"c": conversation_id, "t": person_turn_id}).first()
            if first_said and conv["kind"] != "talk" and not conv.get("preset"):
                # A room or a plan is ABOUT its first question; the default title was who.
                c.execute(text("UPDATE agent_conversations SET title = :t "
                               "WHERE conversation_id = :c"),
                          {"t": _title_from(said), "c": conversation_id})
    plan = _Plan(project_id, conversation_id, actor, "reply", speakers, person_turn_id, rounds,
                 REPLY_RESERVE_MICROS, claim)
    return _run(engine, settings, plan)


def _title_from(said: str) -> str:
    first = said.strip().splitlines()[0] if said.strip() else ""
    return first[:77] + "…" if len(first) > 80 else first or "Conversation"


def summarize_room(engine, *, project_id: str, conversation_id: str, actor: str,
                   settings: Any = None) -> Iterator[dict]:
    from ..db import begin

    settings = _settings(settings)
    with begin(engine) as c:
        conv = _row(c, project_id, conversation_id, lock=True)
        if conv["actor"] != actor:
            raise ConverseError(404, "not_found", "No such conversation.")
        if conv["kind"] != "room":
            raise _invalid("only a room has a summary")
        if not c.execute(text("SELECT 1 FROM agent_conversation_turns WHERE conversation_id = :c "
                              "AND speaker_kind = 'agent' AND status = 'complete'"),
                         {"c": conversation_id}).first():
            raise _invalid("nobody has spoken yet")
        claim = _guard(c, settings, project_id, conv, 1, SYNTHESIS_RESERVE_MICROS)
    lead = conv.get("lead_role_key") or conv["seats"][0]
    return _run(engine, settings, _Plan(project_id, conversation_id, actor, "summary",
                                        [_Speaker(lead, "asked_by_person")], None, 1,
                                        SYNTHESIS_RESERVE_MICROS, claim))


def propose_start(engine, *, project_id: str, conversation_id: str, actor: str,
                  settings: Any = None) -> Iterator[dict]:
    from ..db import begin

    settings = _settings(settings)
    with begin(engine) as c:
        conv = _row(c, project_id, conversation_id, lock=True)
        if conv["actor"] != actor:
            raise ConverseError(404, "not_found", "No such conversation.")
        if conv["kind"] == "talk":
            raise _invalid("plan it with an agent first")
        if not c.execute(text("SELECT 1 FROM agent_conversation_turns WHERE conversation_id = :c "
                              "AND speaker_kind = 'person'"), {"c": conversation_id}).first():
            raise _invalid("say what you want to start first")
        claim = _guard(c, settings, project_id, conv, 1, SYNTHESIS_RESERVE_MICROS)
    lead = conv.get("lead_role_key") or conv["seats"][0]
    return _run(engine, settings, _Plan(project_id, conversation_id, actor, "proposal",
                                        [_Speaker(lead, "asked_by_person")], None, 1,
                                        SYNTHESIS_RESERVE_MICROS, claim))


# ── Running a plan: one governed model call per speaker ──────────────────────────────────────


def _run(engine, settings: Any, plan: _Plan) -> Iterator[dict]:
    """The stream. Each speaker: turn_start → token* → (proposal|summary)? → turn_end; a failed
    speaker sends error and the room moves on; done closes it with the conversation summary."""
    from ..db import begin

    try:
        round_turns: dict[str, str] = {}   # role_key → its turn in round 1 (for round 2 replies)
        for speaker in plan.speakers:
            if speaker.round == 2:
                # Answer the seat that spoke just before you in round 1 — or, if you spoke
                # first, the last one: everyone answers someone, and nobody answers themselves.
                order = [s.role_key for s in plan.speakers if s.round == 1]
                i = order.index(speaker.role_key)
                target = order[i - 1] if i > 0 else order[-1]
                speaker.reply_to_turn_id = round_turns.get(target)
                speaker.reply_to_role = target
                if not speaker.reply_to_turn_id:
                    continue
            if not _hold_floor(engine, plan):
                break   # the claim lapsed and another stream took the floor: stop here
            started: Optional[str] = None
            speaking = _speak(engine, settings, plan, speaker)
            try:
                for event in speaking:
                    if event["type"] == "turn_start":
                        started = event["turn_id"]
                    if event["type"] == "turn_end" and event.get("status") == "complete":
                        if speaker.round == 1:
                            round_turns[speaker.role_key] = event["turn_id"]
                    yield event
            except Exception as error:  # noqa: BLE001 — one seat failing never ends the room
                log.exception("converse speaker %s failed", speaker.role_key)
                from .model_failures import explain

                # _speak has already settled its turn; say which one failed.
                if started:
                    yield {"type": "error", "turn_id": started, "error": explain(error)}
                    yield {"type": "turn_end", "turn_id": started, "status": "error",
                           "tokens_in": 0, "tokens_out": 0, "cost_usd": 0, "cited_acts": []}
                else:
                    yield {"type": "error", "error": explain(error)}
            finally:
                # Closing the stream (Stop, a closed tab) closes the seat speaking with it, now,
                # so its turn is settled before the floor is given back below.
                speaking.close()
        with begin(engine) as c:
            # Give the floor back BEFORE `done`: the console sends its next turn on `done`.
            _yield_floor(c, plan)
            conv = _row(c, plan.project_id, plan.conversation_id)
            summary = _shape(c, conv, with_turns=False)
        yield {"type": "done", "conversation": summary}
    finally:
        # However the stream ended — done, Stop, a closed tab, an error — the floor is free.
        # Nothing is said to the remaining seats: a stopped room skips them.
        try:
            with begin(engine) as c:
                _yield_floor(c, plan)
        except Exception:  # noqa: BLE001 — the claim lapses by itself if this cannot run
            log.exception("could not give back the floor of %s", plan.conversation_id)


def _transcript(connection, conv: dict, roles: dict[str, dict]) -> str:
    rows = connection.execute(
        text("SELECT * FROM (SELECT * FROM agent_conversation_turns WHERE conversation_id = :c "
             "AND status = 'complete' AND kind <> 'error' ORDER BY seq DESC LIMIT :n) t "
             "ORDER BY seq"),
        {"c": conv["conversation_id"], "n": TRANSCRIPT_TURNS},
    ).mappings().all()
    lines = []
    for r in rows:
        body = str(r["content"] or "")
        if len(body) > TRANSCRIPT_TURN_CHARS:
            body = body[:TRANSCRIPT_TURN_CHARS] + " …"
        if r["speaker_kind"] == "person":
            to = _jsonish(r["addressed_to"], [])
            aimed = (" (to " + ", ".join(_speaker(roles.get(k), k) for k in to) + ")") if to else ""
            lines.append(f"Operator{aimed}: {body}")
        else:
            lines.append(f"{_speaker(roles.get(r['role_key']), r['role_key'])}: {body}")
    return "\n\n".join(lines)


def _memory(connection, project_id: str, role_key: str) -> list[dict]:
    rows = connection.execute(
        text("SELECT act_id, label, status, started_at FROM agent_acts "
             "WHERE project_id = :p AND role_key = :k AND kind <> 'chat' "
             "ORDER BY started_at DESC LIMIT 5"),
        {"p": project_id, "k": role_key},
    ).mappings().all()
    return [{"act_id": r["act_id"], "label": r["label"], "status": r["status"],
             "at": _iso(r["started_at"]), "source": f"act:{r['act_id']}"} for r in rows]


def _about(connection, project_id: str, context: dict) -> str:
    run_id = context.get("workflow_run_id")
    if not run_id:
        return ""
    run = connection.execute(
        text("SELECT r.status, r.input, d.name FROM workflow_runs r "
             "JOIN workflow_definitions d ON d.id = r.workflow_definition_id "
             "WHERE r.id = :r AND r.project_id = :p"),
        {"r": run_id, "p": project_id},
    ).mappings().first()
    if not run:
        return ""
    title = (_jsonish(run["input"], {}) or {}).get("title") or ""
    line = f"This conversation is about the run {run_id} of '{run['name']}'" \
           f"{(' — ' + str(title)) if title else ''}, which is {run['status']}."
    step_id = context.get("step_id")
    if step_id:
        step = connection.execute(
            text("SELECT status, last_error FROM workflow_step_runs WHERE workflow_run_id = :r "
                 "AND step_id = :s ORDER BY iteration DESC, attempt DESC LIMIT 1"),
            {"r": run_id, "s": step_id},
        ).mappings().first()
        if step:
            line += f" Its step '{step_id}' is {step['status']}"
            line += f" ({str(step['last_error'])[:300]})." if step["last_error"] else "."
    return line


def _scope(connection, project_id: str, role_key: str) -> dict:
    skills = [r[0] for r in connection.execute(
        text("SELECT skill_id FROM role_skills WHERE project_id = :p AND role_key = :k "
             "ORDER BY skill_id"), {"p": project_id, "k": role_key}).all()]
    mcp = [r[0] for r in connection.execute(
        text("SELECT s.name FROM role_mcp_servers g JOIN mcp_servers s ON s.id = g.mcp_server_id "
             "WHERE g.project_id = :p AND g.role_key = :k ORDER BY s.name"),
        {"p": project_id, "k": role_key}).all()]
    return {"skills": skills, "mcp": mcp}


def _instruction(plan: _Plan, conv: dict, speaker: _Speaker, roles: dict[str, dict],
                 greeting: bool, catalogue: list[dict]) -> str:
    me = roles[speaker.role_key]
    others = [_speaker(roles.get(k), k) for k in conv["seats"] if k != speaker.role_key]
    if plan.mode == "summary":
        return (
            "The operator asked you, as the room's lead, to close the discussion. Reply with ONE "
            "JSON object and nothing else:\n"
            '{"text": "<the outcome in 2-4 sentences, in your voice>", '
            '"decisions": [{"text": "<what was decided>", "for": ["<role_key>"], '
            '"against": ["<role_key>"]}], '
            '"next_actions": [{"text": "<one action>", "owner_role_key": "<role_key or null>", '
            '"kind": "start|decision|note", "proposal": <only for kind start: '
            '{"workflow_key", "title", "directive", "inputs": {}, "acceptance_criteria": [], '
            '"why"}>}]}\n'
            f"role_keys in this room: {', '.join(conv['seats'])}. Record real dissent; do not "
            "invent agreement. kind=start only when a workflow below fits the work; "
            "kind=decision when the operator must choose.\n\n" + _catalogue_text(catalogue))
    if plan.mode == "proposal":
        return (
            "Turn this conversation into ONE concrete start the operator can review. Reply with "
            "ONE JSON object and nothing else:\n"
            '{"message": "<one sentence, in your voice, introducing the plan>", '
            '"workflow_key": "<one key from the list>", "title": "<≤8 words>", '
            '"directive": "<what to build or do, 1-3 sentences>", "inputs": {"<field>": "<value>"}, '
            '"acceptance_criteria": ["<testable>"], "why": "<why this workflow, one line>", '
            '"agents": {"<step_id>": "<role_key>"}}\n'
            "Fill only the inputs the workflow declares. Suggest agents only for steps marked "
            "Auto or (default), and only agents listed; prefer the agents in this "
            "conversation where their role fits the step.\n\n" + _catalogue_text(catalogue)
            + "\n\nAgents: " + ", ".join(f"{k} ({_speaker(r, k)})" for k, r in roles.items()))
    if conv["kind"] == "talk":
        if greeting:
            return ("The operator just opened a conversation with you. Greet them in one or two "
                    "sentences, in your own voice: who you are, what you can help with from your "
                    "role. Mention once that you are an AI agent. No lists.")
        return ("Reply to the operator's last message. Answer first, from your role's lens; at "
                "most 180 words unless they asked for depth. Cite your own work as [act:<id>] "
                "only if you use it.")
    if conv["kind"] == "plan":
        if greeting:
            return ("The operator wants to start a piece of work with you. Greet them in one "
                    "sentence, in your voice, mention you are an AI agent, and ask the ONE "
                    "question that matters most to scope it.")
        return ("Help the operator scope the work before anything starts. Ask at most two "
                "clarifying questions at a time, in your voice. When you know what, for whom and "
                "what done looks like, say so in one line and tell them to press Propose. At "
                "most 150 words. Workflows this project can run: "
                + (", ".join(f"{w['name']}" for w in catalogue[:8]) or "none installed yet")
                + ".")
    if speaker.round == 2:
        target = _speaker(roles.get(speaker.reply_to_role or ""), speaker.reply_to_role)
        return (f"Answer {target}'s point from this round directly. Agree or disagree by name "
                "with a reason, and add what they missed from your lens. At most 80 words. Do "
                "not repeat yourself.")
    aimed = ("The operator addressed you directly. " if speaker.how == "asked_by_person"
             else "")
    return (f"{aimed}You are seated in a room with {', '.join(others) or 'nobody else'}. Give "
            "your view on the operator's last message from your role's lens, at most 120 words. "
            "If a seat before you this round said something you disagree with, say so by name. "
            "Do not repeat what others said. Address others by name.")


def _catalogue(connection, settings: Any, project_id: str) -> list[dict]:
    from .workflows import PRODUCT_NAMESPACE, list_definitions

    out = []
    try:
        items = list_definitions(connection, project_id, namespace=PRODUCT_NAMESPACE)
    except Exception:  # noqa: BLE001 — a project with no workflows table rows is just empty
        return []
    for item in items[:12]:
        schema = _jsonish(item.get("input_schema"), {}) or {}
        props = schema.get("properties") or {}
        # The library's own outline (workflows._step_outline): id, the name a person reads and
        # who answers for it — the same facts the Library card draws.
        steps = item.get("steps") or []
        out.append({
            "workflow_key": item["workflow_key"], "name": item.get("name") or item["workflow_key"],
            "description": (item.get("description") or "")[:200],
            "inputs": {k: str((v or {}).get("title") or k) for k, v in props.items()},
            "required": list(schema.get("required") or []),
            "steps": [{"id": str(s["id"]), "label": str(s.get("title") or s.get("label")
                                                       or s["id"]),
                       "agent": str(s.get("captain") or "") or None,
                       # The catalogue's soft default: the card may name someone else.
                       "soft": bool(s.get("default_captain"))}
                      for s in steps if isinstance(s, dict) and s.get("id")],
        })
    return out


def _catalogue_text(catalogue: list[dict]) -> str:
    if not catalogue:
        return "Workflows this project can run: none."
    lines = ["Workflows this project can run:"]
    for w in catalogue:
        steps = ", ".join(
            f"{s['id']} ({s['label']}; "
            f"{(s['agent'] + ' (default)') if s['agent'] and s.get('soft') else s['agent'] or 'Auto'})"
            for s in w["steps"])
        inputs = ", ".join(f"{k}{'*' if k in w['required'] else ''}" for k in w["inputs"])
        lines.append(f"- {w['workflow_key']}: {w['name']} — {w['description']} | inputs: "
                     f"{inputs or 'none'} | steps: {steps or 'none'}")
    return "\n".join(lines)


def _reveal(text_value: str) -> Iterator[str]:
    """Word groups of a finished reply, paced so the whole reveal takes REVEAL_SECONDS."""
    words = re.findall(r"\S+\s*", text_value)
    if not words:
        return
    size = max(1, len(words) // 40)
    groups = ["".join(words[i:i + size]) for i in range(0, len(words), size)]
    pause = REVEAL_SECONDS / len(groups) if REVEAL_SECONDS > 0 else 0
    for group in groups:
        yield group
        if pause:
            time.sleep(pause)


def _call(adapter, system_prompt: str, prompt: str) -> Iterator[tuple[str, Any]]:
    """Yields ('chunk', text)… then ('done', (full_text, usage)). Streams when the adapter can."""
    streamer = getattr(adapter, "stream_text_response", None)
    if callable(streamer):
        parts: list[str] = []
        usage: dict = {}
        for piece in streamer(prompt, system_prompt=system_prompt):
            if isinstance(piece, dict):
                usage = piece
                continue
            parts.append(str(piece))
            yield "chunk", str(piece)
        yield "done", ("".join(parts), usage)
        return
    reply = adapter.create_text_response(prompt, system_prompt=system_prompt)
    if isinstance(reply, tuple):
        full = str(reply[1] if len(reply) > 1 else reply[0] or "")
        usage = reply[2] if len(reply) > 2 and isinstance(reply[2], dict) else {}
    else:
        full, usage = str(reply or ""), {}
    yield "done", (full, usage)


def _only_json(value: str) -> dict:
    trimmed = str(value or "").strip()
    start, end = trimmed.find("{"), trimmed.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("The agent did not answer with a plan. Try again.")
    parsed = json.loads(trimmed[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("The agent did not answer with a plan. Try again.")
    return parsed


def _receipt(connection, project_id: str, turn_id: str, scope: dict, payload: dict) -> str:
    from .receipts import ReceiptService

    owner = connection.execute(
        text("SELECT p.workspace_id, w.org_id FROM projects p "
             "JOIN workspaces w ON w.id = p.workspace_id WHERE p.id = :p"),
        {"p": project_id},
    ).mappings().first()
    receipts = ReceiptService()
    receipt, _ = receipts.create_or_reuse(
        connection, owner["org_id"], owner["workspace_id"], project_id,
        dedupe_key=f"converse:{turn_id}", effect_kind="model.converse_turn",
        effect_scope=scope, payload=payload)
    receipts.mark_succeeded(connection, receipt["id"], None, payload)
    return receipt["id"]


def _speak(engine, settings: Any, plan: _Plan, speaker: _Speaker) -> Iterator[dict]:
    from ..db import begin
    from .budget import BudgetGovernor
    from .llm_telemetry import record_llm_call, set_llm_context
    from .model_failures import explain
    from .openai_adapter import estimate_cost_usd_micros

    budget = BudgetGovernor(settings)
    with begin(engine) as c:
        conv = _row(c, plan.project_id, plan.conversation_id, lock=True)
        roles = _roles(c, plan.project_id, list(dict.fromkeys(conv["seats"] + [speaker.role_key])))
        role = roles.get(speaker.role_key)
        if not role or (role.get("lifecycle") or "active") != "active":
            name = (role or {}).get("display_name") or speaker.role_key
            yield {"type": "error", "error": {
                "kind": "agent_unavailable", "headline": f"{name} is not available.",
                "action": "Resume it on its Passport, or ask another agent.",
                "href": f"/?area=workforce&role={speaker.role_key}", "retry": False}}
            return
        greeting = plan.mode == "reply" and plan.person_turn_id is None
        catalogue = _catalogue(c, settings, plan.project_id) if (
            plan.mode in ("summary", "proposal") or conv["kind"] == "plan") else []
        if plan.mode == "proposal" and not catalogue:
            yield {"type": "error", "error": {
                "kind": "no_workflow", "headline": "No workflow to start yet.",
                "action": "Add one from Workflows › Library, then propose again.",
                "href": "/?area=delivery_workflows", "retry": False}}
            return
        memory = _memory(c, plan.project_id, speaker.role_key)
        about = _about(c, plan.project_id, conv["context"])
        transcript = _transcript(c, conv, roles)
        # THE ACT. Parent: what the dock was opened about, else the turn being answered, else
        # this conversation's previous agent turn — a conversation is one chain of work.
        parent = None
        if speaker.reply_to_turn_id:
            parent = c.execute(text("SELECT act_id FROM agent_conversation_turns "
                                    "WHERE turn_id = :t"),
                               {"t": speaker.reply_to_turn_id}).scalar()
        if not parent:
            parent = c.execute(text(
                "SELECT act_id FROM agent_conversation_turns WHERE conversation_id = :c AND "
                "act_id IS NOT NULL ORDER BY seq DESC LIMIT 1"),
                {"c": plan.conversation_id}).scalar() or conv["context"].get("act_id")
        root = None
        if parent:
            root = c.execute(text("SELECT root_act_id FROM agent_acts WHERE act_id = :a"),
                             {"a": parent}).scalar()
        is_room = conv["kind"] == "room"
        scope = _scope(c, plan.project_id, speaker.role_key)
        reservation = budget.reserve(c, plan.project_id, BUDGET_CATEGORY, plan.reserve_micros,
                                     f"conversation {plan.conversation_id}",
                                     expires_in_minutes=15)
        held = reservation.get("reservation_id") if reservation.get("status") == "reserved" \
            else None
        label_kind = {"talk": "Talk", "plan": "Plan", "room": "Room"}[conv["kind"]]
        act_id = agent_acts.open_act(
            c, project_id=plan.project_id, kind="seat" if is_room else "chat",
            role_key=speaker.role_key,
            how="seated" if is_room and speaker.how == "seated" else "asked_by_person",
            handed_by_kind="person", handed_by_id=plan.actor, parent_act_id=parent,
            root_act_id=root, why={"summary": "Asked to sum up the room",
                                   "proposal": "Asked to propose a start"}.get(plan.mode),
            scope=scope, accountable=conv["actor"], subject_kind="conversation",
            subject_id=plan.conversation_id, label=f"{label_kind} · {conv['title']}"[:160],
            thread_id=plan.conversation_id,
            workflow_run_id=conv["context"].get("workflow_run_id"),
            step_id=conv["context"].get("step_id"))
        if act_id and not conv.get("root_act_id"):
            c.execute(text("UPDATE agent_conversations SET root_act_id = COALESCE(:r, :a) "
                           "WHERE conversation_id = :c"),
                      {"r": root, "a": act_id, "c": plan.conversation_id})
        turn = _insert_turn(c, plan.conversation_id, speaker_kind="agent",
                            role_key=speaker.role_key, reply_to_turn_id=speaker.reply_to_turn_id,
                            kind={"summary": "summary", "proposal": "proposal"}.get(
                                plan.mode, "message"),
                            status="streaming", act_id=act_id, fingerprint=role.get("fingerprint"),
                            # Until it finishes, the turn carries its reservation, so whoever
                            # finds it cut off can give the budget back (_sweep).
                            payload={"reservation_id": held} if held else {})
        system_prompt = _compose(
            role, mode="panel" if is_room else "chat",
            act={"act_id": act_id, "label": f"{label_kind} · {conv['title']}",
                 "handed_by_kind": "person", "handed_by_id": plan.actor,
                 "how": "seated" if is_room and speaker.how == "seated" else "asked_by_person",
                 "accountable": conv["actor"], "scope": scope,
                 "seats": [_seat_info(roles[k]) for k in conv["seats"] if k in roles]},
            memory=memory)
        adapter = make_adapter(c, settings, plan.project_id)
        engine_name = _engine_name(c, settings, plan.project_id)
    turn_id = turn["turn_id"]
    # Once the turn exists it is settled exactly once, whatever happens next: completed below,
    # failed with a reason (`fail`), or — if the stream is closed mid-answer (Stop, a closed
    # tab: GeneratorExit) or anything after this point raises — stopped by the `except` at the
    # end. Never left 'streaming' with its act open and its budget held.
    finished = False

    def fail(error: Any) -> Iterator[dict]:
        nonlocal finished
        failure = explain(error)
        with begin(engine) as c2:
            _settle_cut_off(c2, settings, plan.project_id, turn_id, act_id, held, failure)
        finished = True
        yield {"type": "error", "turn_id": turn_id, "error": failure}
        yield {"type": "turn_end", "turn_id": turn_id, "status": "error", "tokens_in": 0,
               "tokens_out": 0, "cost_usd": 0, "cited_acts": []}

    def record(**call: Any) -> None:
        # Attribution is set in the SAME generator step as the write. A streamed response
        # drives each step on a fresh copy of the request's context, so a value set before a
        # yield is gone after it.
        set_llm_context(db_engine=engine, project_id=plan.project_id, stage="converse",
                        act_id=act_id, role_key=speaker.role_key, settings=settings)
        try:
            record_llm_call(engine_name=engine_name, stage="converse",
                            latency_ms=int((time.monotonic() - started) * 1000), **call)
        finally:
            set_llm_context(act_id=None, role_key=None)

    try:
        yield {"type": "turn_start", "turn_id": turn_id, "seq": turn["seq"],
               "speaker_kind": "agent", "role_key": speaker.role_key, "act_id": act_id,
               **({"reply_to_turn_id": speaker.reply_to_turn_id}
                  if speaker.reply_to_turn_id else {})}

        if not held:
            yield from fail(f"budget reservation denied: {reservation.get('message')}")
            return

        prompt_parts = [p for p in (
            about,
            f"The conversation so far:\n\n{transcript}" if transcript else "",
            "Your task now: " + _instruction(plan, conv, speaker, roles, greeting, catalogue),
        ) if p]
        prompt = "\n\n".join(prompt_parts)
        started = time.monotonic()
        full, usage = "", {}
        structured = plan.mode in ("summary", "proposal")
        try:
            # An adapter that reads the context (provider-capacity, a streaming adapter) sees
            # this call's attribution too.
            set_llm_context(db_engine=engine, project_id=plan.project_id, stage="converse",
                            act_id=act_id, role_key=speaker.role_key, settings=settings)
            for kind, value in _call(adapter, system_prompt, prompt):
                if kind == "chunk" and not structured:
                    yield {"type": "token", "turn_id": turn_id, "text": value}
                elif kind == "done":
                    full, usage = value
                    usage = usage or {}
        except Exception as error:  # noqa: BLE001 — explained to the operator, never raised
            record(usage={}, ok=False, error=str(error))
            yield from fail(error)
            return
        cost = estimate_cost_usd_micros(usage) if usage else 0
        record(usage=usage, cost_usd_micros=cost)
        if not structured and not getattr(adapter, "stream_text_response", None):
            for piece in _reveal(full):
                yield {"type": "token", "turn_id": turn_id, "text": piece}

        payload: dict = {}
        content = full.strip()
        extra_events: list[dict] = []
        if structured:
            try:
                with begin(engine) as c:
                    parsed = _only_json(full)
                    if plan.mode == "proposal":
                        proposal = validate_proposal(c, settings, plan.project_id, parsed,
                                                     catalogue)
                        _supersede(c, plan.conversation_id)
                        payload = proposal
                        content = str(parsed.get("message") or proposal["why"]
                                      or "Here is a plan.")
                        extra_events.append({"type": "proposal", "turn_id": turn_id,
                                             "proposal": proposal})
                    else:
                        summary = _validate_summary(c, settings, plan.project_id, parsed, conv,
                                                    speaker.role_key, catalogue)
                        payload = summary
                        content = summary["text"]
                        extra_events.append({"type": "summary", "turn_id": turn_id,
                                             "summary": summary})
            except (ValueError, KeyError, TypeError) as error:
                yield from fail(f"failed: {error}")
                return
            for piece in _reveal(content):
                yield {"type": "token", "turn_id": turn_id, "text": piece}
        offered = {m["act_id"] for m in memory}
        cited = [a for a in dict.fromkeys(_CITE.findall(content)) if a in offered]
        tokens_in = int(usage.get("prompt_tokens") or 0)
        tokens_out = int(usage.get("completion_tokens") or 0)
        receipt_id = None
        with begin(engine) as c:
            # Only a turn still streaming completes. One already settled as cut off (this
            # stream outlived its claim and the next turn swept it) stays settled: its budget
            # was given back and its act closed, so this answer is not recorded twice.
            if not c.execute(text(
                    "UPDATE agent_conversation_turns SET status = 'complete' "
                    "WHERE turn_id = :t AND status = 'streaming'"), {"t": turn_id}).rowcount:
                finished = True
                return
            receipt_id = _receipt(c, plan.project_id, turn_id,
                                  {"conversation_id": plan.conversation_id, "turn_id": turn_id,
                                   "role_key": speaker.role_key, "act_id": act_id},
                                  {"tokens_in": tokens_in, "tokens_out": tokens_out,
                                   "cost_usd_micros": cost, "model": usage.get("model")})
            budget.consume(c, plan.project_id, held, receipt_id=receipt_id,
                           category=BUDGET_CATEGORY,
                           skill_or_model_id=f"converse:{usage.get('model') or 'model'}",
                           estimated_usd_micros=plan.reserve_micros, actual_usd_micros=cost,
                           actor=plan.actor)
            c.execute(text("""
                UPDATE agent_conversation_turns SET content = :content,
                       payload = CAST(:payload AS JSONB), cited_acts = CAST(:cited AS JSONB),
                       tokens_in = :ti, tokens_out = :to, cost_usd = :cost, receipt_id = :r
                 WHERE turn_id = :t
            """), {"content": content, "payload": json.dumps(payload),
                   "cited": json.dumps(cited), "ti": tokens_in, "to": tokens_out,
                   "cost": cost / 1_000_000, "r": receipt_id, "t": turn_id})
            if plan.mode == "summary":
                c.execute(text("UPDATE agent_conversations SET summary = CAST(:s AS JSONB), "
                               "status = 'summarized', updated_at = now() "
                               "WHERE conversation_id = :c"),
                          {"s": json.dumps(payload), "c": plan.conversation_id})
            # Tokens and cost roll up from this act's llm_calls row (`record` above): the
            # ledger counts a model call once, so nothing of the caller's own is added.
            if act_id:
                agent_acts.close_act(c, act_id, status="complete",
                                     artifacts=[{"kind": "receipt", "ref": receipt_id}])
        finished = True
        yield from extra_events
        yield {"type": "turn_end", "turn_id": turn_id, "status": "complete",
               "tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": cost / 1_000_000,
               "receipt_id": receipt_id, "cited_acts": cited}
    except BaseException as error:
        if not finished:
            try:
                with begin(engine) as c2:
                    _settle_cut_off(c2, settings, plan.project_id, turn_id, act_id, held,
                                    STOPPED if isinstance(error, GeneratorExit)
                                    else explain(error))
            except Exception:  # noqa: BLE001 — the next turn's sweep settles it instead
                log.exception("could not settle the cut-off turn %s", turn_id)
        raise


# ── Proposals and summaries ──────────────────────────────────────────────────────────────────


def validate_proposal(connection, settings: Any, project_id: str, raw: dict,
                      catalogue: list[dict]) -> dict:
    """A model's plan, held to what this project can actually run. Raises ValueError."""
    key = str(raw.get("workflow_key") or "").strip()
    chosen = next((w for w in catalogue if w["workflow_key"] == key), None) or next(
        (w for w in catalogue if w["name"].lower() == key.lower()), None)
    if not chosen:
        raise ValueError("the plan named a workflow this project does not have")
    title = str(raw.get("title") or "").strip()[:80] or chosen["name"]
    directive = str(raw.get("directive") or "").strip()[:2000]
    if not directive:
        raise ValueError("the plan had no directive")
    given = raw.get("inputs") if isinstance(raw.get("inputs"), dict) else {}
    inputs = {k: str(v).strip()[:2000] for k, v in given.items()
              if k in chosen["inputs"] and v not in (None, "") and not str(k).startswith("adopt_")}
    for field, value in (("title", title), ("directive", directive),
                         ("instructions", directive)):
        if field in chosen["inputs"] and field not in inputs:
            inputs[field] = value
    roles = _roles(connection, project_id)
    suggested = raw.get("agents") if isinstance(raw.get("agents"), dict) else {}
    agents = []
    for step in chosen["steps"]:
        # Exactly what the run will do (step_agents.with_run_agent): a step the workflow names
        # keeps its agent, a gate keeps its approver, and the suggestion fills a step that names
        # nobody or replaces a soft catalogue default — never a promise the run will not keep.
        named = step["agent"]
        pick = None
        if named and (named == "human" or not step.get("soft")):
            pick = None if named == "human" else named
        else:
            candidate = _resolve(str(suggested.get(step["id"]) or ""))
            if candidate in roles and (roles[candidate].get("lifecycle") or "active") == "active":
                pick = candidate
            elif named:
                pick = named
        agents.append({"step_id": step["id"], "step_label": step["label"], "role_key": pick})
    criteria = [str(c).strip()[:300] for c in (raw.get("acceptance_criteria") or [])
                if str(c).strip()][:8]
    proposal = {
        "proposal_id": make_id("prp"), "workflow_key": chosen["workflow_key"], "title": title,
        "directive": directive, "inputs": inputs, "acceptance_criteria": criteria,
        "agents": agents, "why": str(raw.get("why") or "").strip()[:200],
        "status": "proposed", "workflow_name": chosen["name"],
    }
    proposal["readiness"] = readiness_of(connection, settings, project_id, proposal, chosen)
    return proposal


def readiness_of(connection, settings: Any, project_id: str, proposal: dict,
                 chosen: Optional[dict] = None) -> dict:
    """What Start would say about this card right now: {ok, problems[]}."""
    from .workflow_readiness import readiness_of_version
    from .workflows import running_version

    problems: list[str] = []
    if chosen:
        problems += [f"Needs {chosen['inputs'][f]}" for f in chosen["required"]
                     if not proposal["inputs"].get(f)]
    running = running_version(connection, project_id, proposal["workflow_key"])
    if not running:
        return {"ok": False, "problems": ["That workflow is no longer installed"]}
    try:
        found = readiness_of_version(connection, settings, project_id,
                                     version_id=running["version_id"],
                                     input_data=proposal["inputs"])
        problems += [str(m.get("label") or m.get("key")) for m in found.get("missing") or []]
    except Exception:  # noqa: BLE001 — Start re-checks; the card must still render
        pass
    return {"ok": not problems, "problems": problems}


def _validate_summary(connection, settings: Any, project_id: str, raw: dict, conv: dict,
                      lead: str, catalogue: list[dict]) -> dict:
    seats = set(conv["seats"])

    def keys(values: Any) -> list[str]:
        out = []
        for v in values or []:
            k = _resolve(str(v))
            if k in seats and k not in out:
                out.append(k)
        return out

    text_value = str(raw.get("text") or "").strip()
    if not text_value:
        raise ValueError("the summary was empty")
    decisions = [{"text": str(d.get("text") or "").strip()[:400], "for": keys(d.get("for")),
                  "against": keys(d.get("against"))}
                 for d in (raw.get("decisions") or []) if isinstance(d, dict)
                 and str(d.get("text") or "").strip()][:6]
    actions = []
    for a in (raw.get("next_actions") or [])[:6]:
        if not isinstance(a, dict) or not str(a.get("text") or "").strip():
            continue
        owner = _resolve(str(a.get("owner_role_key") or ""))
        kind = a.get("kind") if a.get("kind") in ("start", "decision", "note") else "note"
        action: dict = {"text": str(a["text"]).strip()[:300],
                        "owner_role_key": owner if owner in seats else None, "kind": kind}
        if kind == "start":
            try:
                action["proposal"] = validate_proposal(
                    connection, settings, project_id, a.get("proposal") or {}, catalogue)
            except ValueError:
                # A start the project cannot run is said as what it is: something to do.
                action["kind"] = "note"
        actions.append(action)
    return {"text": text_value[:1500], "by_role_key": lead, "decisions": decisions,
            "next_actions": actions}


def _supersede(connection, conversation_id: str) -> None:
    connection.execute(
        text("""
            UPDATE agent_conversation_turns
               SET payload = jsonb_set(payload, '{status}', '"superseded"')
             WHERE conversation_id = :c AND kind = 'proposal' AND payload->>'status' = 'proposed'
        """),
        {"c": conversation_id},
    )


def _find_proposal(connection, conversation_id: str, proposal_id: str
                   ) -> tuple[str, dict, Optional[int]]:
    """(turn_id, proposal, index into summary.next_actions or None)."""
    row = connection.execute(
        text("SELECT turn_id, payload FROM agent_conversation_turns WHERE conversation_id = :c "
             "AND kind = 'proposal' AND payload->>'proposal_id' = :id"),
        {"c": conversation_id, "id": proposal_id},
    ).mappings().first()
    if row:
        return row["turn_id"], _jsonish(row["payload"], {}), None
    row = connection.execute(
        text("SELECT turn_id, payload FROM agent_conversation_turns WHERE conversation_id = :c "
             "AND kind = 'summary' ORDER BY seq DESC"),
        {"c": conversation_id},
    ).mappings().all()
    for r in row:
        summary = _jsonish(r["payload"], {})
        for i, action in enumerate(summary.get("next_actions") or []):
            if (action.get("proposal") or {}).get("proposal_id") == proposal_id:
                return r["turn_id"], action["proposal"], i
    raise ConverseError(404, "not_found", "No such proposal.")


def _latest_summary_turn(connection, conversation_id: str) -> Optional[str]:
    return connection.execute(
        text("SELECT turn_id FROM agent_conversation_turns WHERE conversation_id = :c "
             "AND kind = 'summary' AND status = 'complete' ORDER BY seq DESC LIMIT 1"),
        {"c": conversation_id}).scalar()


def _patch_action(connection, conversation_id: str, turn_id: str, index: int,
                  fields: dict) -> None:
    """Set fields on ONE next action of ONE summary turn. A next action is addressed by
    (summary turn, index), and lives in that turn's payload; the conversation's `summary` is a
    copy of the latest summary, so it gets the same fields only when this turn IS the latest —
    merged into what is there, never replaced by an older summary."""
    summary = _jsonish(connection.execute(
        text("SELECT payload FROM agent_conversation_turns WHERE turn_id = :t"),
        {"t": turn_id}).scalar(), {})
    summary["next_actions"][index].update(fields)
    connection.execute(
        text("UPDATE agent_conversation_turns SET payload = CAST(:p AS JSONB) WHERE turn_id = :t"),
        {"p": json.dumps(summary), "t": turn_id})
    if _latest_summary_turn(connection, conversation_id) != turn_id:
        return
    current = _jsonish(connection.execute(
        text("SELECT summary FROM agent_conversations WHERE conversation_id = :c"),
        {"c": conversation_id}).scalar(), None) or summary
    actions = current.get("next_actions") or []
    if 0 <= index < len(actions):
        actions[index].update(fields)
    connection.execute(
        text("UPDATE agent_conversations SET summary = CAST(:p AS JSONB), updated_at = now() "
             "WHERE conversation_id = :c"), {"p": json.dumps(current), "c": conversation_id})


def _save_proposal(connection, conversation_id: str, turn_id: str, proposal: dict,
                   index: Optional[int]) -> None:
    if index is None:
        connection.execute(
            text("UPDATE agent_conversation_turns SET payload = CAST(:p AS JSONB) "
                 "WHERE turn_id = :t"), {"p": json.dumps(proposal), "t": turn_id})
        return
    _patch_action(connection, conversation_id, turn_id, index, {"proposal": proposal})


def dismiss_proposal(connection, *, project_id: str, conversation_id: str, actor: str,
                     proposal_id: str) -> dict:
    conv = _row(connection, project_id, conversation_id, lock=True)
    if conv["actor"] != actor:
        raise ConverseError(404, "not_found", "No such conversation.")
    turn_id, proposal, index = _find_proposal(connection, conversation_id, proposal_id)
    if proposal.get("status") == "proposed":
        proposal["status"] = "dismissed"
        _save_proposal(connection, conversation_id, turn_id, proposal, index)
    return get_conversation(connection, project_id, conversation_id)


def start_from_proposal(connection, *, project_id: str, conversation_id: str, actor: str,
                        proposal_id: str, start_anyway: Optional[list[str]] = None,
                        inputs: Optional[dict] = None,
                        starter: Optional[Callable[[dict], dict]] = None) -> dict:
    """{run_id, conversation}. `starter` is the API's own start door (the composer's), called
    with its request body; its refusals (readiness 409 and the rest) propagate unchanged.

    Idempotent per card: a card already started answers with the run it started.
    """
    conv = _row(connection, project_id, conversation_id, lock=True)
    if conv["actor"] != actor:
        raise ConverseError(404, "not_found", "No such conversation.")
    turn_id, proposal, index = _find_proposal(connection, conversation_id, proposal_id)
    if proposal.get("status") == "started" and proposal.get("run_id"):
        return {"run_id": proposal["run_id"],
                "conversation": get_conversation(connection, project_id, conversation_id)}
    if proposal.get("status") != "proposed":
        raise ConverseError(409, "stale", f"This plan was {proposal.get('status')}.")
    if starter is None:
        raise RuntimeError("start_from_proposal needs the API's start door")
    run_input = dict(proposal.get("inputs") or {})
    for key, value in (inputs or {}).items():
        if isinstance(key, str) and not key.startswith("adopt_") and value is not None:
            run_input[key] = str(value)[:2000]
    named = {a["step_id"]: a["role_key"] for a in proposal.get("agents") or [] if a.get("role_key")}
    if named:
        run_input["step_agents"] = named
    started = starter({
        "project_id": project_id, "actor": actor, "workflow_key": proposal["workflow_key"],
        "input": run_input, "start_anyway": list(start_anyway or []),
        # One card, one run: a second press replays the first start.
        "dedupe_key": f"converse:{proposal_id}",
    })
    run = started.get("run") or {}
    run_id = str(run.get("id") or run.get("run_id") or "")
    proposal.update({"status": "started", "run_id": run_id, "inputs": run_input})
    _save_proposal(connection, conversation_id, turn_id, proposal, index)
    connection.execute(
        text("UPDATE agent_conversations SET status = 'started', started_run_id = :r, "
             "updated_at = now() WHERE conversation_id = :c"),
        {"r": run_id, "c": conversation_id},
    )
    emit_event(connection, project_id, "conversation.started_run", actor, "conversation",
               conversation_id, {"run_id": run_id, "proposal_id": proposal_id,
                                 "workflow_key": proposal["workflow_key"]})
    return {"run_id": run_id, "conversation": get_conversation(connection, project_id,
                                                               conversation_id)}


def open_decision(connection, *, project_id: str, conversation_id: str, actor: str,
                  index: int, turn_id: Optional[str] = None) -> dict:
    """A room summary's `decision` next action, opened as a real decision for the operator.

    The action is (summary turn, index): every summary card in the transcript opens its own
    action. Without `turn_id`, the latest summary's."""
    from .core import create_blocking_decision

    conv = _row(connection, project_id, conversation_id, lock=True)
    if conv["actor"] != actor:
        raise ConverseError(404, "not_found", "No such conversation.")
    turn_id = turn_id or _latest_summary_turn(connection, conversation_id)
    row = connection.execute(
        text("SELECT payload FROM agent_conversation_turns WHERE turn_id = :t "
             "AND conversation_id = :c AND kind = 'summary' AND status = 'complete'"),
        {"t": turn_id, "c": conversation_id}).first() if turn_id else None
    if not row:
        raise _invalid("no such summary")
    summary = _jsonish(row[0], {})
    actions = summary.get("next_actions") or []
    if not 0 <= int(index) < len(actions):
        raise _invalid("no such next action")
    action = actions[int(index)]
    if action.get("decision_id"):
        return {"decision_id": action["decision_id"],
                "conversation": get_conversation(connection, project_id, conversation_id)}
    if action.get("kind") != "decision":
        raise _invalid("that next action is not a decision")
    decision_id = create_blocking_decision(
        connection, project_id, None, action["text"][:300],
        {"action": "decide", "from_conversation": conversation_id, "from_turn": turn_id,
         "owner_role_key": action.get("owner_role_key"), "context": summary.get("text")},
        {}, "agent_room", risk_tier=1)
    _patch_action(connection, conversation_id, turn_id, int(index), {"decision_id": decision_id})
    emit_event(connection, project_id, "conversation.decision_opened", actor, "conversation",
               conversation_id, {"decision_id": decision_id, "turn_id": turn_id})
    return {"decision_id": decision_id,
            "conversation": get_conversation(connection, project_id, conversation_id)}


def sse(events: Iterator[dict]) -> Iterator[str]:
    """`event: <type>` + `data: <json of the rest>` per event — the ConverseEvent wire.

    Closing this (the client went away) closes the conversation stream at once, so the seat
    speaking is settled and the floor given back now, not whenever the collector gets to it."""
    try:
        for event in events:
            body = {k: v for k, v in event.items() if k != "type"}
            yield f"event: {event['type']}\ndata: {json.dumps(body, default=str)}\n\n"
    finally:
        close = getattr(events, "close", None)
        if callable(close):
            close()
