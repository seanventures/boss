"""A This-week deploy point keeps its three-word label and carries the commit it shipped.

week() already reads stack_deployments.commit_sha. The point's label stays "{Environment}
deploy" (at most three words). When that commit is present, the point also carries sha7, the
first seven characters. When it is missing, the point does not grow a commit.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from boss_engine.services.project_experience.overview import week

URL = "postgresql+psycopg://boss:boss@127.0.0.1:55944/boss_test_week_sha"
PROJECT = "proj_week_sha"
SHA = "1234567890abcdef1234567890abcdef12345678"

DDL = """
CREATE TABLE IF NOT EXISTS deployment_targets (
    id text PRIMARY KEY,
    environment text
);
CREATE TABLE IF NOT EXISTS stack_deployments (
    id text PRIMARY KEY,
    project_id text,
    target_id text,
    status text,
    finished_at timestamptz,
    commit_sha text
);
CREATE TABLE IF NOT EXISTS code_task_runs (
    id text PRIMARY KEY,
    project_id text,
    pull_request_number int,
    pull_request_url text,
    created_at timestamptz
);
CREATE TABLE IF NOT EXISTS decisions (
    id text PRIMARY KEY,
    project_id text,
    status text,
    approved_at timestamptz,
    recommendation jsonb
);
CREATE TABLE IF NOT EXISTS incidents (
    id text PRIMARY KEY,
    project_id text,
    title text,
    first_seen timestamptz
);
CREATE TABLE IF NOT EXISTS meeting_runs (
    id text PRIMARY KEY,
    project_id text,
    title text,
    directive text,
    status text,
    updated_at timestamptz
);
"""


@pytest.fixture
def connection():
    engine: Engine = create_engine(URL, future=True)
    with engine.begin() as setup:
        setup.execute(text(DDL))
    held = engine.connect()
    transaction = held.begin()
    try:
        yield held
    finally:
        transaction.rollback()
        held.close()
        engine.dispose()


def test_a_deploy_point_carries_sha7_only_when_the_commit_is_present(connection):
    now = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
    connection.execute(text(
        "INSERT INTO deployment_targets (id, environment) VALUES "
        "('t_stg', 'staging'), ('t_prod', 'production'), ('t_dev', 'dev')"))
    connection.execute(text(
        "INSERT INTO stack_deployments (id, project_id, target_id, status, finished_at, commit_sha) "
        "VALUES (:stg, :p, 't_stg', 'healthy', :ago, :sha), "
        "(:prod, :p, 't_prod', 'healthy', :later, NULL), "
        "(:dev, :p, 't_dev', 'healthy', :mid, '')"),
        {"stg": "sdep_stg", "prod": "sdep_prod", "dev": "sdep_dev", "p": PROJECT,
         "ago": now - timedelta(days=1), "later": now - timedelta(hours=1),
         "mid": now - timedelta(hours=5), "sha": SHA})

    points = {p["label"]: p for p in week(connection, PROJECT, now)}

    staged = points["Staging deploy"]
    assert staged["label"] == "Staging deploy"
    assert len(staged["label"].split()) <= 3
    assert staged["sha7"] == "1234567"
    assert staged["sha7"] != SHA
    assert "sha7" not in points["Production deploy"]
    assert points["Production deploy"]["label"] == "Production deploy"
    assert "sha7" not in points["Dev deploy"]
    assert points["Dev deploy"]["label"] == "Dev deploy"
