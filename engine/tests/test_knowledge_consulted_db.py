"""Durable "consulted" and "reused" (BOSSAPP-768, KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §2.5, §4.2).

Breaks covered: a use recorded before the run's node exists lost when ingest captures the run; a
re-ingest that duplicates edges; a "shown" item drawn as consulted; a cited precedent not counted
as reused; a citation in an output not recorded; a kg tool's hit not drawn live; the node panel's
"learned by X in run A → used by Y in run B" lineage missing the later run; the sweep leaving a
workflow run's uses without edges.
"""
from __future__ import annotations

import os

import pytest
from boss_engine.db import ensure_schema
from boss_engine.services.knowledge import learning_live, view
from boss_engine.services.knowledge import learning_writeback as lw
from boss_engine.services.knowledge.capture import capture_lifecycle
from boss_engine.services.knowledge.store import PostgresKnowledgeStore
from boss_engine.services.llm_telemetry import clear_llm_context, set_llm_context
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

STORE = PostgresKnowledgeStore()
P = "proj_kgc"


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


@pytest.fixture()
def connection(pg_engine):
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text("INSERT INTO orgs (id, name) VALUES ('org_kgc', 'KGC')"))
            conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_kgc', 'org_kgc', 'KGC')"))
            conn.execute(text(f"INSERT INTO projects (id, workspace_id, name, slug) VALUES ('{P}', 'ws_kgc', 'KGC', 'kgc')"))
            conn.execute(text(
                "INSERT INTO roles (id, project_id, role_key, display_name, spec) VALUES "
                f"('role_kgc_sre', '{P}', 'sre', 'SRE', CAST('{{\"persona\": {{\"callsign\": \"Responder\"}}}}' AS JSONB)), "
                f"('role_kgc_be', '{P}', 'backend_engineer', 'Backend', CAST('{{\"persona\": {{\"callsign\": \"Builder\"}}}}' AS JSONB))"))
            yield conn
        finally:
            transaction.rollback()
            clear_llm_context()


def _learn(connection, run_id: str, role: str, kind: str = "precedent", **detail):
    set_llm_context(role_key=role, run_id=run_id, stage="investigate")
    return lw.record(connection, project_id=P, learning_kind=kind, title=f"{kind} for the 502s",
                     statement="502s on /orders came from the pool limit; raising it fixed them.",
                     detail=detail or {"symptom": "502s on /orders", "cause_summary": "pool limit",
                                       "fix_summary": "raise the pool limit", "applies_to": ["orders"]},
                     about={}, evidence=[{"type": "metric", "ref": "chk_1", "excerpt": "errors 0.1%"}],
                     confidence=0.75)


def _edges(connection, dst: str, kind: str):
    return [dict(r) for r in connection.execute(text(
        "SELECT s.node_key AS src_key, e.attrs FROM kg_edges e JOIN kg_nodes s ON s.id = e.src_id "
        "WHERE e.dst_id = :d AND e.kind = :k AND e.valid_to IS NULL ORDER BY s.node_key"),
        {"d": dst, "k": kind}).mappings()]


def _run(connection, run_id: str) -> None:
    connection.execute(text(
        "INSERT INTO code_task_runs (id, project_id, title, status, spec, state) VALUES "
        "(:i, :p, 'Fix the 502s', 'completed', '{}'::jsonb, '{}'::jsonb)"), {"i": run_id, "p": P})


def test_a_use_recorded_before_the_run_node_exists_becomes_an_edge_at_ingest_and_stays_one(connection):
    precedent = _learn(connection, "crun_a", "sre")
    # A later run's tool returned it: kg_usage only — the row a tool writes when it cannot draw.
    STORE.log_usage(connection, project_id=P, run_id="crun_b", step="investigate",
                    fact_ids=[precedent["node"]["id"]], use_kind="consulted", agent_role="backend_engineer")
    assert not _edges(connection, precedent["node"]["id"], "consulted")
    assert connection.execute(text(
        "SELECT 1 FROM kg_nodes WHERE project_id = :p AND node_key = 'run:crun_b'"), {"p": P}).first() is None
    _run(connection, "crun_b")
    record = {"kind": "run", "source_id": "crun_b", "title": "Fix the 502s", "body": "Run crun_b completed"}
    first = capture_lifecycle(connection, STORE, project_id=P, record=record)
    assert first["usage_edges"] == 1
    edges = _edges(connection, precedent["node"]["id"], "consulted")
    assert [e["src_key"] for e in edges] == ["run:crun_b"]
    assert (edges[0]["attrs"]["how"], edges[0]["attrs"]["agent_role"], edges[0]["attrs"]["agent_name"]) == (
        "tool", "backend_engineer", "Builder")
    capture_lifecycle(connection, STORE, project_id=P, record={**record, "body": "Run crun_b completed again"})
    assert len(_edges(connection, precedent["node"]["id"], "consulted")) == 1          # idempotent
    assert connection.execute(text(
        "SELECT count(*) FROM kg_edges WHERE project_id = :p AND kind = 'consulted'"), {"p": P}).scalar() == 1


def test_shown_draws_nothing_and_a_cited_precedent_is_consulted_and_reused(connection):
    precedent = _learn(connection, "crun_a", "sre")
    pid = precedent["node"]["id"]
    set_llm_context(role_key="backend_engineer", run_id="crun_b", stage="implement")
    lw.log_use(connection, project_id=P, ids=[pid], use_kind="shown")
    assert not _edges(connection, pid, "consulted")
    lw.log_use(connection, project_id=P, ids=[pid, "kgn_not_a_thing"], use_kind="cited")
    consulted = _edges(connection, pid, "consulted")
    assert [e["src_key"] for e in consulted] == ["run:crun_b"] and consulted[0]["attrs"]["how"] == "cited"
    assert [e["src_key"] for e in _edges(connection, pid, "reused")] == ["run:crun_b"]
    # A later tool hit does not demote "cited".
    lw.log_use(connection, project_id=P, ids=[pid], use_kind="consulted")
    assert _edges(connection, pid, "consulted")[0]["attrs"]["how"] == "cited"


def test_a_citation_in_a_build_summary_is_recorded_as_cited(connection):
    fix = _learn(connection, "crun_a", "backend_engineer", kind="fix",
                 change_summary="raise the pool limit", files=["app/db.py"])
    out = lw.observe(connection, project_id=P, hook="repair_checkpoint", run_id="crun_b", attempt=0, passed=True,
                     tests_passed=True, command="pytest", files=["app/db.py"],
                     summary=f"Applied the earlier fix [L:{fix['node']['id']}] to the pool.")
    assert out == []
    assert [e["src_key"] for e in _edges(connection, fix["node"]["id"], "reused")] == ["run:crun_b"]


def test_the_lineage_says_learned_by_x_in_run_a_and_used_by_y_in_run_b(connection):
    precedent = _learn(connection, "crun_a", "sre")
    set_llm_context(role_key="backend_engineer", run_id="crun_b", stage="implement")
    lw.log_use(connection, project_id=P, ids=[precedent["node"]["id"]], use_kind="cited")
    detail = view.node_detail(connection, P, precedent["node"]["id"])
    lineage = detail["lineage"]
    assert (lineage["learned"]["run_id"], lineage["learned"]["agent_name"]) == ("crun_a", "Responder")
    used = {(u["run_id"], u["how"]) for u in lineage["used"]}
    assert ("crun_b", "cited") in used and ("crun_b", "reused") in used
    assert all(u["agent_name"] == "Builder" for u in lineage["used"])
    assert detail["usage"]["consulted_runs"] == 1 and detail["usage"]["reused_runs"] == 1


def test_the_live_feed_shows_a_use_before_its_edge_is_drawn(connection):
    precedent = _learn(connection, "crun_a", "sre")
    STORE.log_usage(connection, project_id=P, run_id="wfr_inc_2", step="triage", fact_ids=[precedent["node"]["id"]],
                    use_kind="consulted", workflow_run_id="wfr_inc_2", agent_role="sre")
    feed = learning_live.live(connection, P, run="wfr_inc_2")
    edge = next(e for e in feed["edges"] if e["dst"] == precedent["node"]["id"])
    assert (edge["kind"], edge["how"], edge["run_id"]) == ("consulted", "tool", "wfr_inc_2")
    assert precedent["node"]["id"] in {i["node"]["id"] for i in feed["learnings"]}     # named for the strip


def test_the_sweep_draws_a_workflow_runs_uses(pg_engine):
    from boss_engine.services.knowledge.ingest import derive_usage_edges

    import uuid

    project = "proj_kgc_sweep_" + uuid.uuid4().hex[:8]
    with pg_engine.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_kgcs', 'S') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_kgcs', 'org_kgcs', 'S') ON CONFLICT DO NOTHING"))
        c.execute(text(f"INSERT INTO projects (id, workspace_id, name, slug) VALUES ('{project}', 'ws_kgcs', 'S', '{project}') "
                       "ON CONFLICT DO NOTHING"))
        set_llm_context(role_key="sre", run_id="crun_s1", stage="investigate")
        item = lw.record(c, project_id=project, learning_kind="root_cause", title="Pool limit",
                         statement="the pool limit causes 502s", detail={}, about={},
                         evidence=[{"type": "log", "ref": "x", "excerpt": "pool exhausted"}], confidence=0.6)
        STORE.log_usage(c, project_id=project, run_id="wfr_s2", step="triage", fact_ids=[item["node"]["id"]],
                        use_kind="consulted", workflow_run_id="wfr_s2", agent_role="sre")
    out = derive_usage_edges(pg_engine, project_id=project)
    clear_llm_context()
    assert out["edges"] == 1 and out["failed"] == 0
    with pg_engine.connect() as c:
        assert [e["src_key"] for e in _edges(c, item["node"]["id"], "consulted")] == ["run:wfr_s2"]
