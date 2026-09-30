"""A connection that was made stays made.

"Even when it is connected, after refreshing or deployment it is not getting saved.. can you
pick up the one that's last saved?"

Nothing was being lost. The Codex CLI was signed in, the credential was on the shared volume
and `entitlement_sources` held a row saying so — but the wizard rendered live detection and a
Connect button, and `setup_status` counted a model as connected only when a credential
reference had been stored. A subscription has no credential to store a reference to, so the
one thing the operator had actually finished was the one thing the screen never showed.

These tests run against a real database rather than reading the source, because the defect was
in what a query returned, and a test that greps for the clause would have passed the moment the
clause was written regardless of whether it was right.
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services.setup_service import setup_status, with_recorded_connections


class _Settings:
    """Nothing configured through the environment — so these tests measure the database."""

    github_token = ""
    target_github_owner = ""
    target_github_repo = ""
    openai_api_key = ""
    openai_base_url = ""
    cli_harnesses_json = ""


@pytest.fixture()
def connection():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE github_repository_bindings "
                          "(project_id TEXT, status TEXT)"))
        conn.execute(text("CREATE TABLE github_connections (project_id TEXT, status TEXT)"))
        conn.execute(text(
            "CREATE TABLE entitlement_sources ("
            " project_id TEXT, source_id TEXT, provider TEXT, auth_method TEXT,"
            " connection_status TEXT, status TEXT, credential_secret_ref_id TEXT,"
            " connection_metadata TEXT, updated_at TIMESTAMP)"))
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
    conn.execute(text(
        "INSERT INTO entitlement_sources (project_id, source_id, provider, auth_method,"
        " connection_status, status, credential_secret_ref_id, connection_metadata, updated_at)"
        " VALUES (:project_id, :source_id, :provider, :auth_method, :connection_status,"
        " :status, :credential_secret_ref_id, :connection_metadata, :updated_at)"), row)


def _model_step(status):
    return next(s for s in status["steps"] if s["id"] == "model")


def test_a_connected_subscription_is_a_connected_model(connection):
    """THE BUG. A Codex subscription records no credential reference — there is no credential —
    so counting stored references alone told an operator who had just watched BOSS prove a
    live turn that they still had to "Connect a model"."""
    _add(connection, source_id="openai_codex_chatgpt", auth_method="official_cli_oauth",
         connection_status="connected")

    status = setup_status(connection, _Settings(), "p")

    assert _model_step(status)["done"] is True
    assert status["model_sources"] == 1


def test_a_catalogue_row_nobody_connected_is_not_a_connection(connection):
    """Every project is seeded with a row for every provider BOSS knows about. Treating the
    row's existence as the answer would report a CLI nobody has ever signed in to."""
    _add(connection, source_id="cursor_cli", auth_method="official_cli_oauth")
    _add(connection, source_id="google_gemini_cli", auth_method="official_cli_oauth")

    status = setup_status(connection, _Settings(), "p")

    assert _model_step(status)["done"] is False
    assert status["model_saved"] == []


def test_a_stored_api_key_still_counts(connection):
    """The path that already worked keeps working."""
    _add(connection, source_id="setup-openrouter", provider="openrouter",
         credential_secret_ref_id="ref_1")

    status = setup_status(connection, _Settings(), "p")

    assert _model_step(status)["done"] is True


def test_a_saved_key_is_configured_but_explicitly_unverified(connection):
    _add(connection, source_id="setup-openai", credential_secret_ref_id="ref_1",
         status="verified",
         connection_metadata='{"verification_state": "unverified"}')

    status = setup_status(connection, _Settings(), "p")

    assert status["ready"] is False  # A repository is still required.
    assert _model_step(status)["done"] is True
    assert status["model_saved"][0]["verified"] is False
    assert status["model_probe_available"] is False


def test_the_status_says_which_one_was_saved_and_when(connection):
    """A tick beside "Connect a model" and nothing else is a wizard that has forgotten what it
    was told: the operator cannot tell whether what they connected is what is saved."""
    _add(connection, source_id="openai_codex_chatgpt", auth_method="official_cli_oauth",
         connection_status="connected")

    saved = setup_status(connection, _Settings(), "p")["model_saved"]

    assert len(saved) == 1
    assert saved[0]["source_id"] == "openai_codex_chatgpt"
    assert saved[0]["kind"] == "subscription"
    assert saved[0]["connected"] is True
    assert saved[0]["saved_at"].startswith("2026-09-18")


def test_the_last_saved_one_comes_first(connection):
    """"Can you pick up the one that's last saved" — so the order is the answer, and the
    console reselects the head of this list."""
    _add(connection, source_id="setup-openrouter", provider="openrouter",
         credential_secret_ref_id="ref_1",
         updated_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    _add(connection, source_id="openai_codex_chatgpt", auth_method="official_cli_oauth",
         connection_status="connected",
         updated_at=datetime(2026, 9, 18, tzinfo=timezone.utc))

    saved = setup_status(connection, _Settings(), "p")["model_saved"]

    assert [s["source_id"] for s in saved] == ["openai_codex_chatgpt", "setup-openrouter"]


def test_a_saved_source_names_the_dropdown_entry_it_is(connection):
    """A source_id is not a wizard provider key: the API-key route writes `setup-<key>` and the
    CLI route writes the harness's entitlement id. The console must not have to guess."""
    _add(connection, source_id="openai_codex_chatgpt", auth_method="official_cli_oauth",
         connection_status="connected")
    _add(connection, source_id="setup-anthropic", provider="anthropic",
         credential_secret_ref_id="ref_1",
         updated_at=datetime(2026, 9, 2, tzinfo=timezone.utc))

    saved = setup_status(connection, _Settings(), "p")["model_saved"]
    keys = {s["source_id"]: s["wizard_provider"] for s in saved}

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


# -- "What BOSS is running on" must name what BOSS will run on ---------------------------------

def test_the_card_reports_the_connected_repository_not_the_environment():
    """Measured, with a connection that was tested and passing:

        REPOSITORY  CHECK FAILED
        example-org/boss-sandbox · token …ThOQ — GitHub has no example-org/boss-sandbox
        visible to this token.

    The project was bound to example-user/boss, every build would use it, and the card showed a
    shipped default failing instead. A screen called "What BOSS is running on" has to name the
    repository BOSS will run on.
    """
    import inspect

    from boss_engine.services import setup_service

    source = inspect.getsource(setup_service.setup_status)
    assert "check_github_for_project(connection, settings, project_id)" in source


def test_and_says_which_of_the_two_it_is():
    """They are different facts. A card that shows one while implying the other is how an
    operator concludes their working connection did not take."""
    import inspect

    from boss_engine.services.setup_service import check_github_for_project

    source = inspect.getsource(check_github_for_project)
    assert '"source": "connected"' in source
    assert "return check_github(settings)" in source          # nothing bound: the env IS it


def test_it_checks_with_the_credential_the_repository_was_connected_with():
    """Checking a connected repository with the environment's token would report a failure
    about a credential the project does not use."""
    import inspect

    from boss_engine.services.setup_service import check_github_for_project

    source = inspect.getsource(check_github_for_project)
    assert "_resolve_secret(secret, connection)" in source
    assert 'permissions.get("push")' in source


def test_a_404_on_a_private_repository_is_explained_here_too():
    import inspect

    from boss_engine.services.setup_service import check_github_for_project

    assert "for a private" in inspect.getsource(check_github_for_project)


def test_no_install_ships_pointed_at_somebody_elses_repository():
    """`target_github_owner` shipped as "example-org" and `target_github_repo` as
    "boss-sandbox", so every install claimed to be pointed at one developer's private sandbox
    and the setup screen reported it failing — about a repository the operator has never heard
    of and cannot see."""
    from boss_engine.config import Settings

    assert Settings.model_fields["target_github_owner"].default == ""
    assert Settings.model_fields["target_github_repo"].default == ""
