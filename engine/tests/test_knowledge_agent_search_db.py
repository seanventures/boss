"""The agent kg tools find learnings by incident id and by meaning, as the console's Graph search
does (find.find_written: one search for both).

Break covered (northpeak-bank, 2026-10-25): asked "which repayment strategy did INC-2041 affect?",
the SRE agent said kg_precedent and kg_search "return zero hits for INC-2041" and guessed the wrong
strategy. The graph held INC-2041's root cause, investigation and precedent: INC-2041 sat in the
root cause's evidence attributes and in the brief of the conversation that produced all three,
which only the console searched. Also: a symptom in other words (INC-2088's brief) must reach
INC-2041's precedent, and an empty answer says what it searched.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager

import pytest
from boss_engine.db import ensure_schema
from boss_engine.services.knowledge import view
from boss_engine.services.knowledge.find import find_written
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

P = "proj_kgas"
RC = "kgn_as_rc_2041"
INV = "kgn_as_inv_2041"
PREC = "kgn_as_prec_2041"
RC_2088 = "kgn_as_rc_2088"
B4_SYMPTOM = ("Since yesterday's release, when a customer pays before their instalment is due, the ledger "
              "books more than the cash received. Some customers' next instalments show as paid that they "
              "have not paid.")


@pytest.fixture(scope="module")
def pg_engine():
    url = os.environ.get("BOSS_KG_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
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


def _node(connection, nid: str, kind: str, name: str, *, body: str = "", attrs: dict | None = None,
          trust: str = "inferred", key: str | None = None) -> str:
    connection.execute(text(
        "INSERT INTO kg_nodes (id, project_id, kind, node_key, name, attrs, body, trust_tier, layer, created_by) "
        "VALUES (:id, :p, :k, :key, :n, CAST(:a AS JSONB), :b, :t, 'inferred', 'system')"),
        {"id": nid, "p": P, "k": kind, "key": key or f"learning:{kind}:{nid}", "n": name,
         "a": json.dumps(attrs or {}), "b": body, "t": trust})
    return nid


def _edge(connection, src: str, dst: str, kind: str) -> None:
    connection.execute(text(
        "INSERT INTO kg_edges (id, project_id, src_id, dst_id, kind, attrs, layer) "
        "VALUES (:id, :p, :s, :d, :k, '{}'::jsonb, 'inferred')"),
        {"id": f"kge_{src}_{dst}"[:60], "p": P, "s": src, "d": dst, "k": kind})


def _conversation(connection, nid: str, thread: str, brief: str, produced: list[str], later: str = "") -> None:
    connection.execute(text("INSERT INTO agent_threads (id, project_id, actor) VALUES (:t, :p, 'ceo')"),
                       {"t": thread, "p": P})
    for i, (content, ago) in enumerate([(brief, "2 hours"), (later, "1 hour")]):
        if content:
            connection.execute(text(
                "INSERT INTO agent_messages (id, thread_id, role, content, created_at) "
                "VALUES (:i, :t, 'user', :c, now() - CAST(:ago AS interval))"),
                {"i": f"amsg_{thread}_{i}", "t": thread, "c": content, "ago": ago})
    _node(connection, nid, "run", brief.splitlines()[0].lstrip("# "), trust="observed", key=f"run:{thread}",
          attrs={"subtype": "conversation", "thread_id": thread, "source_id": thread})
    for dst in produced:
        _edge(connection, nid, dst, "produced")


@pytest.fixture()
def connection(pg_engine):
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text("INSERT INTO orgs (id, name) VALUES ('org_kgas', 'KGAS')"))
            conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_kgas', 'org_kgas', 'KGAS')"))
            conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, 'ws_kgas', 'KGAS', 'kgas')"),
                         {"p": P})
            # INC-2041 as the graph holds it: in the root cause's evidence only, and in the brief of
            # the conversation that produced the root cause, the investigation and the precedent.
            _node(conn, RC, "root_cause",
                  "Standard strategy (mifos-standard-strategy) overbooks principal by the interest amount",
                  body="FineractStyleLoanRepaymentScheduleTransactionProcessor.handleTransactionThatIsOnTimePaymentOfInstallment "
                       "computes interestPortion but never does transactionAmountRemaining = "
                       "transactionAmountRemaining.minus(interestPortion).",
                  attrs={"learning_kind": "root_cause", "confidence": 0.9,
                         "evidence": [{"type": "metric", "ref": "INC-2041 reconciliation report"}],
                         "provenance": {"agent_role": "sre", "agent_name": "Responder"}})
            _node(conn, INV, "investigation", "On-time repayment branch is the sole cause; waiver branches ruled out",
                  body="Only the final else branch fails to decrement transactionAmountRemaining by interestPortion.",
                  attrs={"learning_kind": "investigation", "confidence": 0.85})
            _node(conn, PREC, "precedent",
                  "Allocator that skips the remaining-amount decrement after one component overpays every component after it",
                  body="When a repayment handler computes a component against a running remaining amount but omits "
                       "the remaining = remaining.minus(component) step, every later component is sized against cash "
                       "already spoken for.",
                  attrs={"learning_kind": "precedent", "confidence": 0.8,
                         "detail": {"symptom": "booked components (interest+principal) sum to more than the cash "
                                               "received; a loan's later instalments show partial payment though no "
                                               "cash reached them"}})
            _node(conn, RC_2088, "root_cause", "Advance-payment handler never decrements the remaining amount",
                  body="handleTransactionThatIsPaymentInAdvanceOfInstallment books fees and interest from the full amount.",
                  attrs={"learning_kind": "root_cause", "confidence": 0.9})
            # Noise an agent's words also reach: approved rules about repayment strategies.
            for i, name in enumerate(("Repayments follow the product's repayment strategy order",
                                      "The standard repayment strategy pays penalties, fees, interest, then principal")):
                _node(conn, f"kgn_as_rule_{i}", "rule", name, body=name, trust="approved", key=f"rule:as{i}",
                      attrs={"status": "approved", "category": "calculation", "statement": name})
            _conversation(conn, "kgn_as_conv_b3", "athr_as_b3",
                          "# B3 · Incident: repayments post more than the customer paid\n"
                          "**Incident INC-2041 · Severity 2 · opened 06:40**", [RC, INV, PREC])
            _conversation(conn, "kgn_as_conv_b4", "athr_as_b4",
                          "# B4 · Incident: early repayments post more than the customer paid\n"
                          "**Incident INC-2088 · Severity 2 · opened 07:15**\n" + B4_SYMPTOM, [RC_2088],
                          # Asked later in INC-2088's conversation (as on the live thread): it does
                          # not make INC-2088's root cause an INC-2041 learning.
                          later="Which repayment strategy did INC-2041 affect?")
            yield conn
        finally:
            transaction.rollback()


class _Ctx:
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def begin(self):
        yield self.connection


def _tool(connection, name: str, /, **args):
    from boss_engine.tools.builtin import build_default_registry

    return build_default_registry().get(name).handler(_Ctx(connection), project_id=P, actor="agent", args=args)


def test_an_incident_id_finds_its_root_cause_investigation_and_precedent_in_every_agent_tool(connection):
    three = {RC, INV, PREC}
    found = find_written(connection, P, "INC-2041")
    assert found[0] == {"id": RC, "kind": "root_cause", "match": "id"}      # it carries the id itself
    assert {f["id"] for f in found[:3]} == three                            # the conversation's, next
    assert RC_2088 not in [f["id"] for f in found if f["match"] in ("id", "run")]
    assert [f["id"] for f in found[:3]] == [RC, PREC, INV]                   # root cause, precedent, investigation

    for query in ("INC-2041", "which repayment strategy did INC-2041 affect?"):
        hits = _tool(connection, "kg_search", query=query)["hits"]
        assert hits[0]["node_id"] == RC and {h["node_id"] for h in hits[:3]} == three, (query, hits[:4])
        assert "searched" not in _tool(connection, "kg_search", query=query)
        precedent = _tool(connection, "kg_precedent", query=query)
        assert [i["id"] for i in precedent["learned"][:3]] == [RC, PREC, INV], query
        assert f"[L:{RC}]" in precedent["section"]

    # The console and the agents read one search: the same three lead both.
    console = [h["node"]["id"] for h in view.search(connection, P, "INC-2041")["hits"]]
    assert console[:3] == [RC, PREC, INV]

    # kg_why given the incident key (not a node id) names the nodes that carry it.
    why = _tool(connection, "kg_why", node_id="INC-2041")
    assert RC in [m["node_id"] for m in why["matches"]]


def test_inc_2088_finds_its_own_root_cause_by_id_and_inc_2041s_precedent_by_its_symptom(connection):
    hits = _tool(connection, "kg_search", query="INC-2088")["hits"]
    assert hits[0]["node_id"] == RC_2088          # only the conversation's brief names INC-2088
    # INC-2088's symptom, in its own words, reaches the precedent INC-2041 left.
    precedent = _tool(connection, "kg_precedent", query=B4_SYMPTOM)
    assert PREC in [i["id"] for i in precedent["learned"]], precedent["learned"]
    assert PREC in [h["node_id"] for h in _tool(connection, "kg_search", query=B4_SYMPTOM)["hits"]]


def test_an_empty_answer_says_what_it_searched_never_that_the_graph_is_empty(connection):
    for tool in ("kg_search", "kg_precedent"):
        found = _tool(connection, tool, query="INC-9999")
        assert not found.get("hits") and not found.get("learned"), tool
        searched = found["searched"]
        assert {"ops", "knowledge", "rules"} <= set(searched["layers"]), tool
        assert "INC-9999" in searched["note"] and "attributes" in searched["note"], tool
        assert "does not prove" in searched["note"], tool
    why = _tool(connection, "kg_why", node_id="INC-9999")
    assert "not a node id" in why["error"] and why["searched"]["fields"]
    rules = _tool(connection, "kg_rules", query="INC-9999")
    assert rules["count"] == 0 and "rules only" in rules["searched"]["note"]
