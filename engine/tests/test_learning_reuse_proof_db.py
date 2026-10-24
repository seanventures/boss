"""The §4.6 plumbing: a run records what it reused and what it cost, so a before/after pair can
be compared (BOSSAPP-768, KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §4.6). PLUMBING ONLY, not the release
claim: the claim is made on real runs, A vs A′, median of at least three pairs.

Scripted pairs, no model: a scripted builder plays the governed build's implement loop through the
same hooks the build calls (learning_context.for_build at the start, learning_writeback's
repair checkpoint after each gate) and records one llm_calls row per attempt, as every engine's
telemetry does. It reads its instructions like an agent that follows the primer: when the task
context quotes what an earlier run learned about the failure it would otherwise hit, it gets it
right first time and cites the tag in its summary. A scripted responder does the same with the
precedent an incident step is given.

Pairs: (1) a build whose first attempt needed a repair round → a later build on the same code;
(2) an incident → a second incident with the same symptom. Arm A′ (knowledge_graph = keyword) is
given no learned items and repeats the repair round. Asserted: the later run carries the
consulted/reused edge to the earlier run's learning, its scripted step count drops, compare()
attributes it — and A′ is not attributed.
"""
from __future__ import annotations

import os
import re
import uuid

import pytest
from boss_engine.db import ensure_schema
from boss_engine.services import project_settings
from boss_engine.services.knowledge import learning_context, learning_live
from boss_engine.services.knowledge import learning_writeback as lw
from boss_engine.services.knowledge.capture import capture_lifecycle
from boss_engine.services.knowledge.code_index import index_files
from boss_engine.services.knowledge.service import KnowledgeService
from boss_engine.services.knowledge.store import PostgresKnowledgeStore
from boss_engine.services.llm_telemetry import clear_llm_context, set_llm_context
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

STORE = PostgresKnowledgeStore()
REPO = "acme/bank"
PATH = "loan-core/src/loans/disburse.py"
SOURCE = '''class LoanDisburser:
    def disburse(self, loan, account):
        if account is None:
            raise LinkedAccountRequired("loan.link.to.savings.account.required")
        return loan.amount
'''
BRIEF = "Add a disbursement fee to LoanDisburser.disburse; keep the linked savings account rule."
FAILURE = ("FAILED tests/test_disburse.py::test_disburse_without_account\n"
           "LinkedAccountRequired: loan.link.to.savings.account.required was not raised")
TAG = re.compile(r"\[L:(kgn_[A-Za-z0-9_]+)\]")


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
def project(pg_engine):
    """A committed project: every hook runs in its own transaction, as in a real build."""
    pid = "proj_kgp_" + uuid.uuid4().hex[:8]
    with pg_engine.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_kgp', 'KGP') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_kgp', 'org_kgp', 'KGP') "
                       "ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, 'ws_kgp', :p, :p)"),
                  {"p": pid})
        c.execute(text(
            "INSERT INTO roles (id, project_id, role_key, display_name, spec) VALUES "
            "(:a, :p, 'backend_engineer', 'Backend', CAST('{\"persona\": {\"callsign\": \"Builder\"}}' AS JSONB)), "
            "(:b, :p, 'sre', 'SRE', CAST('{\"persona\": {\"callsign\": \"Responder\"}}' AS JSONB))"),
            {"a": f"role_{pid}_be", "b": f"role_{pid}_sre", "p": pid})
        index_files(c, STORE, project_id=pid, repo=REPO,
                    files=[{"path": PATH, "language": "python", "source": SOURCE}])
    # Left in the disposable database: every id below is the project's own, so nothing collides.
    yield pid
    clear_llm_context()


def _call(engine, pid: str, run_id: str, stage: str, tokens: int) -> None:
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO llm_calls (id, project_id, code_task_run_id, stage, engine, model, total_tokens, "
            "cost_usd_micros, turns) VALUES (:i, :p, :r, :s, 'scripted', 'scripted-v1', :t, :m, 3)"),
            {"i": "llm_" + uuid.uuid4().hex[:12], "p": pid, "r": run_id, "s": stage, "t": tokens, "m": tokens * 3})


def _scripted_build(engine, pid: str, run_id: str) -> dict:
    """The governed implement loop's knowledge path, played by a scripted builder."""
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, title, status, spec) VALUES (:i, :p, 'Disbursement fee', "
            "'running', CAST(:s AS JSONB))"), {"i": run_id, "p": pid, "s": '{"instructions": "%s"}' % BRIEF})
    set_llm_context(db_engine=engine, project_id=pid, run_id=run_id, role_key="backend_engineer",
                    stage="implement", model_id="scripted-v1")
    instructions = learning_context.for_build(engine, project_id=pid, run_id=run_id, title="Disbursement fee",
                                              instructions=BRIEF)
    known = TAG.findall(instructions)              # what the task context quoted, by tag
    attempts = 0
    for attempt in range(3):
        attempts += 1
        _call(engine, pid, run_id, "implement" if not attempt else f"implement-repair{attempt}", 1000)
        passed = bool(known) or attempt > 0        # told about the failure: right first time
        summary = (f"Kept the linked-account check before the fee, as [L:{known[0]}] says." if known and passed
                   else "Restored the linked-account check." if passed else "Added the fee.")
        lw.observe(engine, project_id=pid, hook="repair_checkpoint", run_id=run_id, attempt=attempt,
                   passed=passed, tests_passed=passed, output="" if passed else FAILURE, command="pytest -q",
                   failing=[] if passed else ["tests/test_disburse.py::test_disburse_without_account"],
                   files=[PATH], summary=summary)
        if passed:
            break
    with engine.begin() as c:
        c.execute(text(
            "UPDATE code_task_runs SET status = 'completed', state = jsonb_build_object('repair_attempts', "
            "CAST(:n AS int)) WHERE id = :i"), {"n": attempts - 1, "i": run_id})
        # Ingest captures the finished run (the finish trigger's queue, ingest._ingest_one).
        capture_lifecycle(c, STORE, project_id=pid, record={"kind": "run", "source_id": run_id,
                                                             "title": "Disbursement fee", "body": f"Run {run_id}"})
    clear_llm_context()
    return {"attempts": attempts, "known": known}


def test_a_build_that_needed_a_repair_round_teaches_the_next_build_on_the_same_code(pg_engine, project):
    a, b, c_ = (f"crun_{project}_{x}" for x in "abc")
    first = _scripted_build(pg_engine, project, a)
    assert first == {"attempts": 2, "known": []}
    with pg_engine.connect() as c:
        learned = {r[0]: r[1] for r in c.execute(text(
            "SELECT kind, id FROM kg_nodes WHERE project_id = :p AND node_key LIKE 'learning:%'"), {"p": project})}
    assert set(learned) == {"test_failure", "fix"}

    second = _scripted_build(pg_engine, project, b)
    assert second["attempts"] == 1 and set(second["known"]) & set(learned.values())
    with pg_engine.connect() as c:
        proof = learning_live.compare(c, project, run=b, baseline=a)
        edges = c.execute(text(
            "SELECT e.kind, d.kind FROM kg_edges e JOIN kg_nodes s ON s.id = e.src_id JOIN kg_nodes d ON d.id = e.dst_id "
            "WHERE e.project_id = :p AND s.node_key = :k AND e.kind IN ('consulted', 'reused')"),
            {"p": project, "k": f"run:{b}"}).all()
    assert ("consulted", "fix") in edges or ("consulted", "test_failure") in edges
    assert proof["run"]["steps"] < proof["baseline"]["steps"]
    assert proof["delta"]["steps"] == -1 and proof["delta"]["repair_rounds"] == -1
    assert proof["carried"] and {i["learned_in"] for i in proof["carried"]} == {a}
    assert proof["attributed"] is True

    # ARM A′: keyword search only — no learned items given, so the repair round comes back and
    # nothing is attributed.
    with pg_engine.begin() as c:
        project_settings.set_one(c, project, "knowledge_graph", "keyword")
    control = _scripted_build(pg_engine, project, c_)
    assert control == {"attempts": 2, "known": []}
    with pg_engine.connect() as c:
        verdict = learning_live.compare(c, project, run=c_, baseline=a)
    assert verdict["attributed"] is False and verdict["carried"] == []
    assert verdict["run"]["knowledge_graph"] == "keyword"


def _incident_run(engine, pid: str, wfr: str, *, title: str) -> dict:
    """An incident response, scripted: investigate (one call), then the report. The responder
    is given precedent; it cites the one that matches, and skips the long investigation."""
    with engine.begin() as c:
        found = KnowledgeService().precedent(c, project_id=pid, query=title)
    known = TAG.findall(found.get("section") or "")
    calls = 1 if known else 3
    for i in range(calls):
        _call(engine, pid, wfr, "investigate", 800)
    with engine.begin() as c:
        root = "the pool limit is too low for the 502s on /orders"
        lw.write_back(c, project_id=pid, hook="step_complete", observed={
            "step": {"workflow_run_id": wfr, "step_id": "investigate", "output": {
                "title": title, "root_cause": root,
                "hypothesis": {"basis": "no_release_near_onset", "confidence": 0.6},
                "logs_word": "read 200 lines", "diff_word": "no release",
                **({"cited_learning_ids": known[:1]} if known else {})}},
            "template": {"id": "investigate", "captain": "sre", "binding": {"capability": "investigate_incident"}}})
        lw.write_back(c, project_id=pid, hook="step_complete", observed={
            "step": {"workflow_run_id": wfr, "step_id": "report", "output": {"report_markdown": "# 502s"}},
            "template": {"id": "report", "captain": "sre", "binding": {"capability": "incident_report"}},
            "context": {"title": title, "root_cause": root, "pull_request_url": "https://example.test/pr/1",
                        "recovery_summary": "errors back to baseline"}})
    return {"calls": calls, "known": known}


def test_an_incident_teaches_the_next_incident_with_the_same_symptom(pg_engine, project):
    a, b = f"wfr_{project}_a", f"wfr_{project}_b"
    first = _incident_run(pg_engine, project, a, title="502s on /orders")
    assert first["calls"] == 3 and not first["known"]
    second = _incident_run(pg_engine, project, b, title="502s on /orders")
    assert second["calls"] == 1 and second["known"]
    with pg_engine.connect() as c:
        proof = learning_live.compare(c, project, run=b, baseline=a)
    assert proof["run"]["steps"] == 1 and proof["baseline"]["steps"] == 3
    assert proof["carried"] and proof["attributed"] is True
    reused = proof["run"]["reused"] + proof["run"]["consulted"]
    assert any(i["learned_in"] == a for i in reused)
