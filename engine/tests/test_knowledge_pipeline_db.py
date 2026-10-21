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
