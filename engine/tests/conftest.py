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

from sqlalchemy import text

# Identity needs a PERSISTENT server key and refuses to run without one (F3/L2). Tests get a fixed,
# test-only key (never a real one) unless the run names its own.
os.environ.setdefault("BOSS_IDENTITY_SERVER_KEY", "dGVzdC1vbmx5LWlkZW50aXR5LXNlcnZlci1rZXktLS0tLS0tLS0tLS0tLS0=")

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
    " updated_at TIMESTAMP)"
)


#: What Repo and models reads for the repository row (setup_view), mirroring the real columns.
CREATE_GITHUB_TABLES = (
    "CREATE TABLE github_connections (id TEXT, project_id TEXT, connection_label TEXT,"
    " auth_method TEXT, credential_ref TEXT, api_base_url TEXT, status TEXT,"
    " account_login TEXT, expires_at TIMESTAMP, last_test_status TEXT,"
    " last_tested_at TIMESTAMP, last_error TEXT, created_at TIMESTAMP, updated_at TIMESTAMP)",
    "CREATE TABLE github_repository_bindings (id TEXT, project_id TEXT, connection_id TEXT,"
    " full_name TEXT, role TEXT, default_branch TEXT, visibility TEXT, status TEXT,"
    " checks TEXT, checked_at TIMESTAMP, created_at TIMESTAMP, updated_at TIMESTAMP)",
    "CREATE TABLE events (id TEXT, project_id TEXT, event_type TEXT, actor TEXT,"
    " entity_type TEXT, entity_id TEXT, data TEXT, created_at TIMESTAMP)",
)


def create_setup_tables(conn) -> None:
    """The stub schema Repo and models' rows read (`setup_view.build_basics`)."""
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
    """The fake GitHub's answers to Repo and models' probes, per token profile."""
    return _stub("github_probes")


@pytest.fixture()
def model_probes():
    """The model stub's answers to Repo and models' one-question check, per key."""
    return _stub("model_probes")


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
