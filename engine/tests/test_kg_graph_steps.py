"""KG P5a: the `kg_investigation_pipeline` template run end to end through the real workflow
runtime (design: ~/boss-ops design §4.4).

This REPLACES the earlier helper-level test, which called `knowledge.graph_steps` functions
directly and so never proved any of it was wired into a real run. Here the template is started
twice through `workflows`/`workflow_capability_runner` against a seeded graph, with a scripted
stand-in only for the `task`-kind agent placeholders (P5b's wiring, out of scope for P5a):

 - run 1's deterministic steps (`context_enrichment`, `historical_pattern`) run for $0 and their
   AgentDecision/CoverageGap writes land in the Context Graph;
 - the bounded `evidence_loop` runs at most twice (`max_iterations`);
 - run 1's Closure writes a Precedent through `workflow_capability_runner.execute_graph_step` —
   the SAME dispatcher the deterministic steps use, not a one-off call;
 - run 2's `historical_pattern` step finds that Precedent.

Runs against a real, disposable postgres (DATABASE_URL), like test_regulatory_trace.py.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine.db import ensure_schema
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services import workflow_catalogue as catalogue
from boss_engine.services import workflows
from boss_engine.services.knowledge.store import PostgresKnowledgeStore

STORE = PostgresKnowledgeStore()
TEMPLATE_KEY = "kg_investigation_pipeline"


def _database_url() -> str:
    return os.environ.get("BOSS_KG_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""


@pytest.fixture(scope="module")
def pg_engine():
    url = _database_url()
    if not url:
        pytest.skip("DATABASE_URL must be set to a reachable postgres for these tests")
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        pytest.skip(f"postgres is not reachable: {exc.__class__.__name__}")
    ensure_schema(engine)
    return engine


class Ctx:
    """Minimal engine context: the tools under test only need a transaction."""

    def __init__(self, engine):
        self._engine = engine

    def begin(self):
        return self._engine.begin()


@pytest.fixture()
def project(pg_engine):
    org = f"org_kggraph_{uuid.uuid4().hex[:8]}"
    ws = f"ws_kggraph_{uuid.uuid4().hex[:8]}"
    pid = f"p_kggraph_{uuid.uuid4().hex[:8]}"
    with pg_engine.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:i, 'KG Graph Steps')"), {"i": org})
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:i, :o, 'KG Graph Steps')"),
                  {"i": ws, "o": org})
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                       "(:i, :w, 'KG Graph Steps', :s)"), {"i": pid, "w": ws, "s": pid})
    yield pid
    with pg_engine.begin() as c:
        for table in ("workflow_step_runs", "workflow_runs", "workflow_mappings",
                      "workflow_versions", "workflow_definitions", "decisions", "events",
                      "kg_edges", "kg_nodes"):
            try:
                with c.begin_nested():
                    c.execute(text(f"DELETE FROM {table} WHERE project_id=:p"), {"p": pid})
            except Exception:
                pass
        c.execute(text("DELETE FROM projects WHERE id=:p"), {"p": pid})


def _rule(c, project_id, name):
    return STORE.upsert_node(c, project_id=project_id, kind="rule", node_key=f"rule:{name}",
                             name=name, body=f"{name} body", trust_tier="governed",
                             actor_kind="person", actor_ref="qa@example.com")


def _symbol(c, project_id, name):
    return STORE.upsert_node(c, project_id=project_id, kind="symbol", node_key=f"symbol:{name}",
                             name=name, body="", trust_tier="observed", actor_kind="agent",
                             actor_ref="indexer")


def _incident(c, project_id, name):
    return STORE.upsert_node(c, project_id=project_id, kind="incident", node_key=f"incident:{name}",
                             name=name, body=f"{name} body", trust_tier="observed",
                             actor_kind="agent", actor_ref="ingest")


def _link(c, project_id, src, dst):
    STORE.upsert_edge(c, project_id=project_id, src_id=src, dst_id=dst, kind="about",
                      actor_kind="agent", actor_ref="qa@example.com")


def _publish_template(c, project_id):
    catalogue.seed(c, project_id, actor="test")
    definition = c.execute(
        text("SELECT id FROM workflow_definitions WHERE project_id=:p AND workflow_key=:k"),
        {"p": project_id, "k": TEMPLATE_KEY}).mappings().one()
    version = c.execute(
        text("SELECT id FROM workflow_versions WHERE definition_id=:d "
             "ORDER BY created_at DESC LIMIT 1"),
        {"d": definition["id"]}).mappings().one()
    return version["id"]


def _start(c, project_id, version_id, subject_id):
    return workflows.start_run(
        c, project_id=project_id, actor="test",
        input_data={"directive": "investigate", "subject_id": subject_id, "max_hops": 1},
        dedupe_key=f"dk_{uuid.uuid4().hex[:10]}", workflow_version_id=version_id,
    )


def _run_task_step(c, project_id, run_id, step_id, *, subject_id):
    """The scripted stand-in for the `task`-kind agent placeholders (P5b's wiring). `closure`
    writes its Precedent through `runner.execute_graph_step` — the SAME dispatcher the
    deterministic steps use — never a one-off node insert of its own."""
    claimed = workflows.claim_step(c, project_id=project_id, run_id=run_id,
                                   worker_id="scripted-task", lease_seconds=60)
    assert claimed is not None and claimed["step_id"] == step_id, (claimed, step_id)
    if step_id == "closure":
        result = runner.execute_graph_step(
            c, project_id=project_id, run_id=run_id, step_id="closure",
            inputs={"subject_id": subject_id, "symptom": "pattern-subject-rule is unowned",
                    "cause_summary": "no owner rule linked", "fix_summary": "assigned an owner"})
        output = {"status": "completed", **result}
    elif step_id == "orchestrator":
        output = {"status": "completed", "finding_families": []}
    else:
        output = {"status": "completed"}
    result = workflows.complete_step(c, project_id=project_id, step_run_id=claimed["id"],
                                     worker_id="scripted-task", output=output, evidence=[])
    if step_id == "intake":
        # THE FIRST ENRICHMENT ROUND. `evidence_loop` is a "while" (checked before every
        # iteration, including the first): its `missing_kinds` expression cannot be true until
        # `context_enrichment` has run once, so nothing in the template itself starts that
        # first round (the template is P5a's, unchanged here). The P5b analysis agent that owns
        # this decision does not exist yet, so the scripted stand-in starts it exactly as that
        # agent would — by activating the step and running it through the SAME dispatcher
        # (`execute_graph_step`) a real deterministic step uses, never a direct node write.
        _run_context_enrichment(c, project_id, run_id, subject_id=subject_id)
        workflows._activate_step(c, run_id, "evidence_loop", 0)
    return result


def _run_context_enrichment(c, project_id, run_id, *, subject_id):
    workflows._activate_step(c, run_id, "context_enrichment", 0)
    claimed = workflows.claim_step(c, project_id=project_id, run_id=run_id,
                                   worker_id="scripted-task", lease_seconds=60)
    assert claimed is not None and claimed["step_id"] == "context_enrichment", claimed
    result = runner.execute_graph_step(
        c, project_id=project_id, run_id=run_id, step_id="context_enrichment",
        inputs={"subject_id": subject_id, "max_hops": 1})
    return workflows.complete_step(
        c, project_id=project_id, step_run_id=claimed["id"], worker_id="scripted-task",
        output={"kind": "deterministic", **result}, evidence=[])


def _approve_pending_decision(c, project_id, run_id):
    row = c.execute(
        text("SELECT decision_id FROM workflow_step_runs WHERE workflow_run_id=:r "
             "AND status='waiting' AND decision_id IS NOT NULL"),
        {"r": run_id}).mappings().first()
    if not row:
        return False
    c.execute(text("UPDATE decisions SET status='approved', approved_by='qa' WHERE id=:d"),
             {"d": row["decision_id"]})
    workflows.advance_run(c, project_id, run_id)
    return True


_TASK_KINDS = frozenset({"task"})
_CONTROL_KINDS = frozenset({"condition", "parallel", "join", "bounded_loop", "for_each",
                           "decision", "timer", "subworkflow"})


def _drive(eng, project_id, run_id, *, subject_id):
    """Advance the run to completion: `tick()` for capability/deterministic work, the scripted
    stand-in for `task` steps, and an approval for the one human gate."""
    context_enrichment_runs = 0
    for _ in range(100):
        with eng.begin() as c:
            status = c.execute(text("SELECT status FROM workflow_runs WHERE id=:r"),
                               {"r": run_id}).scalar()
            if status in workflows.TERMINAL_RUN_STATES:
                return
            next_row = c.execute(
                text("SELECT step_id, step_kind FROM workflow_step_runs "
                     "WHERE workflow_run_id=:r AND status='ready' "
                     "ORDER BY run_after, created_at, iteration, step_id LIMIT 1"),
                {"r": run_id}).mappings().first()
            if next_row and next_row["step_kind"] in _TASK_KINDS:
                _run_task_step(c, project_id, run_id, next_row["step_id"], subject_id=subject_id)
                continue
        with eng.begin() as c:
            result = runner.tick(c, Ctx(eng), project_id=project_id)
            for item in result["performed"]:
                if item.get("step_id") == "context_enrichment":
                    context_enrichment_runs += 1
            if result["count"] or result["performed"]:
                continue
        with eng.begin() as c:
            if _approve_pending_decision(c, project_id, run_id):
                continue
    raise AssertionError(f"run {run_id} never finished")


# ── P1: run 1's deterministic steps run for $0, bounded, and write to the Context Graph ───────

def test_p1_a_run_executes_its_deterministic_steps_and_closure_writes_a_precedent(
        pg_engine, project):
    with pg_engine.begin() as c:
        subject = _rule(c, project, "pattern-subject-rule")
        shared_symbol = _symbol(c, project, "pattern-shared-symbol")
        sibling_incident = _incident(c, project, "pattern-sibling-incident")
        _link(c, project, subject, shared_symbol)
        _link(c, project, sibling_incident, shared_symbol)
        version_id = _publish_template(c, project)
        run = _start(c, project, version_id, subject)
    run_id = run["id"]

    _drive(pg_engine, project, run_id, subject_id=subject)

    with pg_engine.connect() as c:
        run_status = c.execute(text("SELECT status FROM workflow_runs WHERE id=:r"),
                               {"r": run_id}).scalar()
        assert run_status == "completed", run_status

        det_steps = c.execute(
            text("SELECT step_id, output FROM workflow_step_runs WHERE workflow_run_id=:r "
                 "AND step_id IN ('context_enrichment', 'historical_pattern')"),
            {"r": run_id}).mappings().all()
        by_id = {r["step_id"]: r["output"] for r in det_steps}
        assert by_id["context_enrichment"]["cost_usd_micros"] == 0
        assert by_id["historical_pattern"]["cost_usd_micros"] == 0

        decisions = c.execute(
            text("SELECT attrs FROM kg_nodes WHERE project_id=:p AND kind='agent_decision' "
                 "AND attrs->>'run_id'=:r"),
            {"p": project, "r": run_id}).mappings().all()
        decision_types = {d["attrs"]["decision_type"] for d in decisions}
        assert "ContextAssembled" in decision_types, "Context Enrichment's write did not land"
        assert "HistoricalPatternMatched" in decision_types, (
            "Historical Pattern's write did not land")
        assert "PrecedentRecorded" in decision_types, "Closure's write did not land"

        precedent = c.execute(
            text("SELECT id FROM kg_nodes WHERE project_id=:p AND kind='precedent' "
                 "AND attrs->>'run_id'=:r"),
            {"p": project, "r": run_id}).mappings().first()
        assert precedent is not None, "Closure never wrote a Precedent node"

        loop_row = c.execute(
            text("SELECT iteration FROM workflow_step_runs WHERE workflow_run_id=:r "
                 "AND step_id='context_enrichment' ORDER BY iteration DESC LIMIT 1"),
            {"r": run_id}).scalar()
        assert int(loop_row) < 2, "the bounded evidence_loop exceeded its max_iterations of 2"


# ── P1: the learning loop — run 2's Historical Pattern finds run 1's Closure's Precedent ──────

def test_p1_a_second_runs_historical_pattern_finds_the_first_runs_closure_precedent(
        pg_engine, project):
    with pg_engine.begin() as c:
        subject = _rule(c, project, "closure-later-rule")
        shared_symbol = _symbol(c, project, "closure-shared-symbol")
        version_id = _publish_template(c, project)
        run_1 = _start(c, project, version_id, subject)
        _link(c, project, subject, shared_symbol)
    _drive(pg_engine, project, run_1["id"], subject_id=subject)

    with pg_engine.begin() as c:
        run_2 = _start(c, project, version_id, subject)
    _drive(pg_engine, project, run_2["id"], subject_id=subject)

    with pg_engine.connect() as c:
        status = c.execute(text("SELECT status FROM workflow_runs WHERE id=:r"),
                           {"r": run_2["id"]}).scalar()
        assert status == "completed", status

        hist_output = c.execute(
            text("SELECT output FROM workflow_step_runs WHERE workflow_run_id=:r "
                 "AND step_id='historical_pattern'"),
            {"r": run_2["id"]}).mappings().one()["output"]
        matched = hist_output.get("matched") or []
        precedent_from_run_1 = c.execute(
            text("SELECT id FROM kg_nodes WHERE project_id=:p AND kind='precedent' "
                 "AND attrs->>'run_id'=:r"),
            {"p": project, "r": run_1["id"]}).scalar()
        assert precedent_from_run_1 is not None
        assert precedent_from_run_1 in matched, (
            "run 2's historical_pattern step did not find the Precedent run 1's Closure wrote")
