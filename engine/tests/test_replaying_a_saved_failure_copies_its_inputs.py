"""Replaying a saved failure copies that run's inputs into a new run.

One click writes one new receipt, under a new dedupe key, and reserves once. The failed
run's id is not reused. A run that did not fail does not start a run. The builder is not
started: no step rows and no code-task job.
"""
from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services.failure_replay import replay

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL",
    "postgresql+psycopg://boss:boss@127.0.0.1:55926/boss_test_replay",
)


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True, pool_pre_ping=True)
    from boss_engine.db import ensure_schema

    ensure_schema(engine)
    return engine


def _project(connection) -> tuple[str, str, str]:
    stamp = uuid.uuid4().hex[:10]
    org, workspace, project = f"org_{stamp}", f"ws_{stamp}", f"p_{stamp}"
    connection.execute(
        text("INSERT INTO orgs (id, name) VALUES (:id, 'replay')"), {"id": org})
    connection.execute(
        text("INSERT INTO workspaces (id, org_id, name) VALUES (:id, :org, 'replay')"),
        {"id": workspace, "org": org})
    connection.execute(
        text("INSERT INTO projects (id, workspace_id, name, slug) "
             "VALUES (:id, :ws, 'replay', :slug)"),
        {"id": project, "ws": workspace, "slug": project})
    return org, workspace, project


def _version(connection, project: str) -> tuple[str, str]:
    definition, version = f"wfd_{uuid.uuid4().hex[:12]}", f"wfv_{uuid.uuid4().hex[:12]}"
    connection.execute(
        text("INSERT INTO workflow_definitions (id, project_id, workflow_key, name, "
             "created_by) VALUES (:id, :p, :key, 'Saved failure', 'ada')"),
        {"id": definition, "p": project, "key": f"saved-{definition}"})
    connection.execute(
        text("INSERT INTO workflow_versions (id, project_id, workflow_definition_id, "
             "version, spec, content_hash, created_by) VALUES (:id, :p, :d, 1, "
             "CAST(:spec AS JSONB), :hash, 'ada')"),
        {"id": version, "p": project, "d": definition,
         "spec": json.dumps({"entry_step": "ask", "steps": []}),
         "hash": f"hash-{version}"})
    return definition, version


def _run(connection, project: str, definition: str, version: str, *, status: str,
         inputs: dict, dedupe_key: str) -> str:
    run_id = f"wfr_{uuid.uuid4().hex[:16]}"
    connection.execute(
        text("INSERT INTO workflow_runs (id, project_id, workflow_definition_id, "
             "workflow_version_id, actor, status, input, dedupe_key) "
             "VALUES (:id, :p, :d, :v, 'old', :status, CAST(:input AS JSONB), :dk)"),
        {"id": run_id, "p": project, "d": definition, "v": version, "status": status,
         "input": json.dumps(inputs), "dk": dedupe_key})
    return run_id


def _receipt(connection, org: str, workspace: str, project: str, dedupe_key: str) -> None:
    connection.execute(
        text("INSERT INTO receipts (id, org_id, workspace_id, project_id, dedupe_key, "
             "effect_kind, effect_scope, payload_hash, status) "
             "VALUES (:id, :org, :ws, :p, :dk, 'workflow.run', '{}'::jsonb, :hash, "
             "'failed')"),
        {"id": f"rcpt_{uuid.uuid4().hex[:16]}", "org": org, "ws": workspace, "p": project,
         "dk": dedupe_key, "hash": "saved-failure"})


def _counts(connection, project: str) -> dict[str, int]:
    runs = connection.execute(
        text("SELECT count(*) FROM workflow_runs WHERE project_id = :p"),
        {"p": project}).scalar()
    steps = connection.execute(
        text("SELECT count(*) FROM workflow_step_runs WHERE project_id = :p"),
        {"p": project}).scalar()
    reservations = connection.execute(
        text("SELECT count(*) FROM budget_reservations WHERE project_id = :p"),
        {"p": project}).scalar()
    receipts = connection.execute(
        text("SELECT count(*) FROM receipts WHERE project_id = :p"),
        {"p": project}).scalar()
    jobs = connection.execute(
        text("SELECT count(*) FROM jobs WHERE project_id = :p"), {"p": project}).scalar()
    tasks = connection.execute(
        text("SELECT count(*) FROM code_task_runs WHERE project_id = :p"),
        {"p": project}).scalar()
    return {"runs": int(runs), "steps": int(steps), "reservations": int(reservations),
            "receipts": int(receipts), "jobs": int(jobs), "tasks": int(tasks)}


def _purge(engine, project: str, org: str) -> None:
    with engine.begin() as connection:
        for statement in (
            "DELETE FROM budget_reservations WHERE project_id = :p",
            "DELETE FROM receipts WHERE project_id = :p",
            "DELETE FROM policy_eval_log WHERE project_id = :p",
            "DELETE FROM events WHERE project_id = :p",
            "DELETE FROM workflow_step_runs WHERE project_id = :p",
            "DELETE FROM jobs WHERE project_id = :p",
            "DELETE FROM code_task_runs WHERE project_id = :p",
            "DELETE FROM workflow_runs WHERE project_id = :p",
            "DELETE FROM workflow_versions WHERE project_id = :p",
            "DELETE FROM workflow_definitions WHERE project_id = :p",
            "DELETE FROM projects WHERE id = :p",
        ):
            connection.execute(text(statement), {"p": project})
        connection.execute(text("DELETE FROM workspaces WHERE org_id = :org"), {"org": org})
        connection.execute(text("DELETE FROM orgs WHERE id = :org"), {"org": org})


def test_replaying_a_saved_failure_copies_inputs_reserves_once_and_skips_a_pass(eng):
    inputs = {"request": "POST /orders", "job": "charge", "body": {"id": "ord_1"}}
    with eng.begin() as connection:
        org, workspace, project = _project(connection)
        definition, version = _version(connection, project)
        failed = _run(connection, project, definition, version, status="failed",
                      inputs=inputs, dedupe_key=f"saved-run:{project}")
        _receipt(connection, org, workspace, project, f"saved-failure:{failed}")
        finished = _run(connection, project, definition, version, status="completed",
                        inputs={"request": "GET /health"}, dedupe_key=f"saved-ok:{project}")

    try:
        with eng.begin() as connection:
            outcome = replay(connection, project_id=project, run_id=failed, actor="ada",
                             click_id="click-1")
            assert outcome["created"] is True
            new_id = outcome["run_id"]
            assert new_id and new_id != failed
            row = connection.execute(
                text("SELECT id, status, input, dedupe_key FROM workflow_runs WHERE id = :id"),
                {"id": new_id}).mappings().one()
            assert dict(row["input"]) == inputs
            assert row["dedupe_key"] != f"saved-run:{project}"
            assert row["status"] != "failed"
            source = connection.execute(
                text("SELECT id, status, input FROM workflow_runs WHERE id = :id"),
                {"id": failed}).mappings().one()
            assert source["status"] == "failed"
            assert dict(source["input"]) == inputs
            receipt = connection.execute(
                text("SELECT dedupe_key, status, project_id FROM receipts WHERE id = :id"),
                {"id": outcome["receipt_id"]}).mappings().one()
            assert receipt["dedupe_key"] == outcome["dedupe_key"]
            assert receipt["dedupe_key"] != f"saved-failure:{failed}"
            assert receipt["dedupe_key"] != f"saved-run:{project}"
            assert receipt["status"] == "succeeded"
            assert receipt["project_id"] == project
            held = connection.execute(
                text("SELECT id, status FROM budget_reservations WHERE id = :id"),
                {"id": outcome["reservation_id"]}).mappings().one()
            assert held["status"] == "active"
            counts = _counts(connection, project)
            assert counts == {"runs": 3, "steps": 0, "reservations": 1, "receipts": 2,
                              "jobs": 0, "tasks": 0}

            again = replay(connection, project_id=project, run_id=failed, actor="ada",
                           click_id="click-1")
            assert again["created"] is False
            assert again["run_id"] == new_id
            assert again["dedupe_key"] == outcome["dedupe_key"]
            assert _counts(connection, project)["reservations"] == 1
            assert _counts(connection, project)["runs"] == 3
            assert _counts(connection, project)["receipts"] == 2

            skipped = replay(connection, project_id=project, run_id=finished, actor="ada",
                             click_id="click-2")
            assert skipped["created"] is False
            assert skipped["run_id"] is None
            assert _counts(connection, project) == {
                "runs": 3, "steps": 0, "reservations": 1, "receipts": 2,
                "jobs": 0, "tasks": 0}
    finally:
        _purge(eng, project, org)
