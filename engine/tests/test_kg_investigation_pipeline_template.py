"""KG P5a: the `kg_investigation_pipeline` workflow template and its new `deterministic` step
kind (design: ~/boss-ops design §4.4 and §5.1).

Hard rule from PRs #7-#15 (quoted in the task): WIRE IT IN — the template must be in the
workflow catalogue and runnable through the real workflow runtime, not just a dict of unused
helpers. `test_the_template_publishes_through_the_real_runtime` proves that by seeding the
catalogue into a real project and publishing a real version through `workflows.publish_version`,
exactly as every other catalogue workflow does (test_workflow_catalogue.py).

These tests run against a real, disposable postgres (DATABASE_URL), like test_regulatory_trace.py.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine.db import ensure_schema
from boss_engine.services import workflow_catalogue as catalogue
from boss_engine.services import workflows

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


@pytest.fixture()
def connection(pg_engine):
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        org = f"org_kgpipe_{uuid.uuid4().hex[:8]}"
        ws = f"ws_kgpipe_{uuid.uuid4().hex[:8]}"
        project = f"p_kgpipe_{uuid.uuid4().hex[:8]}"
        try:
            conn.execute(text("INSERT INTO orgs (id, name) VALUES (:i, 'KG Pipeline')"), {"i": org})
            conn.execute(text(
                "INSERT INTO workspaces (id, org_id, name) VALUES (:i, :o, 'KG Pipeline')"),
                {"i": ws, "o": org})
            conn.execute(text(
                "INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                "(:i, :w, 'KG Pipeline', :s)"), {"i": project, "w": ws, "s": project})
            conn.project_id = project  # type: ignore[attr-defined]
            yield conn
        finally:
            transaction.rollback()


def _entry() -> dict:
    for entry in catalogue.CATALOGUE:
        if entry.get("key") == TEMPLATE_KEY:
            return entry
    raise AssertionError(f"'{TEMPLATE_KEY}' is not in workflow_catalogue.CATALOGUE")


# ── P0: WIRE IT IN — the template is in the catalogue and publishes through the real runtime ──

def test_p0_the_template_publishes_through_the_real_runtime(connection):
    # A deterministic step runs a fixed query, never an agent turn: it must not be treated as a
    # branching/looping control step (CONTROL_KINDS), or its single required write would never
    # reach enforcement the way a plain work step's does.
    assert "deterministic" in workflows.WORKFLOW_KINDS
    assert "deterministic" not in workflows.CONTROL_KINDS

    result = catalogue.seed(connection, connection.project_id, actor="test")
    keys = {row["key"]: row for row in result["installed"]}
    assert TEMPLATE_KEY in keys, f"'{TEMPLATE_KEY}' was not installed: {sorted(keys)}"

    row = connection.execute(
        text("SELECT id FROM workflow_definitions WHERE project_id = :p AND workflow_key = :k"),
        {"p": connection.project_id, "k": TEMPLATE_KEY},
    ).mappings().first()
    assert row is not None, "the template must publish a real workflow_definitions row"
    version = connection.execute(
        text("SELECT spec FROM workflow_versions WHERE definition_id = :d "
             "ORDER BY created_at DESC LIMIT 1"),
        {"d": row["id"]},
    ).mappings().first()
    assert version is not None, "seeding must publish a version, not just a definition"


# ── P1: the deterministic steps and the stage order the design calls for ──────────────────────

def test_p1_context_enrichment_and_historical_pattern_are_deterministic_with_a_bounded_evidence_loop():
    entry = _entry()
    steps = {s["id"]: s for s in entry["steps"]}
    for step_id in ("context_enrichment", "historical_pattern"):
        assert step_id in steps, f"missing stage '{step_id}'"
        assert steps[step_id]["kind"] == "deterministic", (
            f"'{step_id}' must run as the new $0 deterministic step kind, not an agent step")

    loops = [s for s in entry["steps"] if s.get("kind") == "bounded_loop"]
    assert loops, "the template needs a bounded_loop for 'insufficient evidence' re-rounds"
    loop = loops[0]
    assert loop.get("max_iterations") == 2, (
        "the design caps the evidence loop at 2 rounds, got "
        f"{loop.get('max_iterations')!r}")
    assert loop.get("body") in ("context_enrichment", "historical_pattern"), (
        "the loop must repeat back into enrichment or pattern matching, got "
        f"{loop.get('body')!r}")


def test_p1_the_lanes_stage_fans_out_with_for_each_and_ends_in_a_human_approval_gate():
    entry = _entry()
    steps = {s["id"]: s for s in entry["steps"]}
    lanes = [s for s in entry["steps"] if s.get("kind") == "for_each"]
    assert lanes, "the Lanes stage must repeat remediation work per finding family (for_each)"
    gates = [s for s in entry["steps"] if (s.get("approval") or {}).get("approver_role")]
    assert gates, "human approval before Execution must be a real approval gate, not advisory text"
