"""Conversations as provenance anchors (BOSSAPP-768 S2c, KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §2.5, §4.6).

The banking case study's proof — "learned by agent X in A, used by agent Y in B" — was made in
chat: an incident sent to the Site Reliability Engineer called kg_precedent and kg_rules, found
the root cause, and a person pressed "Record learning". Nothing of it reached the graph's usage:
log_use needed a run, so a chat turn logged nothing, and the press credited "BOSS (general)".

Breaks covered: a knowledge tool in a chat turn logging no usage, or logging it without the
addressed agent; a reply's [L:…] citation not marking cited / reused, or not naming its message;
a press crediting BOSS rather than the thread's agent; a conversation's learning hanging on no
anchor; consulted / reused edges not derived from conversation rows (live and again by sync); the
proof refusing a conversation or not carrying the earlier conversation's precedent; a backfill
that invents a use the transcript does not show, writes on a dry run, or writes twice.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager

import pytest
from boss_engine.db import ensure_schema
from boss_engine.services import agent_identity
from boss_engine.services.knowledge import learning_conversations as lc
from boss_engine.services.knowledge import learning_live
from boss_engine.services.knowledge import learning_writeback as lw
from boss_engine.services.llm_telemetry import clear_llm_context, get_llm_context
from boss_engine.tools import knowledge_tools
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

P = "proj_kgc"
EARLIER = "athr_kgc_inc2041"
LATER = "athr_kgc_inc2088"


def _database_url() -> str:
    return os.environ.get("BOSS_KG_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""


@pytest.fixture(scope="module")
def pg_engine():
    url = _database_url()
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if not url or not any(marker in name for marker in ("test", "testing", "ci", "scratch", "tmp")):
        pytest.skip("BOSS_KG_TEST_DATABASE_URL must name a disposable database")
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        pytest.skip(f"disposable postgres is not reachable: {exc.__class__.__name__}")
    ensure_schema(engine)
    return engine


def _thread(conn, thread_id: str, role: str, question: str, at: str) -> None:
    conn.execute(text(
        "INSERT INTO agent_threads (id, project_id, actor, surface, agent_role, created_at) "
        "VALUES (:t, :p, 'ceo', 'chat', :r, CAST(:at AS TIMESTAMPTZ))"),
        {"t": thread_id, "p": P, "r": role, "at": at})
    _message(conn, thread_id, "user", question, at)


def _message(conn, thread_id: str, role: str, content: str, at: str, *, agent_role: str = "",
             tool_calls=(), act_id=None) -> str:
    mid = f"amsg_{thread_id[-6:]}_{abs(hash((content, at))) % 10**8}"
    conn.execute(text(
        "INSERT INTO agent_messages (id, thread_id, role, agent_role, content, tool_calls, act_id, created_at) "
        "VALUES (:i, :t, :r, :ar, :c, CAST(:tc AS JSONB), :a, CAST(:at AS TIMESTAMPTZ))"),
        {"i": mid, "t": thread_id, "r": role, "ar": agent_role, "c": content, "tc": json.dumps(list(tool_calls)),
         "a": act_id, "at": at})
    return mid


@pytest.fixture()
def connection(pg_engine):
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text("INSERT INTO orgs (id, name) VALUES ('org_kgc', 'KGC')"))
            conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_kgc', 'org_kgc', 'KGC')"))
            conn.execute(text(f"INSERT INTO projects (id, workspace_id, name, slug) VALUES ('{P}', 'ws_kgc', 'KGC', 'kgc')"))
            conn.execute(text(
                "INSERT INTO roles (id, project_id, role_key, display_name, agent_id, spec) VALUES "
                f"('role_kgc_sre', '{P}', 'sre', 'Site Reliability Engineer', 'agt_kgc_sre', '{{}}'::jsonb)"))
            _thread(conn, EARLIER, "", "# B3 · Incident: repayments post more than the customer paid\n\nINC-2041",
                    "2026-10-25T05:19:38Z")
            _thread(conn, LATER, "sre", "# B4 · Incident: early repayments post more than the customer paid",
                    "2026-10-25T07:13:13Z")
            clear_llm_context()
            yield conn
        finally:
            transaction.rollback()
            clear_llm_context()


class _Ctx:
    """An EngineContext whose tool transactions are savepoints of the test's connection."""

    def __init__(self, connection):
        self._c = connection

    @contextmanager
    def begin(self):
        with self._c.begin_nested():
            yield self._c


def _precedent(connection, statement="An allocator that skips the remaining-amount decrement overpays later components"):
    return lw.record(connection, project_id=P, learning_kind="precedent",
                     title="Allocator skips the remaining-amount decrement", statement=statement,
                     detail={"symptom": "repayment posts more than was paid", "cause_summary": "missing minus",
                             "fix_summary": "decrement after each component", "applies_to": ["allocators"],
                             "reuse_count": 0},
                     about={}, evidence=[{"type": "file", "ref": "Allocator.java:159", "excerpt": "minus missing"}],
                     confidence=0.75)["node"]["id"]


def _in(connection, thread_id, act_id=None):
    return agent_identity.speaking(agent_identity.addressed_in_thread(connection, P, thread_id),
                                   thread_id=thread_id, act_id=act_id)


def _usage(connection, **where):
    sql = "SELECT * FROM kg_usage WHERE project_id = :p" + "".join(f" AND {k} = :{k}" for k in where)
    return [dict(r) for r in connection.execute(text(sql + " ORDER BY created_at, use_kind"), {"p": P, **where}).mappings()]


def _edges(connection, src_key, kind):
    return [dict(r) for r in connection.execute(text(
        "SELECT e.dst_id, e.attrs FROM kg_edges e JOIN kg_nodes s ON s.id = e.src_id "
        "WHERE e.project_id = :p AND s.node_key = :k AND e.kind = :kind"),
        {"p": P, "k": src_key, "kind": kind}).mappings()]


# --- a learning recorded in a conversation, by its addressed agent ---------------------------------
def test_a_press_in_a_thread_credits_the_threads_agent_and_hangs_on_the_conversation(connection):
    with _in(connection, LATER):
        node_id = _precedent(connection)
    item = lw.item_payload(connection, P, node_id)
    assert item["provenance"]["agent_name"] == "Site Reliability Engineer"
    assert item["provenance"]["agent_role"] == "sre" and item["provenance"]["agent_id"] == "agt_kgc_sre"
    anchor = connection.execute(text(
        "SELECT kind, name, attrs FROM kg_nodes WHERE project_id = :p AND node_key = :k"),
        {"p": P, "k": f"run:{LATER}"}).mappings().one()
    assert anchor["kind"] == "run" and anchor["attrs"]["subtype"] == "conversation"
    assert anchor["name"] == "B4 · Incident: early repayments post more than the customer paid · Oct 25, 07:13"
    from boss_engine.services.knowledge.view import node_payload

    assert node_payload({"id": "x", "kind": "run", "name": anchor["name"], "attrs": anchor["attrs"]})["sub"] == \
        "Conversation with Site Reliability Engineer"
    assert anchor["attrs"]["agent_name"] == "Site Reliability Engineer"
    assert [e["dst_id"] for e in _edges(connection, f"run:{LATER}", "produced")] == [node_id]
    # Restored after: nothing recorded later is credited to the thread's agent by accident.
    assert "speaker_role" not in get_llm_context() and "agent_thread_id" not in get_llm_context()


def test_a_press_in_a_thread_addressed_to_nobody_says_boss_never_blank(connection):
    with _in(connection, EARLIER):
        node_id = _precedent(connection)
    assert lw.item_payload(connection, P, node_id)["provenance"]["agent_name"] == "BOSS (general)"
    assert [e["dst_id"] for e in _edges(connection, f"run:{EARLIER}", "produced")] == [node_id]


def test_the_console_press_carries_the_threads_speaker(connection):
    """Every press path (console, Metatron, the CLI) wraps its invoke in the thread's speaker."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for path in (root / "apps" / "api" / "app" / "main.py", root / "engine" / "boss_engine" / "__main__.py"):
        body = path.read_text(encoding="utf-8")
        assert "agent_identity.addressed_in_thread(" in body, path


# --- usage from a chat turn ------------------------------------------------------------------------
def test_a_knowledge_tool_in_a_chat_turn_logs_consulted_with_the_agent_and_draws_the_edge(connection):
    with _in(connection, EARLIER):
        precedent = _precedent(connection)
    with _in(connection, LATER, act_id="act_kgc_1"):
        found = knowledge_tools._precedent(_Ctx(connection), project_id=P, actor="ceo",
                                           args={"query": "remaining-amount decrement allocator"})
    assert precedent in found["cited_fact_ids"]
    rows = _usage(connection, agent_thread_id=LATER)
    assert [(r["fact_id"], r["use_kind"], r["agent_role"], r["act_id"], r["run_id"]) for r in rows] == \
        [(precedent, "consulted", "sre", "act_kgc_1", LATER)]
    (edge,) = _edges(connection, f"run:{LATER}", "consulted")
    assert edge["dst_id"] == precedent and edge["attrs"]["how"] == "tool"
    assert edge["attrs"]["agent_name"] == "Site Reliability Engineer" and edge["attrs"]["conversation"] is True
    # The reply is written: the use names the message that answered.
    lc.turn_done(connection, project_id=P, thread_id=LATER, act_id="act_kgc_1", message_id="amsg_reply_1",
                 reply="Same class as before.", tool_calls=[], agent_role="sre")
    assert _usage(connection, agent_thread_id=LATER)[0]["message_id"] == "amsg_reply_1"


def test_a_reply_that_cites_a_precedent_marks_it_cited_and_reused(connection):
    with _in(connection, EARLIER):
        precedent = _precedent(connection)
    cited = lc.turn_done(connection, project_id=P, thread_id=LATER, act_id="act_kgc_2", message_id="amsg_reply_2",
                         reply=f"This is the INC-2041 class again [L:{precedent}] [L:kgn_notreal123456].",
                         tool_calls=[], agent_role="sre")
    assert cited == [precedent]
    kinds = sorted(r["use_kind"] for r in _usage(connection, agent_thread_id=LATER))
    assert kinds == ["cited", "reused"]
    assert [e["dst_id"] for e in _edges(connection, f"run:{LATER}", "reused")] == [precedent]
    (consulted,) = _edges(connection, f"run:{LATER}", "consulted")
    assert consulted["attrs"]["how"] == "cited"


def test_a_learning_recorded_in_a_conversation_that_cites_one_uses_it(connection):
    with _in(connection, EARLIER):
        precedent = _precedent(connection)
    with _in(connection, LATER):
        lw.record(connection, project_id=P, learning_kind="root_cause", title="Advance handler skips the decrement",
                  statement=f"Same class as [L:{precedent}]: the advance-payment branch never decrements.",
                  detail={}, about={}, evidence=[{"type": "file", "ref": "Adv.java:159", "excerpt": "no minus"}],
                  confidence=0.7)
    assert {r["use_kind"] for r in _usage(connection, agent_thread_id=LATER, fact_id=precedent)} == {"cited", "reused"}


def test_no_run_and_no_conversation_logs_nothing(connection):
    with _in(connection, EARLIER):
        precedent = _precedent(connection)
    assert lw.log_use(connection, project_id=P, ids=[precedent], use_kind="consulted") == []
    assert _usage(connection) == []


def test_sync_rederives_conversation_edges_idempotently(connection):
    with _in(connection, EARLIER):
        precedent = _precedent(connection)
    with _in(connection, LATER):
        lw.log_use(connection, project_id=P, ids=[precedent], use_kind="consulted")
    connection.execute(text("DELETE FROM kg_edges WHERE project_id = :p AND kind = 'consulted'"), {"p": P})
    assert lw.sync_usage_edges(connection, project_id=P) == 1
    assert lw.sync_usage_edges(connection, project_id=P) == 1
    assert len(_edges(connection, f"run:{LATER}", "consulted")) == 1


# --- the proof over two conversations --------------------------------------------------------------
def test_the_proof_compares_two_conversations_and_carries_the_earlier_precedent(connection):
    with _in(connection, EARLIER):
        precedent = _precedent(connection)
    _message(connection, EARLIER, "assistant", "found it", "2026-10-25T05:24:00Z",
             tool_calls=[{"tool": "kg_rules", "args": {"query": "allocation"}, "ok": True},
                         {"tool": "read_code", "args": {}, "ok": True}, {"tool": "search_code", "args": {}, "ok": True}])
    _message(connection, LATER, "assistant", "found it", "2026-10-25T07:17:00Z", agent_role="sre",
             tool_calls=[{"tool": "kg_precedent", "args": {"query": "early repayment"}, "ok": True,
                          "refs": [precedent]}])
    with _in(connection, LATER, act_id="act_kgc_3"):
        lw.log_use(connection, project_id=P, ids=[precedent], use_kind="consulted")
    later = learning_live.run_proof(connection, P, LATER)
    assert later["kind"] == "conversation" and later["agent_name"] == "Site Reliability Engineer"
    assert later["steps"] == 1 and later["turns"] == 1 and later["tokens_measured"] is False
    assert [i["id"] for i in later["consulted"]] == [precedent]
    assert later["knowledge_calls"] == [{"tool": "kg_precedent", "ok": True, "query": "early repayment",
                                         "refs": [precedent]}]
    pair = learning_live.compare(connection, P, run=LATER, baseline=EARLIER)
    assert [i["id"] for i in pair["carried"]] == [precedent] and pair["carried"][0]["learned_in"] == EARLIER
    assert pair["delta"]["steps"] == -2 and pair["delta"]["tokens"] is None
    assert pair["attributed"] is True
    with pytest.raises(LookupError):
        learning_live.run_proof(connection, P, "athr_no_such_thread")


# --- backfill: only what the transcript shows ------------------------------------------------------
def test_backfill_derives_only_what_the_transcript_shows_and_is_idempotent(connection):
    precedent = _precedent(connection)       # recorded before conversations were anchors: blank provenance
    connection.execute(text(
        "UPDATE kg_nodes SET attrs = attrs || '{\"provenance\": {\"agent_role\": null, \"agent_name\": null}}'::jsonb "
        "WHERE id = :i"), {"i": precedent})
    connection.execute(text("DELETE FROM kg_edges WHERE project_id = :p AND kind = 'produced'"), {"p": P})
    _message(connection, EARLIER, "assistant",
                     "✓ The operator pressed **Record learning: precedent**, and `record_learning` ran and SUCCEEDED."
                     f"\n\nIt returned:\n\n```json\n{{\"learning\": {{\"id\": \"{precedent}\", \"kind\": \"precedent\"}}}}\n```",
                     "2026-10-25T05:32:25Z", tool_calls=[{"tool": "record_learning", "args": {}, "ok": True}])
    # The later conversation asked for precedent, but its stored call kept no result ids: unprovable.
    _message(connection, LATER, "assistant", "checked precedent", "2026-10-25T07:17:23Z", agent_role="sre",
             tool_calls=[{"tool": "kg_precedent", "args": {"query": "early repayment"}, "ok": True}])
    dry = lc.backfill(connection, P)
    assert dry["applied"] is False and len(dry["produced"]) == 1
    assert _edges(connection, f"run:{EARLIER}", "produced") == []           # a dry run writes nothing
    report = lc.backfill(connection, P, apply=True)
    assert [p["node_id"] for p in report["produced"]] == [precedent]
    assert report["provenance_filled"] == [{"node_id": precedent, "agent_name": "BOSS (general)"}]
    assert report["consulted"] == [] and report["cited"] == []
    assert [u["tool"] for u in report["unprovable"]] == ["kg_precedent"]
    assert _usage(connection) == []                                         # no invented use
    prov = lw.item_payload(connection, P, precedent)["provenance"]
    assert prov["agent_name"] == "BOSS (general)"
    assert [e["dst_id"] for e in _edges(connection, f"run:{EARLIER}", "produced")] == [precedent]
    # With the ids the call returned in the record, the later conversation consulted it.
    connection.execute(text(
        "UPDATE agent_messages SET tool_calls = CAST(:tc AS JSONB) WHERE thread_id = :t AND role = 'assistant'"),
        {"t": LATER, "tc": json.dumps([{"tool": "kg_precedent", "args": {"query": "early repayment"}, "ok": True,
                                        "refs": [precedent]}])})
    again = lc.backfill(connection, P, apply=True)
    assert again["produced"] == [] and again["provenance_filled"] == []
    assert [c["node_id"] for c in again["consulted"]] == [precedent]
    assert lc.backfill(connection, P, apply=True)["consulted"] == []         # idempotent
    (edge,) = _edges(connection, f"run:{LATER}", "consulted")
    assert edge["dst_id"] == precedent and edge["attrs"]["agent_name"] == "Site Reliability Engineer"
    pair = learning_live.compare(connection, P, run=LATER, baseline=EARLIER)
    assert [i["id"] for i in pair["carried"]] == [precedent]


def test_the_transcript_keeps_what_a_knowledge_read_returned():
    from boss_engine.services.intent_router import tool_call_record

    record = tool_call_record({"tool": "kg_precedent", "args": {"query": "x"},
                               "result": {"cited_fact_ids": ["kgn_aaaaaaaa1", "kgn_bbbbbbbb2"], "learned": []}})
    assert record == {"tool": "kg_precedent", "args": {"query": "x"}, "ok": True, "error": "",
                      "refs": ["kgn_aaaaaaaa1", "kgn_bbbbbbbb2"]}
    plain = tool_call_record({"tool": "read_code", "args": {}, "result": {"ok": False, "error": "nope"}})
    assert plain == {"tool": "read_code", "args": {}, "ok": False, "error": "nope"}


def test_backfill_recredits_a_press_that_said_boss_in_an_agents_thread_and_nothing_else(connection):
    root = lw.record(connection, project_id=P, learning_kind="root_cause", title="Advance handler skips the decrement",
                     statement="The advance-payment branch never decrements.", detail={}, about={},
                     evidence=[{"type": "file", "ref": "Adv.java:159", "excerpt": "no minus"}], confidence=0.7)["node"]["id"]
    assert lw.item_payload(connection, P, root)["provenance"]["agent_name"] == "BOSS (general)"   # the old press
    built = lw.record(connection, project_id=P, learning_kind="root_cause", title="Pool exhausted in a build",
                      statement="The pool of 5 is exhausted.", detail={}, about={},
                      evidence=[{"type": "log", "ref": "crun_x", "excerpt": "QueuePool"}], confidence=0.6,
                      observed={"run_id": "crun_kgc_build", "agent_role": "boss",
                                "agent_name": "BOSS (general)"})["node"]["id"]
    for node_id in (root, built):
        _message(connection, LATER, "assistant",
                 "✓ The operator pressed **Record learning**, and `record_learning` ran and SUCCEEDED."
                 f"\n\nIt returned:\n\n```json\n{{\"learning\": {{\"id\": \"{node_id}\"}}}}\n```",
                 f"2026-10-25T07:20:{51 if node_id == root else 52}Z",
                 tool_calls=[{"tool": "record_learning", "args": {}, "ok": True}])
    report = lc.backfill(connection, P, apply=True)
    assert report["provenance_filled"] == [{"node_id": root, "agent_name": "Site Reliability Engineer"}]
    prov = lw.item_payload(connection, P, root)["provenance"]
    assert prov["agent_role"] == "sre" and prov["created_by"] == "sre"
    # A learning a run produced keeps its run's provenance, whatever thread quotes it.
    assert lw.item_payload(connection, P, built)["provenance"]["run_id"] == "crun_kgc_build"
    assert lw.item_payload(connection, P, built)["provenance"]["agent_name"] == "BOSS (general)"
