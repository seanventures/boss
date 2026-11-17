"""Shared test fixtures for the engine suite.

The entitlement_sources stub lives here, in ONE place.

Three test files each hand-rolled their own partial CREATE TABLE for this, listing the five
columns they happened to need. Adding `observed_capacity`, `limit_shape` and `cost_mode` to
one SELECT in connected_engines therefore broke fifteen tests across three files at once —
none of them because the behaviour was wrong, all of them because each stub was a separate
guess at the same table.

That is the same N-copies shape as the defects this suite keeps catching in the product, so
the fix is the same: the schema is written once, here, and mirrors the real table's columns.
A column added to entitlement_sources is added here and nowhere else.
"""

from __future__ import annotations

import os

# The product ships BOSS_AUTHZ_MODE=enforce (H2). The engine suite's pre-identity tests enqueue
# and lease jobs with no principal, as an upgrading install does, so the suite runs shadow
# EXPLICITLY; identity tests set the mode they test on the one settings object.
os.environ.setdefault("BOSS_AUTHZ_MODE", "shadow")

# A code task's build database arrives as a plain postgresql:// URL (build-services serves every
# language, and node/psql want that form). SQLAlchemy reads that scheme as psycopg2, which BOSS
# does not ship (psycopg 3 only), so every DB test in a build errored "No module named
# 'psycopg2'" (PR #7 follow-up, 2026-10-28) and repair rounds chased it. Name the driver here,
# once, for the variables the tests read.
def name_the_driver(environ) -> None:
    for name in ("DATABASE_URL", "BOSS_TEST_DATABASE_URL"):
        url = environ.get(name, "")
        if url.startswith(("postgresql://", "postgres://")):
            environ[name] = "postgresql+psycopg://" + url.split("://", 1)[1]


name_the_driver(os.environ)

# NOT INSIDE THE LIVE INSTALL'S SERVER (BOSSAPP-1152). The tests here create and drop databases
# and schemas on BOSS_TEST_DATABASE_URL's server. On the server the install uses
# (BOSS_DATABASE_URL, or a DATABASE_URL not named like a test database) that is allowed only for
# a database called boss_test* / boss_scratch*; anything else is refused with the way to start
# the separate test Postgres (`db-test`, compose profile `dev`).
from boss_engine.dev.test_databases import guard_test_url as _guard_test_url, session_start as _session_start  # noqa: E402

_guard_test_url(variable="BOSS_TEST_DATABASE_URL")

# A RUNAWAY QUERY FAILS FAST (BOSSAPP-1144). A test-helper catalog join ran for minutes on the
# shared bench DB and spilled 6-11 GB of temp files. libpq reads PGOPTIONS for every connection
# this process opens that does not name its own `options`, so one line covers every engine.
os.environ.setdefault("PGOPTIONS", "-c statement_timeout=60000")

from sqlalchemy import text


def pytest_sessionstart(session):
    """Storage hygiene first (BOSSAPP-1162): drop `boss_scratch_*_<stamp>` databases and
    `boss_test_*_<stamp>` schemas past their 24 h TTL on the test server — never an unstamped
    name — and warn when its lock table is too small for a schema this size. Best effort."""
    for line in _session_start(os.environ.get("BOSS_TEST_DATABASE_URL", "")):
        print(line)


# Identity needs a PERSISTENT server key and refuses to run without one (F3/L2). Tests get a fixed,
# test-only key (never a real one) unless the run names its own.
os.environ.setdefault("BOSS_IDENTITY_SERVER_KEY", "dGVzdC1vbmx5LWlkZW50aXR5LXNlcnZlci1rZXktLS0tLS0tLS0tLS0tLS0=")

# The test suites name their own credential variables (BOSS_TEST_*, BOSS_E2E_*): the operator's
# extension of the secret-reference env allowlist (services/env_refs.py), as an install would.
os.environ.setdefault("BOSS_SECRET_REF_ENV_ALLOW", "BOSS_TEST_*,BOSS_E2E_*")

#: Mirrors the real entitlement_sources. SQLite types, since the stubs run in memory; JSON
#: columns are TEXT because that is how SQLAlchemy round-trips them here.
CREATE_ENTITLEMENT_SOURCES = (
    "CREATE TABLE entitlement_sources ("
    " project_id TEXT, source_id TEXT, provider TEXT, connection_status TEXT,"
    " credential_secret_ref_id TEXT, updated_at TIMESTAMP,"
    " cost_mode TEXT, limit_shape TEXT, observed_capacity TEXT,"
    " supported_models TEXT, connection_metadata TEXT, auth_method TEXT, status TEXT,"
    " created_at TIMESTAMP)"
)

CREATE_PROJECT_SETTINGS = (
    "CREATE TABLE project_settings (project_id TEXT, key TEXT, value TEXT)"
)


#: The columns `credential_resolve.resolve_project_credential` reads: a project's stored keys.
#: The router binds a lane's stored credential through it, so every routed test reaches it.
CREATE_SECRET_REFS = (
    "CREATE TABLE secret_refs (id TEXT, project_id TEXT, provider TEXT, status TEXT,"
    " updated_at TIMESTAMP, ref_key TEXT)"
)


#: What Setup reads for the repository row (setup_view). Column names track
#: db/schema.sql for these two tables, including columns added later by ALTER
#: (role_hint, registry_repo_id, host). SQLite stubs stay nullable TEXT: a test that
#: inserts a subset of columns must still succeed. engine/tests/test_the_setup_stub_tracks_github_columns.py
#: fails when schema.sql grows a column this stub does not name.
CREATE_GITHUB_TABLES = (
    "CREATE TABLE github_connections (id TEXT, project_id TEXT, connection_label TEXT,"
    " auth_method TEXT, credential_ref TEXT, app_id TEXT, installation_id TEXT,"
    " api_base_url TEXT, status TEXT, required_permissions TEXT, granted_permissions TEXT,"
    " missing_permissions TEXT, account_login TEXT, expires_at TIMESTAMP, metadata TEXT,"
    " last_test_status TEXT, last_tested_at TIMESTAMP, last_error TEXT,"
    " disabled_at TIMESTAMP, disabled_by TEXT, revoked_at TIMESTAMP, revoked_by TEXT,"
    " created_by TEXT, created_at TIMESTAMP, updated_at TIMESTAMP)",
    "CREATE TABLE github_repository_bindings (id TEXT, project_id TEXT, connection_id TEXT,"
    " repository_id TEXT, owner TEXT, name TEXT, full_name TEXT, role TEXT,"
    " default_branch TEXT, visibility TEXT, archived TEXT, status TEXT,"
    " permissions TEXT, capabilities TEXT, metadata TEXT, checks TEXT, checked_at TIMESTAMP,"
    " last_synced_at TIMESTAMP, created_by TEXT, created_at TIMESTAMP, updated_at TIMESTAMP,"
    " role_hint TEXT, registry_repo_id TEXT, host TEXT)",
    "CREATE TABLE events (id TEXT, project_id TEXT, event_type TEXT, actor TEXT,"
    " entity_type TEXT, entity_id TEXT, data TEXT, created_at TIMESTAMP)",
)


def create_setup_tables(conn) -> None:
    """The stub schema Setup's rows read (`setup_view.build_basics`)."""
    for statement in CREATE_GITHUB_TABLES:
        conn.execute(text(statement))


def create_entitlement_tables(conn, *, with_settings: bool = True) -> None:
    """Build the stub schema connected_engines and its callers read."""
    conn.execute(text(CREATE_ENTITLEMENT_SOURCES))
    conn.execute(text(CREATE_SECRET_REFS))
    if with_settings:
        conn.execute(text(CREATE_PROJECT_SETTINGS))


import pytest


@pytest.fixture()
def setup_tables():
    """`create_setup_tables`, as a fixture (see `entitlement_tables` for why a fixture)."""
    return create_setup_tables


@pytest.fixture()
def entitlement_tables():
    """The stub-table builder, as a fixture so every test file reaches the same schema.

    A conftest fixture rather than an importable module: `engine/tests` is not on sys.path
    under the suite's own invocation, and a helper that only imports when you happen to run
    one file is a helper that breaks the full run.
    """
    return create_entitlement_tables


import pytest


@pytest.fixture(autouse=True)
def _no_real_cli_model_catalog(monkeypatch):
    """Never ask a real coding CLI for its model catalog during a test.

    The product asks each CLI which models the signed-in account has, so on a machine with a
    signed-in Codex or Claude every picker test would see that account instead of the list it
    asserts on. This stands in for "what the CLI reported" — test data, not a product list.
    A test about the readers themselves undoes it (`monkeypatch.undo()`).
    """
    from boss_engine.services import cli_harness

    reported = {"claude_code": ("opus", "sonnet", "fable", "haiku"),
                "codex": ("gpt-5.2-codex", "o3")}
    monkeypatch.setattr(cli_harness, "_read_model_catalog",
                        lambda spec: reported.get(spec.name, ()), raising=False)
    monkeypatch.setattr(cli_harness, "_catalog_cache", {}, raising=False)


@pytest.fixture(autouse=True)
def _no_real_recovery_model(monkeypatch):
    """A failed step's recovery agent (step_recovery) never calls a real model in a test. It gets
    a SCRIPTED model that answers with BOSS's own triage (handed to it as `boss_triage`), so the
    runner's executors are exercised as a model would drive them. With no model at all the agent
    does nothing and asks a person — a test about that makes `routed_adapter` raise."""
    import json

    from boss_engine.services import step_recovery

    class _TriageFollowingModel:
        def create_chat_completion(self, messages, tools=None, **_k):
            body = str(messages[1]["content"])
            at = body.index('"boss_triage":') + len('"boss_triage":')
            triage, _ = json.JSONDecoder().raw_decode(body[at:].lstrip())
            choice = {k: v for k, v in triage.items() if k != "suggests"}
            choice.update({"action": triage.get("suggests"), "confidence": 0.9})
            return {"content": json.dumps(choice, default=str)}, {"model": "scripted-triage"}

    def scripted(*_a, **_k):
        model = _TriageFollowingModel()
        return model, model

    monkeypatch.setattr(step_recovery, "routed_adapter", scripted)


def _stub(name: str):
    """A module from tests/e2e/stubs, loaded by path (that folder is not a package)."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "tests" / "e2e" / "stubs" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_stub_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def github_probes():
    """The fake GitHub's answers to Setup's probes, per token profile."""
    return _stub("github_probes")


@pytest.fixture()
def model_probes():
    """The model stub's answers to Setup's one-question check, per key."""
    return _stub("model_probes")


@pytest.fixture(scope="session", autouse=True)
def _default_project_exists_for_the_suite():
    """Some DB tests (e.g. workflow revert tests) name the default project (`boss-dogfood`)
    directly instead of creating their own, assuming it was seeded before the suite ran. A
    build's throwaway database starts empty, so without this an otherwise-correct test fails
    on a foreign key, not on the behaviour it is testing. Idempotent (ON CONFLICT DO NOTHING):
    a real seed that already ran leaves this a no-op.
    """
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith(("postgresql://", "postgresql+psycopg://")):
        return
    # Only a database named like a test one (apps/api/tests/db_guard.py's rule): DATABASE_URL is
    # production inside a deployed container, and a session-wide autouse fixture writing there is
    # the 2026-09-02 incident's shape. A build's boss_run_<hex> database is not test-named either;
    # it gets its own test database (Green the gate part 2), not an exception here.
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if not any(marker in name for marker in ("test", "testing", "ci", "scratch", "tmp", "boss_run")):
        return
    from sqlalchemy import create_engine
    from sqlalchemy.exc import SQLAlchemyError

    from boss_engine.config import get_settings
    from boss_engine.db import ensure_schema

    settings = get_settings()
    engine = create_engine(url, future=True)
    try:
        ensure_schema(engine, settings)
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO orgs (id, name) VALUES (:id, 'BOSS') ON CONFLICT (id) DO NOTHING"),
                {"id": settings.default_org_id})
            connection.execute(text(
                "INSERT INTO workspaces (id, org_id, name) VALUES (:id, :org_id, 'Default workspace') "
                "ON CONFLICT (id) DO NOTHING"),
                {"id": settings.default_workspace_id, "org_id": settings.default_org_id})
            connection.execute(text(
                "INSERT INTO projects (id, workspace_id, name, slug) "
                "VALUES (:id, :workspace_id, 'Default project', 'boss-dogfood') "
                "ON CONFLICT (id) DO NOTHING"),
                {"id": settings.default_project_id, "workspace_id": settings.default_workspace_id})
    except SQLAlchemyError:
        pass
    finally:
        engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def _observability_targets_do_not_outlive_the_session():
    """Observability seeds write deployment_targets on BOSS_TEST_DATABASE_URL and used to
    leave them there for the next suite (BOSSAPP-973). Drop only ids added after the first
    `pg_engine()` in this process, once every test — including a module-scoped seed — is done."""
    yield
    from ._obs_seed import drop_targets_added_after_snapshot

    drop_targets_added_after_snapshot()


@pytest.fixture(autouse=True)
def _fresh_model_router():
    """The model router caches snapshots and classifications per process (model_router.store,
    .classifier). A test's project must not inherit another test's mode or policy."""
    from boss_engine.services.model_router import store
    from boss_engine.services.model_router.classifier import CLASSIFIER

    from boss_engine.services.model_router import accounts, breaker

    store.invalidate()
    CLASSIFIER.clear()
    accounts.reset_caches()
    breaker.reset()
    yield
    store.invalidate()
    accounts.reset_caches()
    breaker.reset()


# ── no test reaches a real model CLI ─────────────────────────────────────────────────────────
class RealModelCliBlocked(RuntimeError):
    """A test tried to start the real `claude` or `codex` CLI."""


_MODEL_CLIS = ("claude", "codex")


@pytest.fixture(autouse=True)
def _no_real_model_cli(request, monkeypatch):
    """A test never spends a real model: starting `claude` or `codex` (however it is reached —
    a decider's CLI fallback, an adapter, a subprocess) raises. A test that truly needs the CLI
    opts in with @pytest.mark.real_model_cli."""
    if request.node.get_closest_marker("real_model_cli"):
        return
    import subprocess

    real_init = subprocess.Popen.__init__

    def guarded(self, args, *a, **k):
        argv = args if isinstance(args, (list, tuple)) else str(args).split()
        first = os.path.basename(str(argv[0])) if argv else ""
        if first in _MODEL_CLIS:
            raise RealModelCliBlocked(
                f"a test tried to start the real `{first}` CLI; stub the model seat, or mark "
                "the test @pytest.mark.real_model_cli")
        return real_init(self, args, *a, **k)

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded)


def pytest_configure(config):
    config.addinivalue_line("markers", "real_model_cli: the test may start the real claude/codex CLI")
