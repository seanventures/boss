"""The console's role pickers must change what a build actually runs.

`code_agent_engine` was set to openrouter in the console. The screen showed openrouter. Every
build ran claude_code, because build_engine_for read `settings.code_agent_engine` — the
environment default — while the console writes `project_settings.code_agent_engine`.

"I want you to respect those settings otherwise having them in console is no use." Exactly
right: a control that cannot change what happens spends the operator's trust and returns
nothing. chat_engine_for had always read the knob; the build path never did.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")
from sqlalchemy import create_engine, text

from boss_engine.services.setup_service import build_engine_for


class _Settings:
    code_agent_engine = "claude_code"      # the environment default
    chat_llm_engine = ""
    llm_engine = "auto"
    openai_api_key = ""


@pytest.fixture()
def connection(entitlement_tables):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as conn:
        entitlement_tables(conn)
    with engine.begin() as conn:
        for source_id in ("anthropic_claude_code", "setup-openrouter"):
            conn.execute(text(
                "INSERT INTO entitlement_sources (project_id, source_id, provider,"
                " connection_status, credential_secret_ref_id, updated_at)"
                " VALUES ('p', :s, 'x', 'connected', NULL, :at)"),
                {"s": source_id, "at": datetime(2026, 9, 18, tzinfo=timezone.utc)})
        yield conn


def _choose(conn, value):
    # project_settings.value is JSONB in the real schema, so SQLAlchemy hands back a decoded
    # string. The stub stores the bare value for the same reason the sibling suites do.
    conn.execute(text("INSERT INTO project_settings (project_id, key, value)"
                      " VALUES ('p', 'code_agent_engine', :v)"), {"v": value})


def test_the_picker_decides_which_engine_writes_the_code(connection):
    _choose(connection, "openrouter")
    assert build_engine_for(connection, _Settings(), "p") == "openrouter"


def test_the_environment_default_applies_when_nobody_has_chosen(connection):
    assert build_engine_for(connection, _Settings(), "p") == "claude_code"


def test_a_choice_the_install_cannot_run_still_falls_back(connection):
    """The existing protection stands: a knob naming an unconnected harness must not produce
    a build that shells out to a binary this install has no credential for."""
    _choose(connection, "cursor")
    assert build_engine_for(connection, _Settings(), "p") in {"claude_code", "openrouter"}
