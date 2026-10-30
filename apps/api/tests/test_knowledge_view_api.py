"""The Graph area's read routes over HTTP (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §3; BOSSAPP-768).

The view logic is tested against the database in engine/tests/test_knowledge_view_db.py; this
holds the routes to it: each answers from its own handler with knowledge:read, maps a missing
node to 404 and a bad parameter to 422 (in words), and returns the Phase 0 wire shape.
"""
from __future__ import annotations

import json
import textwrap

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services.knowledge import view_rollup
from boss_engine.services.knowledge.code_index import index_repository, symbol_key
from boss_engine.services.knowledge.store import PostgresKnowledgeStore
from boss_engine.services.members import MemberDirectoryService

PROJECT_ID = "boss-dogfood"
ACTOR = "ceo"
REPO = "bank/core-banking"
BASE = "/v1/knowledge/view"
TREE = {
    "core/src/bank/core/Ledger.java": """
        package bank.core;
        public class Ledger { public void post(String e) { } }
        """,
    "loan/src/bank/loan/Disbursement.java": """
        package bank.loan;
        import bank.core.Ledger;
        public class Disbursement {
            private final Ledger ledger = new Ledger();
            public void disburse(long amount) { ledger.post("disburse"); }
        }
        """,
}


@pytest.fixture
def graph(engine, tmp_path):
    for path, body in TREE.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(body).lstrip())
    with engine.begin() as db:
        index_repository(db, PostgresKnowledgeStore(), project_id=PROJECT_ID, root=tmp_path, repo=REPO)
        view_rollup.rebuild(db, PROJECT_ID)
        ids = dict(db.execute(text("SELECT name, id FROM kg_nodes WHERE project_id = :p AND kind = 'symbol' "
                                   "AND valid_to IS NULL"), {"p": PROJECT_ID}).all())
    yield ids
    with engine.begin() as db:
        db.execute(text("DELETE FROM kg_view_sync WHERE project_id = :p"), {"p": PROJECT_ID})
        db.execute(text("DELETE FROM kg_edges WHERE project_id = :p"), {"p": PROJECT_ID})
        db.execute(text("DELETE FROM kg_nodes WHERE project_id = :p"), {"p": PROJECT_ID})


def _get(client, path: str, **query):
    return client.get(f"{BASE}{path}", params={"project_id": PROJECT_ID, "actor": ACTOR, **query})


def _get_as(client, path: str, actor: str, **query):
    return client.get(f"{BASE}{path}", params={"project_id": PROJECT_ID, "actor": actor, **query})


@pytest.fixture
def graph_with_decision(engine, graph):
    """``graph`` plus a decision node hung off Ledger — the one kind the view withholds
    without ``decision:read`` (view.KIND_PERMISSIONS)."""
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO kg_nodes (id, project_id, kind, node_key, name, attrs, trust_tier, layer, "
            "created_by, valid_to) VALUES (:id, :p, 'decision', :key, :n, CAST(:a AS JSONB), "
            "'observed', 'inferred', 'system', NULL)"),
            {"id": "kgn_view_api_dec", "p": PROJECT_ID, "key": "decision:kgn_view_api_dec",
             "n": "Keep the ledger append-only", "a": json.dumps({"learning_kind": "decision"})})
        db.execute(text(
            "INSERT INTO kg_edges (id, project_id, src_id, dst_id, kind, attrs, layer) "
            "VALUES (:id, :p, :s, :d, 'about', CAST('{}' AS JSONB), 'inferred')"),
            {"id": "kge_view_api_dec_ledger", "p": PROJECT_ID, "s": "kgn_view_api_dec", "d": graph["Ledger"]})
        view_rollup.rebuild(db, PROJECT_ID)
    yield graph
    with engine.begin() as db:
        db.execute(text("DELETE FROM kg_view_sync WHERE project_id = :p"), {"p": PROJECT_ID})
        db.execute(text("DELETE FROM kg_edges WHERE id = 'kge_view_api_dec_ledger'"))
        db.execute(text("DELETE FROM kg_nodes WHERE id = 'kgn_view_api_dec'"))
        view_rollup.rebuild(db, PROJECT_ID)


def test_a_legacy_capability_member_sees_decisions_through_the_scoped_translation(
    client, engine, graph_with_decision
):
    """BOSSAPP-768's withholding checks the scoped ``decision:read`` permission (view.KIND_PERMISSIONS),
    but a project member seeded before the scoped catalogue existed only holds the legacy
    ``decision_resolve`` capability (services/members.py's DEFAULT_MEMBER_IDS). Without
    MemberDirectoryService._scoped_permissions translating that capability on read, the route's
    fallback lookup (_holds in knowledge_view.py, taken when there is no identity context on the
    request) would wrongly withhold the owner's own decision from the Graph view."""
    settings = get_settings()
    with engine.begin() as db:
        MemberDirectoryService(settings).upsert_member(
            db, project_id=PROJECT_ID, member_id="legacy_owner_view_api",
            display_name="Legacy Owner", member_type="human", role_keys=[],
            permissions=["decision_resolve", "knowledge:read"], actor="test")
        MemberDirectoryService(settings).upsert_member(
            db, project_id=PROJECT_ID, member_id="narrow_agent_view_api",
            display_name="Narrow Agent", member_type="human", role_keys=[],
            permissions=["knowledge:read"], actor="test")

    seen = _get_as(client, "/neighbourhood", "legacy_owner_view_api",
                   node=graph_with_decision["Ledger"], depth=1).json()
    assert "decision" in {n["kind"] for n in seen["nodes"]}

    hidden = _get_as(client, "/neighbourhood", "narrow_agent_view_api",
                     node=graph_with_decision["Ledger"], depth=1).json()
    assert "decision" not in {n["kind"] for n in hidden["nodes"]}


@pytest.fixture
def graph_with_precedent(engine, graph):
    """``graph`` plus a precedent node hung off Ledger — the other kind
    ``view.KIND_PERMISSIONS`` withholds without ``decision:read``, alongside ``decision``."""
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO kg_nodes (id, project_id, kind, node_key, name, attrs, trust_tier, layer, "
            "created_by, valid_to) VALUES (:id, :p, 'precedent', :key, :n, CAST(:a AS JSONB), "
            "'observed', 'inferred', 'system', NULL)"),
            {"id": "kgn_view_api_prec", "p": PROJECT_ID, "key": "precedent:kgn_view_api_prec",
             "n": "Ledger posts are append-only", "a": json.dumps({"learning_kind": "precedent"})})
        db.execute(text(
            "INSERT INTO kg_edges (id, project_id, src_id, dst_id, kind, attrs, layer) "
            "VALUES (:id, :p, :s, :d, 'about', CAST('{}' AS JSONB), 'inferred')"),
            {"id": "kge_view_api_prec_ledger", "p": PROJECT_ID, "s": "kgn_view_api_prec", "d": graph["Ledger"]})
        view_rollup.rebuild(db, PROJECT_ID)
    yield graph
    with engine.begin() as db:
        db.execute(text("DELETE FROM kg_view_sync WHERE project_id = :p"), {"p": PROJECT_ID})
        db.execute(text("DELETE FROM kg_edges WHERE id = 'kge_view_api_prec_ledger'"))
        db.execute(text("DELETE FROM kg_nodes WHERE id = 'kgn_view_api_prec'"))
        view_rollup.rebuild(db, PROJECT_ID)


def test_a_caller_without_decision_read_does_not_see_a_precedent_either(
    client, engine, graph_with_precedent
):
    """``decision`` has its own HTTP-level withholding test above; ``precedent`` is the other
    kind mapped to ``decision:read`` in ``view.KIND_PERMISSIONS`` and needs the same coverage
    over the wire, not just in the engine suite's ``withheld_for`` unit test."""
    settings = get_settings()
    with engine.begin() as db:
        MemberDirectoryService(settings).upsert_member(
            db, project_id=PROJECT_ID, member_id="decision_reader_view_api",
            display_name="Decision Reader", member_type="human", role_keys=[],
            permissions=["knowledge:read", "decision:read"], actor="test")
        MemberDirectoryService(settings).upsert_member(
            db, project_id=PROJECT_ID, member_id="narrow_reader_view_api",
            display_name="Narrow Reader", member_type="human", role_keys=[],
            permissions=["knowledge:read"], actor="test")

    seen = _get_as(client, "/neighbourhood", "decision_reader_view_api",
                   node=graph_with_precedent["Ledger"], depth=1).json()
    assert "precedent" in {n["kind"] for n in seen["nodes"]}

    hidden = _get_as(client, "/neighbourhood", "narrow_reader_view_api",
                     node=graph_with_precedent["Ledger"], depth=1).json()
    assert "precedent" not in {n["kind"] for n in hidden["nodes"]}


def test_every_view_route_answers_in_the_wire_shape(client, graph):
    overview = _get(client, "/overview")
    assert overview.status_code == 200, overview.text
    body = overview.json()
    assert {a["label"] for a in body["areas"]} == {"core", "loan"} and body["repos"] == [REPO]
    assert [layer["id"] for layer in body["layers"]] == ["layer:rules", "layer:learning"]
    assert body["edges"][0]["label"] == "1 call"
    drill = _get(client, "/overview", area="loan", layers="code").json()
    assert drill["level"] == "package" and drill["layers"] == []

    hood = _get(client, "/neighbourhood", node=graph["Disbursement"], depth=2).json()
    assert hood["level"] == "class" and {n["label"] for n in hood["nodes"]} == {"Disbursement", "Ledger"}

    method = graph["Disbursement.disburse"]
    detail = _get(client, f"/node/{method}").json()
    assert detail["node"]["label"] == "disburse()" and detail["area"] == "loan"
    area_key = f"area:{REPO}:loan"
    assert _get(client, f"/node/{area_key}").json()["node"]["kind"] == "area"   # a key with '/' in it

    areas = _get(client, "/tree/children").json()
    assert [r["label"] for r in areas["rows"]] == ["core", "loan"]
    classes = _get(client, "/tree/children", parent=f"pkg:{REPO}:loan:bank.loan").json()
    assert classes["rows"][0]["key"] == symbol_key(REPO, "loan/src/bank/loan/Disbursement.java", "Disbursement")

    found = _get(client, "/search", q="disburse").json()
    assert found["hits"][0]["match"] == "exact" and found["meaning_available"] is False


def test_the_routes_refuse_in_words(client, graph):
    missing = _get(client, "/node/kgn_nope")
    assert missing.status_code == 404 and "not in this project" in missing.json()["detail"]
    deep = _get(client, "/neighbourhood", node=graph["Ledger"], depth=9)
    assert deep.status_code == 422 and "depth" in deep.json()["detail"]
    chips = _get(client, "/overview", layers="code,secrets")
    assert chips.status_code == 422 and "unknown layer secrets" in chips.json()["detail"]
    tab = _get(client, "/tree/children", tab="other")
    assert tab.status_code == 422
    meaning = _get(client, "/search", q="ledger", meaning=1)
    assert meaning.status_code == 422 and "meaning" in meaning.json()["detail"]
    stranger = client.get(f"{BASE}/overview", params={"project_id": PROJECT_ID, "actor": "nobody-at-all"})
    assert stranger.status_code in (401, 403)
