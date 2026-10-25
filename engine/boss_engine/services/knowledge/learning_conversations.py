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


def title_of(connection: Connection, thread_id: str) -> str:
    """The conversation's name: the first line of its first question ("B4 · Incident: early
    repayments post more than the customer paid"), without the Markdown around it."""
    first = connection.execute(text(
        "SELECT content FROM agent_messages WHERE thread_id = :t AND role = 'user' "
        "ORDER BY created_at, id LIMIT 1"), {"t": thread_id}).scalar()
    for line in str(first or "").splitlines():
        cleaned = _HEADING.sub("", line).strip().strip("*_` ")
        if cleaned:
            return cleaned[:MAX_TITLE]
    return f"Conversation {thread_id}"


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
    started = row.get("created_at")
    name = title_of(connection, thread_id)
    if started is not None:          # as a run is named (learning_writeback.run_title)
        name = f"{name if len(name) <= 90 else name[:89].rstrip() + '…'} · {started:%b %d, %H:%M}"
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
                              "provenance_filled": [], "consulted": [], "cited": [], "unprovable": []}
    savepoint = connection.begin_nested()
    try:
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
