""""I want multiple models and choose only those active models to be assigned in the role."

The role pickers were built from the harness registry, which answers a different question:
what BOSS can DRIVE. On the install this was reported from, that was eleven options —

    aider, azure, claude_code, codex, cursor, deepseek, groq, native, ollama, openrouter, vllm

— of which exactly one, codex, had a credential. Ten of those choices save fine and then fail
inside a build with a missing key, which looks like a bad model rather than an empty dropdown.

So there are two lists, deliberately: what may be ACCEPTED (can BOSS drive it at all — the
right bound for validation, and unchanged) and what is OFFERED (has this project connected it).
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services.project_settings import KNOBS, _allowed_choices, offered_choices
from boss_engine.services.setup_service import connected_engines


class _Settings:
    """No credentials in the environment, so these tests measure the database alone."""

    openai_api_key = ""
    anthropic_api_key = ""
    claude_code_oauth_token = ""
    azure_openai_key = ""
    openrouter_api_key = ""
    groq_api_key = ""
    deepseek_api_key = ""
    vllm_base_url = ""
    ollama_base_url = ""
    cli_harnesses_json = ""


@pytest.fixture()
def connection(entitlement_tables):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as conn:
                entitlement_tables(conn)
    with engine.begin() as conn:
        yield conn


def _add(conn, source_id, *, connected=False, ref=None, provider="x", when=None):
    conn.execute(text(
        "INSERT INTO entitlement_sources (project_id, source_id, provider,"
        " connection_status, credential_secret_ref_id, updated_at)"
        " VALUES ('p', :s, :prov, :cs, :ref, :at)"),
        {"s": source_id, "prov": provider,
         "cs": "connected" if connected else "not_connected", "ref": ref,
         "at": when or datetime(2026, 9, 18, tzinfo=timezone.utc)})


ROLE = KNOBS["code_review_engine"]


def test_a_connected_subscription_is_offered(connection):
    _add(connection, "openai_codex_chatgpt", connected=True)

    assert sorted(connected_engines(connection, _Settings(), "p")) == ["codex"]
    assert offered_choices(connection, _Settings(), "p", ROLE) == ["codex"]


def test_claude_code_is_offered_once_it_is_connected(connection):
    """The whole point of the exercise: "I choose claude code for few roles"."""
    _add(connection, "anthropic_claude_code", connected=True)
    _add(connection, "openai_codex_chatgpt", connected=True)

    assert offered_choices(connection, _Settings(), "p", ROLE) == ["claude_code", "codex"]


def test_a_stored_api_key_is_offered_as_the_lane_that_spends_it(connection):
    """An OpenAI key is the engine-owned native agent; an OpenRouter key is its own lane."""
    _add(connection, "setup-openai", ref="sref_1")
    _add(connection, "setup-openrouter", ref="sref_2")

    assert offered_choices(connection, _Settings(), "p", ROLE) == ["native", "openrouter"]


def test_the_ten_nobody_connected_are_not_offered(connection):
    """Every project is seeded with a catalogue row per provider. Those are not connections."""
    for seeded in ("cursor_cli", "google_gemini_cli", "xai_grok_build", "anthropic_api"):
        _add(connection, seeded)
    _add(connection, "openai_codex_chatgpt", connected=True)

    offered = offered_choices(connection, _Settings(), "p", ROLE)

    assert offered == ["codex"]
    assert len(_allowed_choices(ROLE)) > len(offered)


def test_the_environment_counts_as_connected(connection):
    """An install configured the old way IS configured, and dropping it here would take a
    working engine off the menu of somebody whose BOSS has been building for weeks."""
    class _WithEnv(_Settings):
        claude_code_oauth_token = "sk-ant-oat01-x"
        openai_api_key = "sk-x"

    assert sorted(connected_engines(connection, _WithEnv(), "p")) == ["claude_code", "native"]


def test_what_is_accepted_is_still_the_wider_list(connection):
    """Narrowing the PICKER must not narrow what the API will store: an operator connecting a
    model through some other route, or a script, is not doing anything wrong."""
    from boss_engine.services.project_settings import coerce

    _add(connection, "openai_codex_chatgpt", connected=True)

    assert coerce("code_review_engine", "claude_code") == "claude_code"
    assert "claude_code" in _allowed_choices(ROLE)
    assert "claude_code" not in offered_choices(connection, _Settings(), "p", ROLE)


def test_a_chosen_model_is_never_dropped_from_its_own_picker(connection):
    """A model can be disconnected after being chosen. A picker that silently omits the value
    it is displaying rewrites the setting the next time the operator touches the form."""
    _add(connection, "openai_codex_chatgpt", connected=True)

    offered = offered_choices(connection, _Settings(), "p", ROLE, current="claude_code")

    assert offered == ["claude_code", "codex"]


def test_nothing_connected_shows_everything_rather_than_nothing(connection):
    """An empty dropdown reads as "this install cannot do it". The truth is "connect a model
    first", and eleven greyed-out possibilities say that better than a blank."""
    assert offered_choices(connection, _Settings(), "p", ROLE) == _allowed_choices(ROLE)


def test_a_shipped_default_is_not_a_connection(connection):
    """MEASURED, not imagined: ollama_base_url defaults to the local daemon's address, so
    "non-empty" reported Ollama as a connected model on an install with no Ollama running —
    and it sorted ahead of codex in every role picker."""
    from boss_engine.config import Settings, get_settings

    shipped = Settings.model_fields["ollama_base_url"].default
    assert shipped, "this test is meaningless if the field has no default"

    live = get_settings()
    assert "ollama" not in connected_engines(connection, live, "p") or \
        str(live.ollama_base_url).strip() != str(shipped).strip()


def test_but_a_changed_default_is(connection):
    """Pointing BOSS at your own endpoint IS connecting it — there is no credential to store."""
    class _PointedAtOne(_Settings):
        ollama_base_url = "http://gpu-box.internal:11434/v1"

    assert "ollama" in connected_engines(connection, _PointedAtOne(), "p")


def test_a_number_knob_is_untouched(connection):
    assert offered_choices(connection, _Settings(), "p", KNOBS["code_agent_max_concurrency"]) == []
