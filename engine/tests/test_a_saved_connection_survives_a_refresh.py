"""A connection that was made stays made.

"Even when it is connected, after refreshing or deployment it is not getting saved.. can you
pick up the one that's last saved?"

Nothing was being lost. The Codex CLI was signed in, the credential was on the shared volume
and `entitlement_sources` held a row saying so — but the wizard rendered live detection and a
Connect button, and `setup_status` counted a model as connected only when a credential
reference had been stored. A subscription has no credential to store a reference to, so the
one thing the operator had actually finished was the one thing the screen never showed.

Repo and models now renders one server view (setup_view); these tests read that view.

These tests run against a real database rather than reading the source, because the defect was
in what a query returned, and a test that greps for the clause would have passed the moment the
clause was written regardless of whether it was right.
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import setup_view
from boss_engine.services.setup_service import with_recorded_connections


class _Settings:
    """Nothing configured through the environment — so these tests measure the database."""

    github_token = ""
    target_github_owner = ""
    target_github_repo = ""
    openai_api_key = ""
    openai_base_url = ""
    cli_harnesses_json = ""


@pytest.fixture()
def connection(entitlement_tables, setup_tables):
    # The shared stubs (conftest): the rows Repo and models reads.
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as conn:
        setup_tables(conn)
        entitlement_tables(conn)
    with engine.begin() as conn:
        yield conn


def _add(conn, **row):
    row.setdefault("project_id", "p")
    row.setdefault("provider", "openai")
    row.setdefault("auth_method", "api_key")
    row.setdefault("connection_status", "not_connected")
    row.setdefault("status", "declared")
    row.setdefault("credential_secret_ref_id", None)
    row.setdefault("connection_metadata", "{}")
    row.setdefault("updated_at", datetime(2026, 9, 18, 2, 15, tzinfo=timezone.utc))
    row.setdefault("created_at", row["updated_at"])
    row.setdefault("cost_mode", "subscription" if row["connection_status"] == "connected"
                   and not row["credential_secret_ref_id"] else "usage_based_api")
    conn.execute(text(
        "INSERT INTO entitlement_sources (project_id, source_id, provider, auth_method,"
        " connection_status, status, credential_secret_ref_id, connection_metadata, updated_at,"
        " created_at, cost_mode)"
        " VALUES (:project_id, :source_id, :provider, :auth_method, :connection_status,"
        " :status, :credential_secret_ref_id, :connection_metadata, :updated_at, :created_at,"
        " :cost_mode)"), row)


def _view(connection):
    return setup_view.build_basics(connection, _Settings(), "p")


def _models(connection):
    return _view(connection)["models"]


def test_a_connected_subscription_is_a_connected_model(connection):
    """THE BUG. A Codex subscription records no credential reference — there is no credential —
    so counting stored references alone told an operator who had just watched BOSS prove a
    live turn that they still had to "Connect a model"."""
    _add(connection, source_id="openai_codex_chatgpt", auth_method="official_cli_oauth",
         connection_status="connected")

    [model] = _models(connection)
    # Listed and usable — but NOT "Working": nothing has proved it yet (BOSSAPP-769, a session
    # recorded without a proof is Not checked; the page's Test settles it).
    assert model["kind"] == "subscription" and model["pill"]["label"] == "Not checked"
    assert setup_view.basics(_view(connection))["model"] is True


def test_a_catalogue_row_nobody_connected_is_not_a_connection(connection):
    """Every project is seeded with a row for every provider BOSS knows about. Treating the
    row's existence as the answer would report a CLI nobody has ever signed in to."""
    _add(connection, source_id="cursor_cli", auth_method="official_cli_oauth")
    _add(connection, source_id="google_gemini_cli", auth_method="official_cli_oauth")

    assert _models(connection) == []
    assert setup_view.basics(_view(connection))["model"] is False


def test_a_stored_api_key_still_counts(connection):
    """The path that already worked keeps working."""
    _add(connection, source_id="setup-openrouter", provider="openrouter",
         credential_secret_ref_id="ref_1")

    [model] = _models(connection)
    assert model["label"] == "OpenRouter" and model["kind"] == "metered"
    assert setup_view.basics(_view(connection))["model"] is True


def test_a_key_saved_before_checks_existed_says_not_checked(connection):
    """No green tick over a key nobody proved: grey `Not checked`, with Test as its action."""
    _add(connection, source_id="setup-openai", credential_secret_ref_id="ref_1",
         connection_metadata='{"verification_state": "unverified"}')

    [model] = _models(connection)
    assert model["pill"]["label"] == "Not checked" and model["pill"]["action"] == "test"


def test_a_failed_check_is_the_rows_one_verdict(connection):
    _add(connection, source_id="setup-openai", credential_secret_ref_id="ref_1",
         connection_metadata='{"last_check": {"ok": false, "state": "key_rejected",'
                             ' "at": "2026-09-18T02:15:00+00:00"}}')

    [model] = _models(connection)
    assert (model["pill"]["label"], model["pill"]["tone"]) == ("Key rejected", "red")
    assert model["checked_at"].startswith("2026-09-18")


def test_the_view_says_which_one_was_saved_and_when(connection):
    """A tick beside "Connect a model" and nothing else is a wizard that has forgotten what it
    was told: the operator cannot tell whether what they connected is what is saved."""
    _add(connection, source_id="openai_codex_chatgpt", auth_method="official_cli_oauth",
         connection_status="connected")

    [model] = _models(connection)
    assert model["source_id"] == "openai_codex_chatgpt"
    assert model["provider"] == "codex" and model["label"] == "Codex"
    # Saved, never proved: no check time is invented from when the row was written.
    assert model["checked_at"] is None and model["pill"]["state"] == "not_checked"


def test_rows_keep_their_order_when_a_check_updates_one(connection):
    """Ordered by when each was connected, so a re-check does not shuffle the list."""
    _add(connection, source_id="setup-openrouter", provider="openrouter",
         credential_secret_ref_id="ref_1",
         created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
         updated_at=datetime(2026, 9, 20, tzinfo=timezone.utc))
    _add(connection, source_id="openai_codex_chatgpt", auth_method="official_cli_oauth",
         connection_status="connected",
         created_at=datetime(2026, 9, 18, tzinfo=timezone.utc))

    assert [m["source_id"] for m in _models(connection)] == [
        "setup-openrouter", "openai_codex_chatgpt"]


def test_a_saved_source_names_the_provider_it_is(connection):
    """A source_id is not a provider key: the API-key route writes `setup-<key>` and the CLI
    route writes the harness's entitlement id. The console must not have to guess."""
    _add(connection, source_id="openai_codex_chatgpt", auth_method="official_cli_oauth",
         connection_status="connected")
    _add(connection, source_id="setup-anthropic", provider="anthropic",
         credential_secret_ref_id="ref_1",
         updated_at=datetime(2026, 9, 2, tzinfo=timezone.utc))

    keys = {m["source_id"]: m["provider"] for m in _models(connection)}
    assert keys["openai_codex_chatgpt"] == "codex"
    assert keys["setup-anthropic"] == "anthropic"


# -- what the wizard's coding-agent list reports ----------------------------------------------

def _agents():
    """Shaped like detect_all's answer, with the join key detect() now exposes."""
    return [
        {"name": "codex", "ready": True, "next_step": None,
         "entitlement_source_id": "openai_codex_chatgpt"},
        {"name": "cursor", "ready": False, "next_step": "cursor-agent login",
         "entitlement_source_id": "cursor_subscription"},
        {"name": "aider", "ready": False, "next_step": "install aider",
         "entitlement_source_id": ""},
    ]


def test_the_cli_that_was_connected_says_so(connection):
    _add(connection, source_id="openai_codex_chatgpt", auth_method="official_cli_oauth",
         connection_status="connected")

    agents = {a["name"]: a for a in with_recorded_connections(_agents(), connection, "p")}

    assert agents["codex"]["connected"] is True
    assert agents["codex"]["connected_at"].startswith("2026-09-18")
    assert agents["codex"]["connected_status"] == "connected"
    assert agents["cursor"]["connected"] is False
    assert agents["cursor"]["connected_at"] is None


def test_a_seeded_catalogue_row_does_not_count_as_connected(connection):
    """The bug this endpoint would have shipped with. Every project starts with a row for every
    provider, so joining on the row alone reports Cursor as connected on a machine where
    cursor-agent has never been installed."""
    _add(connection, source_id="cursor_subscription", auth_method="official_cli_oauth")

    agents = {a["name"]: a for a in with_recorded_connections(_agents(), connection, "p")}

    assert agents["cursor"]["connected"] is False


def test_an_agent_with_no_entitlement_to_record_is_answered_not_skipped(connection):
    """aider has no subscription of its own. The field still has to be present, or the console
    reads `undefined` and renders nothing rather than "not connected"."""
    agents = {a["name"]: a for a in with_recorded_connections(_agents(), connection, "p")}

    assert agents["aider"]["connected"] is False
    assert agents["aider"]["connected_status"] is None


def test_without_a_project_the_answer_is_detection_alone(connection):
    """The record is per project. Asked without one, say nothing was connected rather than
    guessing at another project's row."""
    _add(connection, source_id="openai_codex_chatgpt", auth_method="official_cli_oauth",
         connection_status="connected")

    agents = with_recorded_connections(_agents(), connection, "")

    assert all(a["connected"] is False for a in agents)


def test_another_projects_connection_is_not_this_projects(connection):
    _add(connection, project_id="other", source_id="openai_codex_chatgpt",
         auth_method="official_cli_oauth", connection_status="connected")

    agents = {a["name"]: a for a in with_recorded_connections(_agents(), connection, "p")}

    assert agents["codex"]["connected"] is False


# -- The repository row names the repository BOSS will run on ---------------------------------

def _bind(conn, *, full_name="example-user/boss", checks=None, role="primary"):
    conn.execute(text(
        "INSERT INTO github_connections (id, project_id, connection_label, auth_method,"
        " status, account_login, created_at, updated_at) VALUES ('ghc1', 'p', 'GitHub',"
        " 'personal_access_token', 'active', 'example-user', :at, :at)"),
        {"at": datetime(2026, 9, 18, tzinfo=timezone.utc)})
    conn.execute(text(
        "INSERT INTO github_repository_bindings (id, project_id, connection_id, full_name, role,"
        " status, checks, checked_at, created_at) VALUES ('ghb1', 'p', 'ghc1', :fn, :role,"
        " 'active', :checks, :at, :at)"),
        {"fn": full_name, "role": role, "checks": checks,
         "at": datetime.now(timezone.utc) if checks else None})


def test_the_row_reports_the_connected_repository_not_the_environment(connection):
    """Measured, with a connection that was tested and passing: the card showed a shipped
    default failing instead of the repository every build used."""
    class _EnvToo(_Settings):
        github_token = "ghp_env"
        target_github_owner = "example-org"
        target_github_repo = "boss-sandbox"

    _bind(connection)
    view = setup_view.build_basics(connection, _EnvToo(), "p")
    assert [r["full_name"] for r in view["repository"]["repos"]] == ["example-user/boss"]
    assert view["repository"]["connection"]["source"] == "connected"
    assert view["repository"]["connection"]["account_login"] == "example-user"


def test_and_its_pill_is_its_last_check(connection):
    _bind(connection, checks='{"state": "working", "read": {"ok": true}, "write": {"ok": true},'
                             ' "pull_requests": {"ok": true}}')
    [repo] = _view(connection)["repository"]["repos"]
    assert repo["pill"]["label"] == "Working"
    assert repo["checks"]["pull_requests"]["ok"] is True


def test_a_404_on_a_private_repository_is_named_as_access(connection):
    """GitHub answers 404 for a private repository the token cannot see; that is a scope
    answer, and the pill says which repository to add to the token."""
    _bind(connection, checks='{"state": "no_access", "read": {"ok": false, "state": '
                             '"no_access"}}')
    [repo] = _view(connection)["repository"]["repos"]
    assert repo["pill"]["label"] == "No access"
    assert "example-user/boss" in repo["pill"]["tip"]


def test_no_install_ships_pointed_at_somebody_elses_repository():
    """`target_github_owner` shipped as "example-org" and `target_github_repo` as
    "boss-sandbox", so every install claimed to be pointed at one developer's private sandbox
    and the setup screen reported it failing — about a repository the operator has never heard
    of and cannot see."""
    from boss_engine.config import Settings

    assert Settings.model_fields["target_github_owner"].default == ""
    assert Settings.model_fields["target_github_repo"].default == ""
