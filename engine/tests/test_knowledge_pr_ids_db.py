"""A pull request's knowledge id names its repository (operator decision 2026-10-22).

Breaks covered: PR #15 keyed a pull request as ``pr-<n>``, so PR 9 of one repository and PR 9 of
another in the same project became ONE node, and the second PR's change links landed on the
first's record; the software factory building that unqualified id; old ``pr-<n>`` ids left
behind after the change (the migration rewrites them where the project's runs name one
repository, and leaves a number two repositories share alone).
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest
from boss_engine.db import ensure_schema
from boss_engine.services.knowledge.capture import capture_lifecycle, pull_request_source_id
from boss_engine.services.knowledge.store import PostgresKnowledgeStore
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

STORE = PostgresKnowledgeStore()
ROOT = Path(__file__).resolve().parents[2]


def test_the_id_names_the_repository_and_the_number():
    assert pull_request_source_id("Acme/Shop", 9) == "pr-acme/shop#9"
    assert pull_request_source_id("acme/api", 9) != pull_request_source_id("acme/shop", 9)
    assert pull_request_source_id("", 9) == "pr-9"          # nothing to qualify with
    with pytest.raises(ValueError):
        pull_request_source_id("acme/shop", "")


def test_the_software_factory_builds_the_qualified_id():
    """No caller formats ``pr-<n>`` itself: the one capture point goes through the helper."""
    source = (ROOT / "engine/boss_engine/services/software_factory.py").read_text()
    tree = ast.parse(source)
    unqualified = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr) and node.values
        and isinstance(node.values[0], ast.Constant) and str(node.values[0].value) == "pr-"
    ]
    assert not unqualified, f"software_factory.py formats an unqualified pr-<n> at lines {unqualified}"
    assert "pull_request_source_id(" in source


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
            conn.execute(text("INSERT INTO orgs (id, name) VALUES ('org_pi', 'PI')"))
            conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_pi', 'org_pi', 'PI')"))
            conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES ('proj_pi', 'ws_pi', 'PI', 'pi')"))
            yield conn
        finally:
            transaction.rollback()


def _py(path: str, source: str) -> dict:
    return {"path": path, "language": "python", "source": source}


def _capture(connection, source_id: str, path: str, fn: str) -> dict:
    return capture_lifecycle(connection, STORE, project_id="proj_pi", record={
        "kind": "pull_request", "source_id": source_id, "title": f"change {fn}", "body": f"Changes {fn}",
        "files": [_py(path, f"def {fn}():\n    return 1\n")],
    })


def _pr_keys(connection) -> list[str]:
    return connection.execute(text(
        "SELECT node_key FROM kg_nodes WHERE project_id = 'proj_pi' AND kind = 'pull_request' "
        "ORDER BY node_key")).scalars().all()


def test_pr_9_of_two_repositories_is_two_records(connection):
    _capture(connection, pull_request_source_id("acme/shop", 9), "shop.py", "checkout")
    _capture(connection, pull_request_source_id("acme/api", 9), "api.py", "serve")
    assert _pr_keys(connection) == ["pull_request:pr-acme/api#9", "pull_request:pr-acme/shop#9"]
    links = connection.execute(text(
        "SELECT source_id, count(*) FROM kg_change_links WHERE project_id = 'proj_pi' "
        "GROUP BY source_id ORDER BY source_id")).all()
    assert [tuple(r) for r in links] == [("pr-acme/api#9", 1), ("pr-acme/shop#9", 1)]


def _run(connection, run_id: str, number: int, url: str) -> None:
    connection.execute(text(
        "INSERT INTO code_task_runs (id, project_id, title, status, spec, state, pull_request_number, "
        "pull_request_url) VALUES (:i, 'proj_pi', 't', 'completed', '{}'::jsonb, '{}'::jsonb, :n, :u)"),
        {"i": run_id, "n": number, "u": url})


def test_the_migration_qualifies_old_ids_and_leaves_a_shared_number_alone(connection):
    _capture(connection, "pr-9", "shop.py", "checkout")      # one repository has a PR 9
    _capture(connection, "pr-4", "cart.py", "add")           # two repositories have a PR 4
    _run(connection, "run_pi_1", 9, "https://github.com/Acme/Shop/pull/9")
    _run(connection, "run_pi_2", 4, "https://github.com/acme/shop/pull/4")
    _run(connection, "run_pi_3", 4, "https://github.com/acme/api/pull/4")
    assert connection.execute(text("SELECT kg_qualify_pr_source_ids()")).scalar_one() > 0
    assert _pr_keys(connection) == ["pull_request:pr-4", "pull_request:pr-acme/shop#9"]
    assert connection.execute(text(
        "SELECT attrs->>'source_id' FROM kg_nodes WHERE project_id = 'proj_pi' "
        "AND node_key = 'pull_request:pr-acme/shop#9'")).scalar_one() == "pr-acme/shop#9"
    assert connection.execute(text(
        "SELECT source_id FROM kg_captures WHERE project_id = 'proj_pi' AND source_kind = 'pull_request' "
        "ORDER BY source_id")).scalars().all() == ["pr-4", "pr-acme/shop#9"]
    assert connection.execute(text(
        "SELECT DISTINCT source_id FROM kg_change_links WHERE project_id = 'proj_pi' "
        "ORDER BY source_id")).scalars().all() == ["pr-4", "pr-acme/shop#9"]
    assert connection.execute(text("SELECT kg_qualify_pr_source_ids()")).scalar_one() == 0   # converged
