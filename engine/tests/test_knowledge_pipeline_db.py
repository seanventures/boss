"""Knowledge graph pipeline against a disposable Postgres."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine.db import ensure_schema
from boss_engine.services.knowledge.archivist import ScriptedExtractor, SpendingExtractor, distil
from boss_engine.services.knowledge.capture import capture_lifecycle
from boss_engine.services.knowledge.code_index import index_files
from boss_engine.services.knowledge.service import KnowledgeService
from boss_engine.services.knowledge.sources import accept_connector_document, ingest_adr_dir
from boss_engine.services.knowledge.store import PostgresKnowledgeStore

STORE = PostgresKnowledgeStore()
SERVICE = KnowledgeService(STORE)


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
            yield conn
        finally:
            transaction.rollback()


def _projects(connection):
    connection.execute(text("INSERT INTO orgs (id, name) VALUES ('org_a', 'A'), ('org_b', 'B')"))
    connection.execute(
        text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_a', 'org_a', 'A'), ('ws_b', 'org_b', 'B')")
    )
    connection.execute(
        text(
            "INSERT INTO projects (id, workspace_id, name, slug) VALUES "
            "('proj_a', 'ws_a', 'A', 'a'), ('proj_b', 'ws_b', 'B', 'b')"
        )
    )


def test_index_is_idempotent_and_blast_radius_matches_the_hand_checked_set(connection):
    _projects(connection)
    source = "def commit():\n    return 1\n\ndef reserve():\n    return commit()\n\ndef place():\n    return reserve()\n"
    files = [{"path": "stock.py", "language": "python", "source": source}]
    first = index_files(connection, STORE, project_id="proj_a", files=files)
    second = index_files(connection, STORE, project_id="proj_a", files=files)
    assert first["symbols"] == 3
    assert second["skipped"] == 1
    assert second["indexed"] == 0
    reserve = SERVICE.symbol(connection, project_id="proj_a", name="reserve")
    blast = SERVICE.blast(connection, project_id="proj_a", node_id=reserve["id"])
    names = {item["name"] for item in blast["affected"]}
    assert names == {"commit", "place"}
    callers = SERVICE.callers(connection, project_id="proj_a", node_id=reserve["id"])
    assert [row["name"] for row in callers["callers"]] == ["place"]


def test_secret_does_not_land_and_inferred_does_not_close_governed(connection):
    _projects(connection)
    secret = "AKIAIOSFODNN7EXAMPLE"
    capture = STORE.capture(
        connection,
        project_id="proj_a",
        source_kind="run",
        source_id="run-1",
        body="placeholder",
        scrub_counts={},
    )
    result = distil(
        connection,
        STORE,
        project_id="proj_a",
        capture_id=capture["id"],
        text=f"key {secret} size_band treats 15 as large",
        extractor=ScriptedExtractor(facts=({
            "subject": "size_band",
            "predicate": "treats",
            "object": "large",
            "statement": f"size_band uses {secret}",
            "kind": "invariant",
            "confidence": 0.9,
        },)),
    )
    fact_id = result["fact_ids"][0]
    body = connection.execute(text("SELECT body FROM kg_nodes WHERE id = :id"), {"id": fact_id}).scalar_one()
    assert secret not in body
    assert secret not in connection.execute(
        text("SELECT body FROM kg_captures WHERE id = :id"), {"id": capture["id"]}
    ).scalar_one()
    governed = STORE.upsert_node(
        connection,
        project_id="proj_a",
        kind="fact",
        node_key="fact:size_band|treats|governed-rule",
        name="size_band treats governed-rule",
        body="auth changes need security sign-off",
        trust_tier="governed",
        actor_kind="person",
    )
    distil(
        connection,
        STORE,
        project_id="proj_a",
        capture_id=capture["id"],
        text="a later guess",
        extractor=ScriptedExtractor(facts=({
            "subject": "size_band",
            "predicate": "treats",
            "object": "guess",
            "statement": "size_band treats guess",
            "kind": "lesson",
        },)),
    )
    still = STORE.get_node(connection, project_id="proj_a", node_id=governed)
    assert still is not None
    assert still["valid_to"] is None
    with pytest.raises(PermissionError):
        STORE.upsert_node(
            connection,
            project_id="proj_a",
            kind="fact",
            node_key="fact:agent|writes|governed",
            name="no",
            body="no",
            trust_tier="governed",
            actor_kind="agent",
        )


def test_budget_refusal_does_not_call_the_model(connection):
    _projects(connection)
    capture = STORE.capture(
        connection,
        project_id="proj_a",
        source_kind="run",
        source_id="run-2",
        body="plain",
        scrub_counts={},
    )
    result = distil(
        connection,
        STORE,
        project_id="proj_a",
        capture_id=capture["id"],
        text="nothing secret",
        extractor=SpendingExtractor(),
        budget_allows=False,
    )
    assert result["mode"] == "structure_only"
    assert result["fact_ids"] == []


def test_a_spending_extractor_without_a_budget_grant_never_runs(connection):
    """The gate closes by default: a caller that forgets budget_allows gets structure only."""
    _projects(connection)
    capture = STORE.capture(
        connection, project_id="proj_a", source_kind="run", source_id="run-3",
        body="plain", scrub_counts={},
    )
    result = distil(
        connection, STORE, project_id="proj_a", capture_id=capture["id"],
        text="nothing secret", extractor=SpendingExtractor(),
    )
    assert result == {"mode": "structure_only", "fact_ids": [], "scrub_counts": {}}


def test_replay_capture_promotion_and_forget(connection):
    _projects(connection)
    first = STORE.capture(
        connection, project_id="proj_a", source_kind="adr", source_id="0001.md",
        body="decision", scrub_counts={},
    )
    second = STORE.capture(
        connection, project_id="proj_a", source_kind="adr", source_id="0001.md",
        body="decision", scrub_counts={},
    )
    assert first["replayed"] is False
    assert second["replayed"] is True
    assert first["id"] == second["id"]
    proposed = SERVICE.propose_fact(
        connection,
        project_id="proj_a",
        statement="running frontend checks first catches review rounds",
        subject="frontend_checks",
        predicate="catch",
        obj="review_rounds",
        kind="lesson",
    )
    assert proposed["trust_tier"] == "inferred"
    fact_id = proposed["fact_ids"][0]
    for _ in range(3):
        SERVICE.log_pack(connection, project_id="proj_a", run_id=f"run-{_}", step="review", fact_ids=[fact_id])
        STORE.record_outcome(connection, project_id="proj_a", run_id=f"run-{_}", success=True)
    status = SERVICE.status(connection, project_id="proj_a")
    assert fact_id in {item["id"] for item in status["promotion_candidates"]}
    assert status["ship_gate"] == "case_study_primary_not_run"
    assert status["cost_usd"] is None
    SERVICE.promote(connection, project_id="proj_a", fact_id=fact_id, actor_kind="person")
    promoted = STORE.get_node(connection, project_id="proj_a", node_id=fact_id)
    assert promoted["trust_tier"] == "approved"
    with pytest.raises(PermissionError):
        SERVICE.promote(connection, project_id="proj_a", fact_id=fact_id, actor_kind="agent")
    SERVICE.forget(connection, project_id="proj_a", fact_id=fact_id, actor_kind="person")
    assert STORE.get_node(connection, project_id="proj_a", node_id=fact_id) is None
    embeddings = connection.execute(
        text("SELECT COUNT(*) FROM kg_embeddings WHERE node_id = :id"), {"id": fact_id}
    ).scalar_one()
    assert embeddings == 0


def test_cross_project_symbol_is_invisible_and_adr_ingest_replays(connection, tmp_path: Path):
    _projects(connection)
    index_files(
        connection, STORE, project_id="proj_b",
        files=[{"path": "only_b.py", "language": "python", "source": "def secret_name():\n    return 1\n"}],
    )
    missing = SERVICE.symbol(connection, project_id="proj_a", name="secret_name")
    assert missing["found"] is False
    adr = tmp_path / "adr"
    adr.mkdir()
    (adr / "0001-reservation.md").write_text(
        "Stock reservation uses https://hooks.example.com/orders and 203.0.113.10\n",
        encoding="utf-8",
    )
    ingested = ingest_adr_dir(connection, STORE, project_id="proj_a", root=adr)
    again = ingest_adr_dir(connection, STORE, project_id="proj_a", root=adr)
    assert ingested["ingested"] == 1
    assert again["documents"][0]["replayed"] is True
    slack = accept_connector_document(
        connection, STORE, project_id="proj_a", connector="slack",
        document_id="C1", body="see ada@example.com",
    )
    stored = connection.execute(
        text("SELECT body FROM kg_captures WHERE id = :id"), {"id": slack["capture_id"]}
    ).scalar_one()
    assert "ada@example.com" not in stored
    assert datetime.now(timezone.utc)


def _index_stock(connection):
    index_files(
        connection,
        STORE,
        project_id="proj_a",
        files=[{
            "path": "stock.py",
            "language": "python",
            "source": "def reserve():\n    return 1\n",
        }],
    )


def test_a_pull_request_links_the_symbol_it_changed_and_drops_mail(connection):
    _projects(connection)
    _index_stock(connection)
    record = {
        "kind": "pull_request",
        "source_id": "pr-9",
        "title": "Reserve before commit",
        "body": "Discussed with ada@example.com",
        "symbol_names": ["reserve", "missing"],
    }
    first = capture_lifecycle(connection, STORE, project_id="proj_a", record=record)
    node = STORE.find_by_key(
        connection, project_id="proj_a", kind="pull_request", node_key="pull_request:pr-9"
    )
    assert node is not None
    assert "ada@example.com" not in node["body"]
    assert "[EMAIL]" in node["body"]
    linked = STORE.neighbors(
        connection, project_id="proj_a", node_id=node["id"], kind="changed", direction="out"
    )
    assert [row["name"] for row in linked] == ["reserve"]
    second = capture_lifecycle(connection, STORE, project_id="proj_a", record=record)
    assert first["replayed"] is False
    assert second["replayed"] is True
    again = STORE.neighbors(
        connection, project_id="proj_a", node_id=node["id"], kind="changed", direction="out"
    )
    assert len(again) == 1
    index_files(
        connection, STORE, project_id="proj_b",
        files=[{"path": "other.py", "language": "python", "source": "def reserve():\n    return 2\n"}],
    )
    foreign = capture_lifecycle(
        connection, STORE, project_id="proj_a",
        record={
            "kind": "pull_request",
            "source_id": "pr-other",
            "title": "Other",
            "body": "no link",
            "symbol_names": ["not_in_this_project"],
        },
    )
    foreign_node = STORE.find_by_key(
        connection, project_id="proj_a", kind="pull_request", node_key="pull_request:pr-other"
    )
    assert foreign["id"] == foreign_node["id"]
    assert STORE.neighbors(
        connection, project_id="proj_a", node_id=foreign_node["id"], kind="changed", direction="out"
    ) == []


def test_a_person_decision_is_governed_and_names_only_an_id(connection):
    _projects(connection)
    _index_stock(connection)
    capture_lifecycle(
        connection, STORE, project_id="proj_a",
        record={
            "kind": "decision",
            "source_id": "dec-1",
            "title": "Auth needs security sign-off",
            "body": "Signed by the security owner",
            "actor_ref": "person-7",
            "symbol_names": ["reserve"],
        },
    )
    node = STORE.find_by_key(
        connection, project_id="proj_a", kind="decision", node_key="decision:dec-1"
    )
    assert node["trust_tier"] == "governed"
    people = STORE.neighbors(
        connection, project_id="proj_a", node_id=node["id"], kind="decided_by", direction="out"
    )
    assert people[0]["node_key"] == "person:person-7"
    assert people[0]["body"] == ""
    about = STORE.neighbors(
        connection, project_id="proj_a", node_id=node["id"], kind="about", direction="out"
    )
    assert [row["name"] for row in about] == ["reserve"]


def test_attribute_run_counts_the_facts_a_run_was_shown(connection):
    _projects(connection)
    proposed = SERVICE.propose_fact(
        connection, project_id="proj_a",
        statement="running frontend checks first catches review rounds",
        subject="frontend_checks", predicate="catch", obj="review_rounds", kind="lesson",
    )
    fact_id = proposed["fact_ids"][0]
    for index in range(3):
        SERVICE.log_pack(
            connection, project_id="proj_a", run_id=f"shown-{index}", step="review", fact_ids=[fact_id]
        )
        SERVICE.attribute_run(connection, project_id="proj_a", run_id=f"shown-{index}", success=True)
    status = SERVICE.status(connection, project_id="proj_a")
    assert fact_id in {item["id"] for item in status["promotion_candidates"]}
    assert status["retention_years"] == 1
    assert status["embedding_model"] is None
    assert status["vector_index"] == "not_created"
    assert status["ship_gate"] == "case_study_primary_not_run"


def test_the_sre_graph_captures_a_lifecycle_record(connection):
    from boss_engine.services.knowledge_graph import KnowledgeGraphService

    _projects(connection)
    _index_stock(connection)
    graph = KnowledgeGraphService()
    captured = graph.capture_lifecycle(
        connection, "proj_a",
        {
            "kind": "release",
            "source_id": "rel-1",
            "title": "Stock release",
            "body": "Shipped reserve",
            "symbol_names": ["reserve"],
        },
    )
    found = graph.search_knowledge(connection, "proj_a", "reserve")
    assert captured["id"]
    assert any(hit["node_id"] == captured["id"] or "reserve" in str(hit.get("body") or hit.get("name") or "") for hit in found["hits"])


def test_the_read_skill_searches_and_the_propose_skill_stays_inferred(connection, monkeypatch):
    """Both skills are bundles: the agent names the tool (bundle_tool); the project is the run's."""
    from contextlib import contextmanager

    from boss_engine.config import get_settings
    from boss_engine.context import EngineContext
    from boss_engine.services.skill_executors import SkillExecutorContext, resolve_executor

    @contextmanager
    def same_transaction(self):
        yield connection

    monkeypatch.setattr(EngineContext, "begin", same_transaction)
    _projects(connection)
    _index_stock(connection)
    context = SkillExecutorContext(
        settings=get_settings(), connection=connection, project_id="proj_a", actor="archivist"
    )
    found = resolve_executor("knowledge.read", context=context)(
        {"bundle_tool": "kg_symbol", "name": "reserve", "project_id": "proj_b"})
    assert found["tool"] == "kg_symbol" and found["result"]["found"] is True
    proposed = resolve_executor("knowledge.propose", context=context)({
        "bundle_tool": "kg_propose_fact",
        "statement": "reserve runs before commit",
        "subject": "reserve",
        "predicate": "precedes",
        "object": "commit",
        "kind": "invariant",
        "trust_tier": "governed",
    })
    assert proposed["result"]["trust_tier"] == "inferred"
    with pytest.raises(ValueError):
        resolve_executor("knowledge.read", context=context)({"bundle_tool": "kg_propose_fact"})


def test_a_failed_pack_lookup_leaves_the_transaction_usable(connection, monkeypatch):
    from boss_engine.services.knowledge.runtime import quoted_pack_for_turn

    def boom(self, connection, **_kwargs):
        connection.execute(text("SELECT * FROM knowledge_runtime_missing"))
        return {"items": [], "fact_ids": []}

    monkeypatch.setattr(KnowledgeService, "pack", boom)
    assert quoted_pack_for_turn(
        connection, project_id="proj_a", query="reserve", run_id="run-1"
    ) == ""
    assert connection.execute(text("SELECT 1")).scalar() == 1


def test_a_written_pull_request_is_indexed_and_cites_the_fact(connection):
    from types import SimpleNamespace

    from boss_engine.services.knowledge.runtime import (
        attribute_shown_facts,
        quoted_pack_for_turn,
        remember_written_files,
    )

    _projects(connection)
    proposed = SERVICE.propose_fact(
        connection,
        project_id="proj_a",
        statement="reserve needs a sign-off",
        subject="reserve",
        predicate="needs",
        obj="sign-off",
        kind="lesson",
    )
    fact_id = proposed["fact_ids"][0]
    remembered = remember_written_files(
        connection,
        project_id="proj_a",
        source_id="pr-9",
        title="checkout",
        body="Discussed with ada@example.com",
        files=[
            SimpleNamespace(path="stock.py", content="def reserve():\n    return 1\n"),
            SimpleNamespace(path="README.md", content="# notes"),
        ],
        run_id="run-pr",
    )
    assert fact_id in remembered["cited_fact_ids"]
    node = STORE.find_by_key(
        connection, project_id="proj_a", kind="pull_request", node_key="pull_request:pr-9"
    )
    assert "ada@example.com" not in node["body"]
    assert "[EMAIL]" in node["body"]
    linked = STORE.neighbors(
        connection, project_id="proj_a", node_id=node["id"], kind="changed", direction="out"
    )
    assert [row["name"] for row in linked] == ["reserve"]
    assert STORE.find_by_key(
        connection, project_id="proj_a", kind="file", node_key="file:README.md"
    ) is None
    again = remember_written_files(
        connection,
        project_id="proj_a",
        source_id="pr-9",
        title="checkout",
        body="Discussed with ada@example.com",
        files=[SimpleNamespace(path="stock.py", content="def reserve():\n    return 1\n")],
        run_id="run-pr",
    )
    assert again["replayed"] is True
    assert len(STORE.neighbors(
        connection, project_id="proj_a", node_id=node["id"], kind="changed", direction="out"
    )) == 1
    section = quoted_pack_for_turn(
        connection, project_id="proj_a", query="reserve", run_id="run-agent"
    )
    assert "reserve needs a sign-off" in section
    assert "quoted source material" not in section
    assert fact_id in section
    counted = attribute_shown_facts(
        connection, project_id="proj_a", run_id="run-agent", success=True
    )
    assert fact_id in counted


def test_the_knowledge_switch_off_gives_an_agent_no_pack_and_a_pr_no_citations(connection):
    """The case study's control arm: knowledge_graph = off. Capture and indexing go on."""
    from types import SimpleNamespace

    from boss_engine.services.knowledge.runtime import quoted_pack_for_turn, remember_written_files

    _projects(connection)
    SERVICE.propose_fact(connection, project_id="proj_a", statement="reserve needs a sign-off",
                         subject="reserve", predicate="needs", obj="sign-off", kind="lesson")
    assert "reserve" in quoted_pack_for_turn(connection, project_id="proj_a", query="reserve sign-off")
    connection.execute(text(
        "INSERT INTO project_settings (project_id, key, value) VALUES ('proj_a', 'knowledge_graph', '\"off\"')"))
    assert quoted_pack_for_turn(connection, project_id="proj_a", query="reserve sign-off") == ""
    remembered = remember_written_files(
        connection, project_id="proj_a", source_id="pr-off", title="reserve", body="change",
        files=[SimpleNamespace(path="stock.py", content="def reserve():\n    return 1\n")])
    assert remembered["cited_fact_ids"] == [] and remembered["linked"]


def test_an_agent_re_recording_the_same_fact_is_not_an_overwrite(connection) -> None:
    """A second run of a step that writes what the graph already holds (Historical Pattern
    re-finding INC-2088 on the same project) must not be refused: identical content only
    refreshes last_seen. Changing a non-inferred fact is still refused (2026-10-31)."""
    _projects(connection)
    first = STORE.upsert_node(
        connection, project_id="proj_a", kind="incident", node_key="incident:INC-2088",
        name="INC-2088", body="Repayment misallocated", trust_tier="observed",
        actor_kind="agent", actor_ref="graph_steps:historical_pattern",
        attrs={"incident_id": "INC-2088"})
    again = STORE.upsert_node(
        connection, project_id="proj_a", kind="incident", node_key="incident:INC-2088",
        name="INC-2088", body="Repayment misallocated", trust_tier="observed",
        actor_kind="agent", actor_ref="graph_steps:historical_pattern",
        attrs={"incident_id": "INC-2088"})
    assert again == first
    with pytest.raises(PermissionError):
        STORE.upsert_node(
            connection, project_id="proj_a", kind="incident", node_key="incident:INC-2088",
            name="INC-2088", body="A different account of it", trust_tier="observed",
            actor_kind="agent", actor_ref="graph_steps:historical_pattern")
