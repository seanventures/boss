"""The two graphs on the canvas (design §4.2, §4.7.3) and a map that is built before it is read.

Breaks covered:
- the Context chip shows nothing, or a case focus shows the case alone, because the trace's links
  (a decision's run, a case's runs, a run's remediation) were never written as edges;
- a case, a decision or a statement says a raw node id ("Compliance case: kgn_…");
- a decision's panel cannot say which step, run, agent and versions it ran under, or what it
  cited, or which case it belongs to;
- a run the trace names but that has no run node of its own is "not in the graph";
- the Domain and Context bubbles and their words drift from the ontology packs;
- RCA 2026-10-31: a project whose code was indexed but whose map (view rollup) was never built
  showed "0 areas / No code yet" over its code until a manual Rebuild, and a failed rebuild was
  swallowed without a word.
"""
from __future__ import annotations

import logging
import os
import textwrap

import pytest
from boss_engine.db import ensure_schema
from boss_engine.services.knowledge import context_graph, ontology_packs, view, view_context, view_rollup
from boss_engine.services.knowledge.code_index import index_repository
from boss_engine.services.knowledge.store import PostgresKnowledgeStore
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

STORE = PostgresKnowledgeStore()
P = "proj_kgctx"
RUN_A, RUN_B = "wfr_ctxtest00000000aaaa", "wfr_ctxtest00000000bbbb"


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


@pytest.fixture()
def connection(pg_engine):
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text("INSERT INTO orgs (id, name) VALUES ('org_kgctx', 'KGCTX')"))
            conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_kgctx', 'org_kgctx', 'KGCTX')"))
            conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, 'ws_kgctx', 'KGCTX', 'kgctx')"),
                         {"p": P})
            ontology_packs.set_project_pack(conn, project_id=P, pack_id="banking-regulatory", set_by="test")
            yield conn
        finally:
            transaction.rollback()
            ontology_packs._PROJECT_PACK_CACHE.pop(P, None)


def _node(c, kind, key, name, attrs=None, body=""):
    return STORE.upsert_node(c, project_id=P, kind=kind, node_key=key, name=name, body=body,
                             trust_tier="inferred", actor_kind="agent", actor_ref=f"test:{kind}", attrs=attrs or {})


@pytest.fixture()
def case(connection):
    """One clause, its compliance case, two runs of the pipeline on it (the later one with a
    remediation and a precedent), and a rule a decision confirmed — written as the pipeline
    writes them: decisions by run and step, the case by its clause, no edges between them."""
    c = connection
    doc = _node(c, "document", "document:t", "Regulation Z, 12 CFR 1026.36(c)(1)")
    clause = _node(c, "clause", "clause:t", "§1026.36(c)(1)(ii) Partial payments", {"document_id": doc})
    rule = _node(c, "rule", "rule:t", "Repayment-like transaction classification")
    case_id = _node(c, "compliance_case", f"case:{clause}", f"Compliance case: {clause}",
                    {"clause_id": clause, "status": "open"})
    for run in (RUN_A, RUN_B):
        context_graph.write_agent_decision(
            c, P, step_id="clause_intake", run_id=run, decision_type="ClauseIntake",
            statement=f"Opened a compliance case for clause {clause}.", policy_version="",
            workflow_version="", agent_fingerprint="deterministic", actor_ref="pipeline:clause_intake")
    context_graph.write_agent_decision(
        c, P, step_id="lane_confirmation", run_id=RUN_B, decision_type="Confirmed",
        statement=f"Confirmed: clause and code both point at {rule}.", policy_version="p7",
        workflow_version="4", agent_fingerprint="sha256:abcdef0123456789", actor_ref="pipeline:lane_confirmation")
    package = _node(c, "remediation_package", f"remediation:{RUN_B}:remediation_package",
                    "Credit partial payments on the date received", {"owner_team": "Payments Platform Team"},
                    body="Hold partial payments in suspense and credit them on the date of receipt.")
    precedent = _node(c, "precedent", f"precedent:{RUN_B}:compliance_closure", "Suspense handling is absent",
                      {"run_id": RUN_B})
    return {"clause": clause, "case": case_id, "rule": rule, "package": package, "precedent": precedent, "doc": doc}


def test_the_context_bubble_counts_the_trace_in_the_packs_words(connection, case):
    o = view.overview(connection, P)
    bubbles = {b["id"]: b for b in o["layers"]}
    ctx = bubbles["layer:context"]
    assert ctx["layer"] == "context" and ctx["label"] == "Context"
    assert ctx["counts"]["compliance_case"] == 1 and ctx["counts"]["agent_decision"] == 3
    assert ctx["sub"].startswith("1 compliance case")            # cases first, in the pack's word
    assert "3 agent decisions" in ctx["sub"]                    # the decisions that worked it, too
    assert view._mid_sentence("AML case") == "AML case"          # an acronym keeps its capitals
    assert view._mid_sentence("Compliance case") == "compliance case"
    dom = bubbles["layer:domain"]
    assert dom["counts"] == {"clause": 1, "document": 1} and dom["sub"] == "1 document · 1 clause"
    assert o["kinds"]["compliance_case"]["color"] == "#ff9f4a"   # the pack's colour
    assert o["kinds"]["agent_decision"]["layer"] == "context"
    # The two graphs joined at Whole repo: the case concerns a clause; a decision cites a rule.
    pairs = {(e["src"], e["dst"]) for e in o["edges"] if e["src"].startswith("layer:") and e["dst"].startswith("layer:")}
    assert ("layer:context", "layer:domain") in pairs and ("layer:context", "layer:rules") in pairs
    # Only Context on: the other bubbles are not drawn.
    only = view.overview(connection, P, layers=frozenset({"context"}))
    assert [b["id"] for b in only["layers"]] == ["layer:context"]


def test_a_case_focus_is_its_case_file(connection, case):
    h = view.neighbourhood(connection, P, case["case"], depth=1, layers=frozenset({"context"}), limit=80)
    by_id = {n["id"]: n for n in h["nodes"]}
    kinds = {e["kind"] for e in h["edges"]}
    # Its latest run's decisions and remediation, the precedent it set, and both runs, at one hop.
    assert case["package"] in by_id and case["precedent"] in by_id
    decisions = [n for n in h["nodes"] if n["kind"] == "agent_decision"]
    assert {d["label"] for d in decisions} == {"Clause intake", "Confirmed"}
    runs = [n for n in h["nodes"] if n["kind"] == "run"]
    assert len(runs) == 2 and all(r["id"].startswith(view_context.VIRTUAL_RUN) for r in runs)
    assert all("ctxtest" not in r["label"] and "wfr_" not in r["label"] for r in runs)  # never an id
    assert {"decided", "remedied_by", "worked_on", "precedent"} <= kinds
    assert all(e["label"] is None for e in h["edges"] if e["kind"] in view_context.TRACE_EDGES)
    # With Domain on too, the clause it concerns.
    h2 = view.neighbourhood(connection, P, case["case"], depth=1, layers=frozenset({"context", "domain"}), limit=80)
    assert case["clause"] in {n["id"] for n in h2["nodes"]}
    # At two hops, the earlier run's own decision joins through its run.
    h3 = view.neighbourhood(connection, P, case["case"], depth=2, layers=frozenset({"context"}), limit=80)
    assert len([n for n in h3["nodes"] if n["kind"] == "agent_decision"]) == 3
    # A virtual run is a centre of its own.
    run = runs[0]["id"]
    around = view.neighbourhood(connection, P, run, depth=1, layers=frozenset({"context"}), limit=80)
    assert around["center"]["label"] == runs[0]["label"] and len(around["nodes"]) > 1


def test_a_decision_panel_says_what_where_who_and_links_to_its_case(connection, case):
    d = next(i for i in connection.execute(text(
        "SELECT id FROM kg_nodes WHERE project_id = :p AND kind = 'agent_decision' AND attrs->>'decision_type' = 'Confirmed'"),
        {"p": P}).scalars())
    detail = view.node_detail(connection, P, d)
    card = detail["context"]
    assert detail["node"]["label"] == "Confirmed" and detail["node"]["sub"] == "Lane confirmation step"
    assert card["decision_type"] == "Confirmed" and card["step"] == "Lane confirmation"
    assert card["workflow_version"] == "v4" and card["policy_version"] == "p7"
    assert card["fingerprint"] == "abcdef012345"
    assert card["agent"]["kind"] == "outside" or card["agent"]["name"]   # words, never a key
    assert card["run"]["label"].startswith("A workflow run") and "wfr_" not in card["run"]["label"]
    links = {link["key"]: [n["id"] for n in link["items"]] for link in card["links"]}
    assert links["case"] == [case["case"]]
    assert links["cites"] == [case["rule"]]
    assert links["precedent_produced"] == [case["precedent"]]
    # The statement says the rule by its name once the response is mended — no raw id.
    mended = view_context.mend_ids(connection, P, detail)
    assert case["rule"] not in mended["context"]["statement"]
    assert "Repayment-like transaction classification" in mended["context"]["statement"]


def test_a_case_and_a_virtual_run_say_names_never_ids(connection, case):
    detail = view_context.mend_ids(connection, P, view.node_detail(connection, P, case["case"]))
    assert detail["node"]["label"] == "Compliance case: “§1026.36(c)(1)(ii) Partial payments”"
    keys = {link["key"] for link in detail["context"]["links"]}
    assert {"decisions", "remediation", "runs", "concerns"} <= keys
    run = next(n for link in detail["context"]["links"] if link["key"] == "runs" for n in link["items"])
    vr = view.node_detail(connection, P, run["id"])
    assert vr["node"]["kind"] == "run" and vr["context"]["links"]
    with pytest.raises(LookupError):
        view.node_detail(connection, P, f"{view_context.VIRTUAL_RUN}wfr_nosuchrun")


def test_the_context_and_domain_trees_group_by_the_packs_kinds(connection, case):
    top = view.tree_children(connection, P, tab="context")
    labels = [r["label"] for r in top["rows"]]
    assert labels[0] == "Compliance cases" and "Agent decisions" in labels and "Remediation packages" in labels
    decisions = view.tree_children(connection, P, tab="context", parent="context:agent_decision")
    assert any(r["label"] == "Confirmed · Lane confirmation step" for r in decisions["rows"])
    dom = view.tree_children(connection, P, tab="domain")
    assert [r["label"] for r in dom["rows"]] == ["Documents", "Clauses"]


TREE = {
    "core/src/bank/core/Ledger.java": """
        package bank.core;
        public class Ledger { public void post(String e) { validate(e); } void validate(String e) { } }
        """,
    "loan/src/bank/loan/Disbursement.java": """
        package bank.loan;
        import bank.core.Ledger;
        public class Disbursement { private final Ledger l = new Ledger(); public void go() { l.post("x"); } }
        """,
}


def _index(connection, tmp_path):
    for path, body in TREE.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(body).lstrip())
    index_repository(connection, STORE, project_id=P, root=tmp_path, repo="bank/core")


def test_code_without_a_map_is_mapped_on_first_read(connection, tmp_path):
    """RCA: project setup indexed the code but never built the map; the Graph said "No code yet"."""
    _index(connection, tmp_path)
    connection.execute(text("DELETE FROM kg_view_tree WHERE project_id = :p"), {"p": P})
    connection.execute(text("DELETE FROM kg_view_sync WHERE project_id = :p"), {"p": P})
    o = view.overview(connection, P)
    assert o["map"] == {"built": True, "nodes": view_rollup.live_code_count(connection, P)}
    assert o["areas"] and o["built_at"] and not o["stale"]
    # Built once: the next read reads it.
    assert view.overview(connection, P)["map"] is None


def test_a_map_that_cannot_be_built_is_said_and_logged(connection, tmp_path, monkeypatch, caplog):
    _index(connection, tmp_path)
    connection.execute(text("DELETE FROM kg_view_tree WHERE project_id = :p"), {"p": P})
    connection.execute(text("DELETE FROM kg_view_sync WHERE project_id = :p"), {"p": P})

    def broken(_c, _p):
        raise RuntimeError("disk full")

    monkeypatch.setattr(view_rollup, "rebuild", broken)
    with caplog.at_level(logging.ERROR, logger="boss_engine.services.knowledge.view_rollup"):
        o = view.overview(connection, P)
    assert o["map"]["built"] is False and "disk full" in o["map"]["error"]
    assert o["areas"] == [] and o["stale"] is True
    assert any("could not be built" in r.getMessage() for r in caplog.records)
    # The read itself still answered, and the connection is still usable.
    assert connection.execute(text("SELECT 1")).scalar() == 1


def test_a_failed_rebuild_after_an_index_is_logged_not_swallowed(pg_engine, monkeypatch, caplog):
    def broken(_c, _p):
        raise RuntimeError("lock timeout")

    monkeypatch.setattr(view_rollup, "rebuild", broken)
    monkeypatch.setattr(view_rollup, "is_stale", lambda *_a, **_k: True)
    with caplog.at_level(logging.ERROR, logger="boss_engine.services.knowledge.view_rollup"):
        out = view_rollup.rebuild_after_index(pg_engine, "proj_kgctx_none")
    assert "lock timeout" in out["error"]
    assert any("rebuild failed" in r.getMessage() for r in caplog.records)
