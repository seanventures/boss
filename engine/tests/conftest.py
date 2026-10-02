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

from sqlalchemy import text

#: Mirrors the real entitlement_sources. SQLite types, since the stubs run in memory; JSON
#: columns are TEXT because that is how SQLAlchemy round-trips them here.
CREATE_ENTITLEMENT_SOURCES = (
    "CREATE TABLE entitlement_sources ("
    " project_id TEXT, source_id TEXT, provider TEXT, connection_status TEXT,"
    " credential_secret_ref_id TEXT, updated_at TIMESTAMP,"
    " cost_mode TEXT, limit_shape TEXT, observed_capacity TEXT,"
    " supported_models TEXT, connection_metadata TEXT)"
)

CREATE_PROJECT_SETTINGS = (
    "CREATE TABLE project_settings (project_id TEXT, key TEXT, value TEXT)"
)


def create_entitlement_tables(conn, *, with_settings: bool = True) -> None:
    """Build the stub schema connected_engines and its callers read."""
    conn.execute(text(CREATE_ENTITLEMENT_SOURCES))
    if with_settings:
        conn.execute(text(CREATE_PROJECT_SETTINGS))


import pytest


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
