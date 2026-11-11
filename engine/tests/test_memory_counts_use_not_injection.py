"""BOSSAPP-960: agent memory counts USE apart from INJECTION, ranks by relevance, and decays.

`pack_for_turn` used to bump `use_count` for everything it injected, so whatever fit the budget
ranked higher and got injected again — the loop measured its own output, and the pruner's
`use_count = 0` test was unsatisfiable for anything ever packed. And `prune` existed with
nothing scheduled to call it. Also here: builds attribute the KG facts they were shown on their
terminal transition, once.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine.db import ensure_schema
from boss_engine.services import agent_memory as mem


def _database_url() -> str:
    return os.environ.get("BOSS_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""


@pytest.fixture(scope="module")
def pg_engine():
    url = _database_url()
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if not url or not any(marker in name for marker in ("test", "testing", "ci", "scratch", "tmp")):
        pytest.skip("BOSS_TEST_DATABASE_URL must name a disposable database")
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


def _project(connection, pid="proj_m"):
    connection.execute(text("INSERT INTO orgs (id, name) VALUES ('org_m', 'M')"))
    connection.execute(
        text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_m', 'org_m', 'M')"))
    connection.execute(
        text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
             "(:p, 'ws_m', 'M', :p)"), {"p": pid})
    return pid


def _row(connection, memory_id):
    return connection.execute(
        text("SELECT use_count, injected_count, status FROM agent_memory WHERE id = :i"),
        {"i": memory_id}).mappings().one()


def test_injection_counts_as_injection_and_never_as_use(connection):
    pid = _project(connection)
    row = mem.remember(connection, project_id=pid, actor="op", kind="project",
                       body="the frontend is Vite + React")
    block = mem.pack_for_turn(connection, project_id=pid)
    assert "Vite + React" in block
    counted = _row(connection, row["id"])
    assert counted["injected_count"] == 1
    assert counted["use_count"] == 0, "being packed is opportunity, not value"


def test_teaching_the_same_thing_again_is_the_use_signal(connection):
    pid = _project(connection)
    row = mem.remember(connection, project_id=pid, actor="op", kind="project",
                       body="deploys go through the ops API, never ssh")
    again = mem.remember(connection, project_id=pid, actor="op", kind="project",
                         body="deploys go through the ops API, never ssh")
    assert again["id"] == row["id"] or _row(connection, row["id"])["use_count"] == 1
    assert _row(connection, row["id"])["use_count"] == 1


def test_recall_ranks_by_relevance_to_the_turn(connection):
    pid = _project(connection)
    mem.remember(connection, project_id=pid, actor="op", kind="project",
                 body="the staging deploy needs the ops API token")
    mem.remember(connection, project_id=pid, actor="op", kind="project",
                 body="the newsletter template uses comic sans")
    got = mem.recall(connection, project_id=pid, query="why did the staging deploy fail")
    assert "staging deploy" in got[0]["body"]
    assert mem.relevance("staging deploy failed", "the staging deploy needs a token") > \
        mem.relevance("staging deploy failed", "the newsletter template uses comic sans")


def test_the_retention_sweep_prunes_what_was_injected_but_never_used(connection):
    pid = _project(connection)
    row = mem.remember(connection, project_id=pid, actor="op", kind="project",
                       body="an old note nobody ever used")
    # Injected on many turns over three months, never once mattering.
    for _ in range(5):
        mem.pack_for_turn(connection, project_id=pid)
    connection.execute(text(
        "UPDATE agent_memory SET created_at = now() - interval '120 days' WHERE id = :i"),
        {"i": row["id"]})
    from boss_engine.services.activity_retention import prune as retention_prune

    result = retention_prune(connection)
    assert result["removed"].get("agent_memory", 0) >= 1
    assert _row(connection, row["id"])["status"] == "retired"


def test_a_build_attributes_the_facts_it_was_shown_once_on_its_terminal_transition(connection):
    from boss_engine.services.knowledge.service import KnowledgeService
    from boss_engine.services.run_terminal import on_status_written

    pid = _project(connection)
    service = KnowledgeService()
    proposed = service.propose_fact(
        connection, project_id=pid,
        statement="builds on this repo must run the frontend checks first",
        subject="frontend_checks", predicate="precede", obj="builds", kind="lesson")
    fact_id = proposed["fact_ids"][0]
    connection.execute(text(
        "INSERT INTO code_task_runs (id, project_id, status, phase, title) "
        "VALUES ('crun_960', :p, 'completed', 'done', 'a build')"), {"p": pid})
    service.log_pack(connection, project_id=pid, run_id="crun_960", step="implement",
                     fact_ids=[fact_id])

    on_status_written(connection, "crun_960")
    stat = service.store.fact_stat(connection, project_id=pid, fact_id=fact_id)
    assert stat["success_count"] == 1

    # The transition hook fires from several paths; attribution happens once.
    on_status_written(connection, "crun_960")
    marked = connection.execute(text(
        "SELECT finalize_ctx->>'knowledge_attributed' FROM code_task_runs "
        "WHERE id = 'crun_960'")).scalar()
    assert marked == "completed"
    stat = service.store.fact_stat(connection, project_id=pid, fact_id=fact_id)
    assert stat["success_count"] == 1, "a reclaim re-invoking the hook double-counted"
