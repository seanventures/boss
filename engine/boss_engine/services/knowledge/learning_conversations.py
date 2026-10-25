"""Conversations as provenance anchors (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §2.5, §4.6; BOSSAPP-768 S2c).

An agent investigating in a chat turn (an incident sent to the Site Reliability Engineer) reads
precedents and rules and records what it found exactly as a workflow step does — but it has no
run. Without an anchor its uses were dropped (``log_use`` needed a run id) and what it learned hung
on nothing, so "learned by agent X in A → used by agent Y in B" could not be shown for the very
work that proves it.

A conversation is therefore an anchor of its own, with the least disruption to the run model:

- **The node** is a ``run`` node keyed ``run:<thread id>`` (the key every run reader already
  follows), ``attrs.subtype = 'conversation'``, named after the conversation's first question,
  with the addressed agent (role, name, Passport id) and when it started.
- **Usage** (``kg_usage``) from a chat turn carries ``run_id = agent_thread_id = <thread id>``, the
  turn's act (``act_id``) and the message that answered (``message_id``, stamped when the reply or
  the press outcome is written). ``consulted`` / ``reused`` edges are derived from those rows by
  the same ``sync_usage_edges`` as for runs, live and at ingest.
- **Learnings** recorded in the conversation (said by the agent, or pressed by a person) are
  ``produced`` by that node and credited to the conversation's addressed agent.

``backfill`` derives the same rows and edges for conversations that predate this, ONLY from what
their transcript shows (a press outcome naming the learning it wrote; a stored tool result's ids;
an ``[L:…]`` / ``[R:…]`` citation) — never from a guess or a replay.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .titles import word_cut

LOG = logging.getLogger(__name__)

THREAD_PREFIX = "athr_"
SUBTYPE = "conversation"
#: The read tools whose results are rules or learnings returned to the turn ("consulted").
KNOWLEDGE_READ_TOOLS = frozenset({"kg_precedent", "kg_rules", "kg_why", "kg_search"})
#: The write tools whose pressed outcome names the item they wrote ("produced").
KNOWLEDGE_WRITE_TOOLS = frozenset({"record_learning", "kg_propose_rule"})
MAX_REFS = 50
MAX_TITLE = 120
#: A press outcome quoting what the write returned (AgentService.record_action_outcome).
_WROTE = re.compile(r'"(?:learning|rule)":\s*\{\s*"id":\s*"(?P<id>kgn_[A-Za-z0-9_]+)"')
_HEADING = re.compile(r"^[#>\s*_`]+")


def is_conversation(ref: Any) -> bool:
    return str(ref or "").startswith(THREAD_PREFIX)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def _jsonish(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


# --- the anchor -----------------------------------------------------------------------------------
def thread_row(connection: Connection, project_id: Optional[str], thread_id: str) -> Optional[dict[str, Any]]:
    """The thread, of ``project_id`` when one is named (None: whichever project it is in)."""
    row = connection.execute(text(
        "SELECT id, project_id, agent_role, surface, status, created_at, updated_at FROM agent_threads "
        "WHERE id = :t AND (CAST(:p AS TEXT) IS NULL OR project_id = :p)"),
        {"t": thread_id, "p": project_id}).mappings().first()
    return dict(row) if row else None


def first_line(content: Any) -> Optional[str]:
    """A question's first line, without the Markdown around it ("B4 · Incident: …"), or None."""
    for line in str(content or "").splitlines():
        cleaned = _HEADING.sub("", line).strip().strip("*_` ")
        if cleaned:
            return cleaned[:MAX_TITLE]
    return None


def title_of(connection: Connection, thread_id: str, *, question: Any = None) -> str:
    """The conversation's name: the first line of its first question ("B4 · Incident: early
    repayments post more than the customer paid"), without the Markdown around it.

    ``question`` is the turn's own message (agent_identity.speaking): a knowledge tool anchors the
    conversation on its own connection, before the turn's message row is committed, and reads no
    first question; the turn's message is that question. NEVER THE THREAD ID AS A NAME: with
    neither, the agent it was addressed to and "Conversation" (a later turn, or the read, names it)."""
    first = connection.execute(text(
        "SELECT content FROM agent_messages WHERE thread_id = :t AND role = 'user' "
        "ORDER BY created_at, id LIMIT 1"), {"t": thread_id}).scalar()
    named = first_line(first) or first_line(question)
    if named:
        return named
    return UNNAMED


#: The name of a conversation nobody can name yet. Never a raw ``athr_`` id: the placeholder
#: the anchor once wrote ("Conversation athr_…") is found by is_placeholder and re-named.
UNNAMED = "Conversation"
_PLACEHOLDER = re.compile(r"^Conversation(?: athr_[A-Za-z0-9_]+)?(?: · .*)?$")


def is_placeholder(name: Any) -> bool:
    """A conversation node still named by its id (or by nothing): "Conversation athr_… · Oct 25, 16:15"."""
    return bool(_PLACEHOLDER.match(str(name or "").strip()))


def node_name(title: str, started: Any) -> str:
    """As a run is named (learning_writeback.run_title): the title, then when it started."""
    if isinstance(started, datetime):
        return f"{word_cut(title)} · {started:%b %d, %H:%M}"
    return title


def proper_names(connection: Connection, thread_ids: Iterable[str]) -> dict[str, str]:
    """The node name each conversation SHOULD carry, from its first question, for those that have
    one (one query). A thread with no question yet is left out: it stays as it is."""
    ids = [t for t in dict.fromkeys(str(t) for t in thread_ids) if is_conversation(t)]
    if not ids:
        return {}
    rows = connection.execute(text(
        """
        SELECT t.id, t.created_at,
               (SELECT m.content FROM agent_messages m WHERE m.thread_id = t.id AND m.role = 'user'
                 ORDER BY m.created_at, m.id LIMIT 1) AS question
        FROM agent_threads t WHERE t.id = ANY(:t)
        """), {"t": ids}).mappings().all()
    out: dict[str, str] = {}
    for row in rows:
        title = first_line(row["question"])
        if title:
            out[str(row["id"])] = node_name(title, row["created_at"])
    return out


def rename_placeholder(connection: Connection, thread_id: str) -> int:
    """The turn is written: a conversation node (in any project) still carrying the placeholder
    is named from its first question. Never raises; returns how many were renamed."""
    try:
        with connection.begin_nested():
            named = proper_names(connection, [thread_id]).get(thread_id)
            if not named:
                return 0
            rows = connection.execute(text(
                "SELECT id, name FROM kg_nodes WHERE kind = 'run' AND node_key = :k"),
                {"k": f"run:{thread_id}"}).mappings().all()
            stale = [r["id"] for r in rows if is_placeholder(r["name"])]
            if stale:
                connection.execute(text("UPDATE kg_nodes SET name = :n WHERE id = ANY(:i)"),
                                   {"n": named, "i": stale})
            return len(stale)
    except Exception:  # noqa: BLE001 — a name is a label, never a reason to fail the turn
        LOG.info("could not name the conversation %s", thread_id, exc_info=True)
        return 0


def mend_names(connection: Connection, body: Any) -> Any:
    """REPAIRED ON READ, never written: in a view response, at any depth, a conversation still
    named by its placeholder — a node (KgViewNode, a tree row: ``kind`` run, ``key``
    ``run:athr_…``, ``label``) or a use naming it (``run_id`` athr_…, ``run_title``) — is shown
    under its question's name. Writes nothing (the backfill persists it); returns ``body``."""
    nodes: list[tuple[dict, str]] = []
    uses: list[tuple[dict, str]] = []

    def walk(v: Any) -> None:
        if isinstance(v, list):
            for x in v:
                walk(x)
            return
        if not isinstance(v, dict):
            return
        key = str(v.get("key") or "")
        if v.get("kind") == "run" and key.startswith(f"run:{THREAD_PREFIX}") and is_placeholder(v.get("label")):
            nodes.append((v, key.split(":", 1)[1]))
        if is_conversation(v.get("run_id")) and "run_title" in v and (
                not v.get("run_title") or is_placeholder(v.get("run_title"))):
            uses.append((v, str(v["run_id"])))
        for x in v.values():
            if isinstance(x, (dict, list)):
                walk(x)

    walk(body)
    if not nodes and not uses:
        return body
    try:
        with connection.begin_nested():
            names = proper_names(connection, [t for _, t in nodes + uses])
    except Exception:  # noqa: BLE001 — a name is a label, never a reason to fail a read
        LOG.info("could not name conversations on read", exc_info=True)
        return body
    for payload, thread in nodes:
        if thread in names:
            payload["label"] = names[thread]
    for payload, thread in uses:
        if thread in names:
            payload["run_title"] = names[thread].rsplit(" · ", 1)[0]
    return body


def speaker_of(connection: Connection, project_id: str, role: Optional[str]) -> dict[str, Any]:
    """The addressed agent (agent_identity.addressed), never raising: BOSS itself on any doubt."""
    from ..agent_identity import addressed
    from ..llm_telemetry import GENERAL_NAME, GENERAL_ROLE

    try:
        with connection.begin_nested():
            return addressed(connection, project_id, role)
    except Exception:  # noqa: BLE001 — a name is a label, never a reason to refuse
        return {"addressed": False, "agent_role": GENERAL_ROLE, "agent_name": GENERAL_NAME,
                "agent_id": None, "fingerprint": None}


def anchor(connection: Connection, project_id: str, thread_id: str) -> str:
    """The conversation's node (``run:<thread id>``, subtype conversation) in the project whose
    knowledge was used or added; an existing one is kept. The conversation may be held in another
    project (an agent asked in one project reading another's graph): its node says which, and
    its agent is the one addressed there. A thread that does not exist is refused."""
    from .learning_writeback import _STORE

    key = f"run:{thread_id}"
    found = _STORE.find_by_key(connection, project_id=project_id, kind="run", node_key=key)
    if found is not None:
        return found["id"]
    row = thread_row(connection, None, thread_id)
    if row is None:
        raise LookupError(f"conversation not found: {thread_id}")
    who = speaker_of(connection, row["project_id"], row.get("agent_role"))
    attrs = {"source_id": thread_id, "subtype": SUBTYPE, "thread_id": thread_id,
             "thread_project_id": row["project_id"], "surface": row.get("surface"), "agent_role": who["agent_role"], "agent_name": who["agent_name"],
             "agent_id": who.get("agent_id"), "started_at": _iso(row.get("created_at"))}
    from ..llm_telemetry import get_llm_context

    ctx = get_llm_context()
    # The turn's own question, when this is that turn's conversation (see title_of).
    question = ctx.get("agent_thread_question") if ctx.get("agent_thread_id") == thread_id else None
    name = node_name(title_of(connection, thread_id, question=question), row.get("created_at"))
    return _STORE.upsert_node(connection, project_id=project_id, kind="run", node_key=key,
                              name=name, body="", trust_tier="observed",
                              actor_kind="system", source="learning", attrs=attrs)


# --- the live path --------------------------------------------------------------------------------
def result_refs(tool: Any, result: Any) -> list[str]:
    """The rule and learning ids a knowledge read tool returned, for the transcript (so a later
    reader — or a backfill — can see what the turn was given, not only that it asked)."""
    if str(tool or "") not in KNOWLEDGE_READ_TOOLS or not isinstance(result, Mapping):
        return []
    ids: list[Any] = []
    ids += list(result.get("cited_fact_ids") or [])
    ids += [r.get("id") for r in result.get("rules") or [] if isinstance(r, Mapping)]
    ids += [h.get("node_id") for h in result.get("why") or [] if isinstance(h, Mapping)]
    ids += [h.get("node_id") for h in result.get("hits") or [] if isinstance(h, Mapping)]
    return [str(i) for i in dict.fromkeys(ids) if str(i or "").startswith("kgn_")][:MAX_REFS]


def stamp_message(connection: Connection, *, thread_id: Optional[str], message_id: Optional[str],
                  act_id: Optional[str] = None) -> int:
    """Name the message that answered on the turn's uses (logged before it existed). Turns in a
    thread are serial, so a use of the thread with no message yet is this turn's."""
    if not thread_id or not message_id:
        return 0
    try:
        with connection.begin_nested():
            return int(connection.execute(text(
                "UPDATE kg_usage SET message_id = :m WHERE agent_thread_id = :t AND message_id IS NULL "
                "AND (CAST(:a AS TEXT) IS NULL OR act_id IS NULL OR act_id = :a)"),
                {"m": message_id, "t": thread_id, "a": act_id}).rowcount or 0)
    except Exception:  # noqa: BLE001 — a label on a use, never a reason to fail the turn
        LOG.info("could not stamp the uses of %s", thread_id, exc_info=True)
        return 0


def turn_done(connection: Connection, *, project_id: str, thread_id: str, act_id: Optional[str],
              message_id: Optional[str], reply: Any, tool_calls: Sequence[Mapping[str, Any]] = (),
              agent_role: Optional[str] = None) -> list[str]:
    """A chat turn's reply is written: stamp its uses with the message, and what the reply (or
    what the agent wrote into a tool call) cites as ``[L:<id>]`` / ``[R:<id>]`` is ``cited`` (a cited
    precedent or fix ``reused``). Never raises; returns the ids cited."""
    from .learning_writeback import citations, log_use

    stamp_message(connection, thread_id=thread_id, message_id=message_id, act_id=act_id)
    # A node a tool anchored before the question was readable is named now (rename_placeholder).
    rename_placeholder(connection, thread_id)
    written = [c.get("args") for c in tool_calls or [] if isinstance(c, Mapping) and c.get("ok") is not False]
    cited = citations(reply, written)
    if not cited:
        return []
    try:
        with connection.begin_nested():
            return log_use(connection, project_id=project_id, ids=cited, use_kind="cited", step="chat",
                           agent_role=agent_role, thread_id=thread_id, act_id=act_id, message_id=message_id)
    except Exception:  # noqa: BLE001 — knowledge is an aid; a turn never fails for it
        LOG.info("could not log the citations of %s", thread_id, exc_info=True)
        return []


# --- the proof (§4.6) -----------------------------------------------------------------------------
def scorecard(connection: Connection, project_id: str, thread_id: str, *,
              consulted: list[dict[str, Any]], reused: list[dict[str, Any]], learned: int) -> dict[str, Any]:
    """A conversation's KgRunProof: its agent's tool calls (``steps``) and replies (``turns``),
    tokens and cost when the turn's model calls were metered, wall time, and the time from the
    question to the root cause it recorded. ``consulted`` / ``reused`` / ``learned`` are read by
    learning_live.run_proof, the same reader as for a run."""
    from .runtime import knowledge_mode

    row = thread_row(connection, None, thread_id)
    if row is None or (row["project_id"] != project_id and not connection.execute(text(
            "SELECT 1 FROM kg_nodes WHERE project_id = :p AND kind = 'run' AND node_key = :k"),
            {"p": project_id, "k": f"run:{thread_id}"}).first()):
        raise LookupError(f"conversation not found: {thread_id}")
    messages = [dict(m) for m in connection.execute(text(
        "SELECT id, role, agent_role, content, tool_calls, act_id, created_at FROM agent_messages "
        "WHERE thread_id = :t ORDER BY created_at, id"), {"t": thread_id}).mappings()]
    asked = [m for m in messages if m["role"] == "user"]
    replies = [m for m in messages if m["role"] == "assistant" and not _is_press(m)]
    calls = [c for m in replies for c in (_jsonish(m["tool_calls"]) or []) if isinstance(c, Mapping)]
    knowledge_calls = [{"tool": c.get("tool"), "ok": c.get("ok") is not False,
                        "query": (c.get("args") or {}).get("query") if isinstance(c.get("args"), Mapping) else None,
                        "refs": list(c.get("refs") or [])}
                       for c in calls if str(c.get("tool") or "") in KNOWLEDGE_READ_TOOLS]
    acts = [a for a in dict.fromkeys(m["act_id"] for m in messages if m.get("act_id"))]
    metered = connection.execute(text(
        "SELECT count(*) AS n, coalesce(sum(total_tokens), 0) AS tokens, coalesce(sum(cost_usd_micros), 0) AS cost "
        "FROM llm_calls WHERE act_id = ANY(:a)"), {"a": acts}).mappings().one() if acts else {"n": 0, "tokens": 0, "cost": 0}
    signed = connection.execute(text(
        "SELECT coalesce(sum(tokens_in + tokens_out), 0) AS tokens, coalesce(sum(cost_usd), 0) AS cost "
        "FROM agent_acts WHERE act_id = ANY(:a)"), {"a": acts}).mappings().one() if acts else {"tokens": 0, "cost": 0}
    tokens = max(int(metered["tokens"] or 0), int(signed["tokens"] or 0))
    cost = max(int(metered["cost"] or 0) / 1e6, float(signed["cost"] or 0))
    produced = [dict(r) for r in connection.execute(text(
        """
        SELECT d.id, d.kind, d.first_seen FROM kg_edges e JOIN kg_nodes s ON s.id = e.src_id
        JOIN kg_nodes d ON d.id = e.dst_id
        WHERE e.project_id = :p AND e.kind = 'produced' AND s.node_key = :k ORDER BY d.first_seen
        """), {"p": project_id, "k": f"run:{thread_id}"}).mappings()]
    start = asked[0]["created_at"] if asked else row["created_at"]
    stamps = [m["created_at"] for m in messages] + [p["first_seen"] for p in produced if p["first_seen"]]
    end = max(stamps) if stamps else None
    cause = next((p["first_seen"] for p in produced if p["kind"] == "root_cause"), None) or \
        next((p["first_seen"] for p in produced if p["kind"] in ("investigation", "precedent")), None)
    who = speaker_of(connection, row["project_id"], row.get("agent_role"))
    return {
        "run_id": thread_id,
        "kind": SUBTYPE,
        "title": title_of(connection, thread_id),
        "agent_role": who["agent_role"],
        "agent_name": who["agent_name"],
        "started_at": _iso(start),
        "status": row.get("status"),
        "knowledge_graph": knowledge_mode(connection, project_id),
        "steps": len(calls),
        "turns": len(replies),
        "tokens": tokens,
        "cost_usd": round(cost, 4),
        # The chat engines do not meter every model call yet: 0 tokens is "not measured", said so.
        "tokens_measured": bool(tokens or int(metered["n"] or 0)),
        "wall_seconds": round((end - start).total_seconds(), 1) if start and end else None,
        "time_to_root_cause_seconds": round((cause - start).total_seconds(), 1) if start and cause else None,
        "repair_rounds": 0,
        "learned": learned,
        "tool_calls": len(calls),
        "knowledge_calls": knowledge_calls,
        "consulted": consulted,
        "reused": reused,
    }


#: Who asks through the console or Metatron: the signed-in operator, never named by this id.
CONSOLE_ACTORS = frozenset({"executive_console", "metatron", "coo"})
_KGN = re.compile(r"kgn_[A-Za-z0-9]+")


def asked_by(connection: Connection, project_id: str, actor: Any) -> dict[str, Any]:
    """Who opened the conversation, in words: ``console`` (the console's or Metatron's operator:
    the reader names the signed-in person), a ``person`` or ``service`` by display name, or
    ``unknown``. Never the raw actor id as a name."""
    actor = str(actor or "")
    if actor in CONSOLE_ACTORS:
        return {"kind": "console", "name": None}
    name = None
    kind = "person"
    try:
        with connection.begin_nested():
            row = connection.execute(text(
                "SELECT kind, display_name FROM identity_principals WHERE id = :a"), {"a": actor}).mappings().first()
            if row and row["display_name"]:
                name = str(row["display_name"])
                kind = "service" if str(row["kind"] or "") not in ("user", "person", "human") else "person"
    except Exception:  # noqa: BLE001 — an install without principals: the member list names them
        pass
    if name is None:
        try:
            with connection.begin_nested():
                row = connection.execute(text(
                    "SELECT member_type, display_name FROM project_members WHERE project_id = :p AND member_id = :a"),
                    {"p": project_id, "a": actor}).mappings().first()
            if row and row["member_type"] == "system":
                return {"kind": "console", "name": None}
            if row and row["display_name"]:
                name = str(row["display_name"])
        except Exception:  # noqa: BLE001
            pass
    return {"kind": kind if name else "unknown", "name": name}


def conversation_knowledge(connection: Connection, project_id: str, thread_id: str, *,
                           withheld: Iterable[str] = ()) -> dict[str, Any]:
    """What one conversation wrote to and took from the graph, for the console's conversation view
    (read-only): its header (title, agent, who asked, when), the learnings it ``produced``, the
    EARLIER items it consulted, cited or reused (not its own), and the title of every graph item
    its transcript names — so an ``[L:kgn_…]`` or a quoted id is shown by name, never as an id.
    Items of a kind the reader may not see are left out (``withheld``)."""
    from .learning_live import _used
    from .learning_writeback import LEARNING_KINDS

    thread_id = str(thread_id or "").strip()
    row = thread_row(connection, None, thread_id) if is_conversation(thread_id) else None
    if row is None or (row["project_id"] != project_id and not connection.execute(text(
            "SELECT 1 FROM kg_nodes WHERE project_id = :p AND kind = 'run' AND node_key = :k"),
            {"p": project_id, "k": f"run:{thread_id}"}).first()):
        raise LookupError(f"conversation not found: {thread_id}")
    hidden = frozenset(withheld)
    key = f"run:{thread_id}"
    produced = [dict(r) for r in connection.execute(text(
        """
        SELECT d.id, d.kind, d.name AS title, d.first_seen FROM kg_edges e
        JOIN kg_nodes s ON s.id = e.src_id JOIN kg_nodes d ON d.id = e.dst_id
        WHERE e.project_id = :p AND e.kind = 'produced' AND s.node_key = :k AND e.valid_to IS NULL
        ORDER BY d.first_seen, d.id
        """), {"p": project_id, "k": key}).mappings()]
    produced = [{"id": p["id"], "kind": p["kind"], "title": p["title"]} for p in produced
                if p["kind"] not in hidden]
    mine = {p["id"] for p in produced}
    consulted, reused, _ = _used(connection, project_id, [key], hidden)
    used: dict[str, dict[str, Any]] = {}
    for edge, item in [("reused", i) for i in reused] + [("consulted", i) for i in consulted]:
        if item["id"] in mine:
            continue
        seen = used.setdefault(item["id"], {**item, "hows": []})
        how = "reused" if edge == "reused" else ("cited" if item.get("how") == "cited" else "consulted")
        if how not in seen["hows"]:
            seen["hows"].append(how)
    raw = connection.execute(text(
        "SELECT role, agent_role, content, tool_calls FROM agent_messages WHERE thread_id = :t "
        "ORDER BY created_at, id"), {"t": thread_id}).mappings().all()
    ids: list[str] = []
    for message in raw:
        ids += _KGN.findall(str(message["content"] or ""))
        ids += _KGN.findall(json.dumps(_jsonish(message["tool_calls"]) or [], default=str))
    ids = list(dict.fromkeys(ids + [p["id"] for p in produced] + list(used)))
    names: dict[str, dict[str, Any]] = {}
    if ids:
        for n in connection.execute(text(
                "SELECT id, kind, name FROM kg_nodes WHERE project_id = :p AND id = ANY(:i)"),
                {"p": project_id, "i": ids}).mappings():
            if n["kind"] not in hidden:
                names[n["id"]] = {"title": n["name"], "kind": n["kind"]}
    who = speaker_of(connection, row["project_id"], row.get("agent_role"))
    actor = connection.execute(text("SELECT actor FROM agent_threads WHERE id = :t"), {"t": thread_id}).scalar()
    learning = sorted(LEARNING_KINDS)
    return {
        "thread_id": thread_id,
        "title": title_of(connection, thread_id),
        "started_at": _iso(row.get("created_at")),
        "agent": {"role": who["agent_role"], "name": who["agent_name"], "agent_id": who.get("agent_id"),
                  "addressed": bool(who.get("addressed"))},
        "asked_by": asked_by(connection, row["project_id"], actor),
        "produced": produced,
        "used": list(used.values()),
        "names": names,
        "learned": sum(1 for p in produced if p["kind"] in learning),
        "used_earlier": len(used),
    }


def _is_press(message: Mapping[str, Any]) -> bool:
    """A press outcome (record_action_outcome): the operator's click, not an agent's reply."""
    content = str(message.get("content") or "")
    return not message.get("agent_role") and content.startswith(("✓ The operator pressed", "⚠ The operator pressed"))


# --- backfill (one-time, idempotent, from the transcript only) ------------------------------------
def backfill(connection: Connection, project_id: str, *, apply: bool = False,
             thread_ids: Optional[Sequence[str]] = None) -> dict[str, Any]:
    """Derive the usage rows, ``produced`` edges and blank provenance of conversations that
    predate conversation anchors, from evidence their transcript holds — and nothing else:

    - ``produced``: a press outcome of ``record_learning`` / ``kg_propose_rule`` that SUCCEEDED and
      quotes the id it wrote (``"learning": {"id": "kgn_…"``), for an item of this project; a
      learning whose provenance names no agent — or names BOSS for a press in a thread addressed
      to an agent, with no run (the press did not carry the speaker then) — is credited to the
      conversation's addressed agent (BOSS itself when none was addressed), marked ``backfilled``.
    - ``consulted``: a stored knowledge tool call that succeeded and kept the ids it returned
      (``refs``). A call stored without them proves nothing about what it returned and is
      reported, not guessed.
    - ``cited``: an ``[L:<id>]`` / ``[R:<id>]`` in an agent reply or in what it wrote into a call.

    - ``renamed``: a conversation node still named by its thread id, named from its first question.

    Idempotent (an existing row for the same thread, message, item and kind is kept; edges are
    unique). ``apply=False`` (the default) reports what it would write and writes nothing."""
    from .learning_writeback import LEARNING_KINDS, citations, sync_usage_edges

    if not connection.execute(text("SELECT 1 FROM projects WHERE id = :p"), {"p": project_id}).first():
        raise LookupError(f"project not found: {project_id}")
    params: dict[str, Any] = {"p": project_id}
    where = "project_id = :p"
    if thread_ids:
        where += " AND id = ANY(:t)"
        params["t"] = list(thread_ids)
    threads = [dict(r) for r in connection.execute(text(
        f"SELECT id, agent_role, created_at FROM agent_threads WHERE {where} ORDER BY created_at"), params).mappings()]
    report: dict[str, Any] = {"project_id": project_id, "applied": apply, "threads": 0, "produced": [],
                              "provenance_filled": [], "consulted": [], "cited": [], "unprovable": [],
                              "renamed": []}
    savepoint = connection.begin_nested()
    try:
        # renamed: a conversation node of this project still named by its id (anchored before
        # its question was readable), named from that question.
        stale = [dict(r) for r in connection.execute(text(
            "SELECT id, node_key, name FROM kg_nodes WHERE project_id = :p AND kind = 'run' "
            "AND node_key LIKE :k"), {"p": project_id, "k": f"run:{THREAD_PREFIX}%"}).mappings()
            if is_placeholder(r["name"])]
        if thread_ids:
            stale = [r for r in stale if r["node_key"].split(":", 1)[1] in set(thread_ids)]
        names = proper_names(connection, [r["node_key"].split(":", 1)[1] for r in stale])
        for r in stale:
            named = names.get(r["node_key"].split(":", 1)[1])
            if named:
                connection.execute(text("UPDATE kg_nodes SET name = :n WHERE id = :i"), {"n": named, "i": r["id"]})
                report["renamed"].append({"node_id": r["id"], "from": r["name"], "to": named})
        for thread in threads:
            touched = False
            who = speaker_of(connection, project_id, thread.get("agent_role"))
            rows = connection.execute(text(
                "SELECT id, role, agent_role, content, tool_calls, act_id, created_at FROM agent_messages "
                "WHERE thread_id = :t AND role = 'assistant' ORDER BY created_at, id"),
                {"t": thread["id"]}).mappings().all()
            for message in rows:
                calls = [c for c in (_jsonish(message["tool_calls"]) or []) if isinstance(c, Mapping)]
                # produced: what a pressed write said it wrote.
                if _is_press(message) and any(str(c.get("tool")) in KNOWLEDGE_WRITE_TOOLS and c.get("ok") for c in calls):
                    for match in _WROTE.finditer(str(message["content"] or "")):
                        if _produced(connection, project_id, thread, message, match.group("id"), who, report,
                                     kinds=LEARNING_KINDS):
                            touched = True
                # consulted: a stored read result's ids, never a call without them.
                for call in calls:
                    tool = str(call.get("tool") or "")
                    if tool not in KNOWLEDGE_READ_TOOLS or call.get("ok") is False:
                        continue
                    refs = [str(r) for r in call.get("refs") or [] if str(r or "").startswith("kgn_")]
                    if not refs:
                        report["unprovable"].append({"thread_id": thread["id"], "message_id": message["id"],
                                                     "tool": tool, "why": "the stored call kept no result ids"})
                        continue
                    if _use(connection, project_id, thread, message, refs, "consulted", who, report):
                        touched = True
                # cited: the agent's own words.
                if not _is_press(message):
                    cited = citations(message["content"], [c.get("args") for c in calls if c.get("ok") is not False])
                    if cited and _use(connection, project_id, thread, message, cited, "cited", who, report):
                        touched = True
            if touched:
                report["threads"] += 1
                sync_usage_edges(connection, project_id=project_id, run_ref=thread["id"])
        if apply:
            savepoint.commit()
            from .age import after_write

            after_write(connection, project_id)
        else:
            savepoint.rollback()
    except Exception:
        savepoint.rollback()
        raise
    return report


def _produced(connection: Connection, project_id: str, thread: Mapping[str, Any], message: Mapping[str, Any],
              node_id: str, who: Mapping[str, Any], report: dict, *, kinds: Iterable[str]) -> bool:
    from ..llm_telemetry import GENERAL_ROLE
    from .learning_writeback import _STORE, _attrs

    node = connection.execute(text(
        "SELECT id, kind, attrs, first_seen FROM kg_nodes WHERE id = :i AND project_id = :p"),
        {"i": node_id, "p": project_id}).mappings().first()
    if node is None or (node["kind"] not in set(kinds) and node["kind"] != "rule"):
        return False
    conv = anchor(connection, project_id, thread["id"])
    at = _iso(node["first_seen"]) or _iso(message["created_at"])
    exists = connection.execute(text(
        "SELECT 1 FROM kg_edges WHERE project_id = :p AND src_id = :s AND dst_id = :d AND kind = 'produced'"),
        {"p": project_id, "s": conv, "d": node_id}).first()
    if not exists:
        _STORE.upsert_edge(connection, project_id=project_id, src_id=conv, dst_id=node_id, kind="produced",
                           actor_kind="system",
                           attrs={"step": "chat", "at": at, "run_id": thread["id"], "message_id": message["id"],
                                  "backfilled": True})
        report["produced"].append({"thread_id": thread["id"], "message_id": message["id"], "node_id": node_id})
    attrs = _attrs(node["attrs"])
    prov = _attrs(attrs.get("provenance"))
    blank = not (prov.get("agent_role") or prov.get("agent_name"))
    # A press credited "BOSS (general)" because the press did not carry the thread's speaker
    # (fixed in agent_identity.speaking): in a thread addressed to an agent, with no run named,
    # the transcript shows whose conversation it was.
    miscredited = (prov.get("agent_role") == GENERAL_ROLE and who["agent_role"] != GENERAL_ROLE
                   and not any(prov.get(k) for k in ("run_id", "workflow_run_id", "code_task_run_id")))
    if node["kind"] != "rule" and (blank or miscredited):
        filled = {**prov, "agent_role": who["agent_role"], "agent_name": who["agent_name"],
                  "agent_id": who.get("agent_id"), "agent_thread_id": thread["id"], "message_id": message["id"],
                  "step": prov.get("step") or "chat", "backfilled": True,
                  **({"credited_before": prov.get("agent_name")} if miscredited else {})}
        connection.execute(text(
            "UPDATE kg_nodes SET attrs = jsonb_set(attrs, '{provenance}', CAST(:v AS JSONB), true), "
            "created_by = CASE WHEN :r <> :g THEN :r ELSE created_by END WHERE id = :i"),
            {"v": json.dumps(filled), "i": node_id, "r": who["agent_role"], "g": GENERAL_ROLE})
        if who["agent_role"] and who["agent_role"] != GENERAL_ROLE:
            agent = connection.execute(text(
                "SELECT id FROM kg_nodes WHERE project_id = :p AND kind = 'agent' AND node_key = :k"),
                {"p": project_id, "k": f"agent:{who['agent_role']}"}).scalar()
            if agent is None:
                agent = _STORE.upsert_node(connection, project_id=project_id, kind="agent",
                                           node_key=f"agent:{who['agent_role']}", name=who["agent_name"] or who["agent_role"],
                                           body="", trust_tier="observed", actor_kind="system", source="learning",
                                           attrs={"role_key": who["agent_role"]})
            _STORE.upsert_edge(connection, project_id=project_id, src_id=node_id, dst_id=str(agent), kind="proposed_by",
                               actor_kind="system", attrs={"step": "chat", "at": at, "run_id": thread["id"]})
        report["provenance_filled"].append({"node_id": node_id, "agent_name": who["agent_name"]})
    return True


def _use(connection: Connection, project_id: str, thread: Mapping[str, Any], message: Mapping[str, Any],
         ids: Sequence[str], use_kind: str, who: Mapping[str, Any], report: dict) -> bool:
    from .learning_writeback import LEARNING_KINDS, _STORE

    kinds = {str(r["id"]): str(r["kind"]) for r in connection.execute(text(
        "SELECT id, kind FROM kg_nodes WHERE project_id = :p AND id = ANY(:i) AND (kind = 'rule' OR kind = ANY(:k))"),
        {"p": project_id, "i": list(ids), "k": sorted(LEARNING_KINDS)}).mappings()}
    found = [i for i in dict.fromkeys(ids) if i in kinds]
    wrote = False
    plan = [(i, use_kind) for i in found]
    if use_kind == "cited":
        plan += [(i, "reused") for i in found if kinds.get(i) in ("precedent", "fix")]
    for fact_id, kind in plan:
        if connection.execute(text(
                "SELECT 1 FROM kg_usage WHERE project_id = :p AND agent_thread_id = :t AND message_id = :m "
                "AND fact_id = :f AND use_kind = :k"),
                {"p": project_id, "t": thread["id"], "m": message["id"], "f": fact_id, "k": kind}).first():
            continue
        _STORE.log_usage(connection, project_id=project_id, run_id=thread["id"], step="chat", fact_ids=[fact_id],
                         use_kind=kind, agent_role=who["agent_role"], agent_thread_id=thread["id"],
                         act_id=message.get("act_id"), message_id=message["id"], at=message["created_at"])
        report[use_kind if use_kind != "reused" else "cited"].append(
            {"thread_id": thread["id"], "message_id": message["id"], "node_id": fact_id, "use_kind": kind})
        wrote = True
    return wrote
