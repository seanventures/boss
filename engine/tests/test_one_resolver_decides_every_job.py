"""One resolver decides which model does each job, and every reader asks it.

`choose` (chat) preferred the deployment's environment and `build_engine_for` (builds) preferred
the project's choice, so "which model writes my code?" had two answers, and a stale
OPENAI_API_KEY outranked a subscription connected in the console. `model_routing.resolve_job`
is now the one order: the project's choice, a working subscription with capacity, a working
local model, a working metered key, the install's environment key, nothing.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import model_routing, setup_service


class _Settings:
    openai_api_key = ""
    anthropic_api_key = ""
    claude_code_oauth_token = ""
    azure_openai_key = ""
    openrouter_api_key = ""
    groq_api_key = ""
    deepseek_api_key = ""
    google_api_key = ""
    xai_api_key = ""
    vllm_base_url = ""
    ollama_base_url = ""
    cli_harnesses_json = ""
    chat_llm_engine = ""
    llm_engine = "auto"
    code_agent_engine = ""          # nobody set BOSS_CODE_AGENT_ENGINE
    decision_backend = ""


class _StaleKey(_Settings):
    openai_api_key = "sk-stale"


@pytest.fixture()
def conn(entitlement_tables):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as c:
        entitlement_tables(c)
    with engine.begin() as c:
        yield c


def _add(conn, source_id, *, ref=None, connected=True, capacity=None, metadata=None,
         cost_mode=None):
    conn.execute(text(
        "INSERT INTO entitlement_sources (project_id, source_id, provider, connection_status,"
        " credential_secret_ref_id, updated_at, observed_capacity, connection_metadata,"
        " cost_mode) VALUES ('p', :s, 'x', :cs, :ref, :at, :cap, :meta, :cm)"),
        {"s": source_id, "cs": "connected" if connected else "not_connected", "ref": ref,
         "at": datetime(2026, 9, 18, tzinfo=timezone.utc), "cap": json.dumps(capacity or {}),
         "meta": json.dumps(metadata or {}),
         "cm": cost_mode or ("subscription" if connected and not ref else "usage_based_api")})


def _choose(conn, key, value):
    conn.execute(text("INSERT INTO project_settings (project_id, key, value) VALUES ('p', :k, :v)"),
                 {"k": key, "v": value})


def _job(conn, job, settings=None):
    return model_routing.resolve_job(conn, settings or _Settings(), "p", job)


@pytest.mark.parametrize("job", ["build", "chat", "review", "hard", "routine"])
def test_a_console_subscription_beats_a_stale_environment_key(conn, job):
    _add(conn, "anthropic_claude_code")
    picked = _job(conn, job, _StaleKey())
    assert picked.engine == "claude_code" and picked.kind == "subscription"
    assert not picked.metered


def test_the_environment_key_still_answers_when_nothing_is_connected(conn):
    picked = _job(conn, "chat", _StaleKey())
    assert (picked.engine, picked.why) == ("native", model_routing.ENVIRONMENT)
    assert picked.metered


def test_the_projects_choice_comes_first(conn):
    _add(conn, "anthropic_claude_code")
    _add(conn, "setup-openrouter", ref="sr1", connected=False)
    _choose(conn, "code_review_engine", "openrouter")
    _choose(conn, "code_review_model", "openai/gpt-5")
    picked = _job(conn, "review")
    assert (picked.engine, picked.model, picked.why) == (
        "openrouter", "openai/gpt-5", model_routing.CHOSEN)
    assert picked.metered


def test_jobs_left_on_auto_are_done_by_whatever_does_build(conn):
    """The runtime's own rule: an empty reviewer lane means the implementer reviews."""
    _add(conn, "setup-openrouter", ref="sr1", connected=False)
    _choose(conn, "code_agent_engine", "openrouter")
    for job in ("review", "hard", "routine"):
        assert _job(conn, job).engine == "openrouter"


def test_a_limited_subscription_falls_over_to_a_metered_key_until_it_resets(conn):
    later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    _add(conn, "anthropic_claude_code",
         capacity={"exhausted": True, "retry_at": later})
    _add(conn, "setup-openrouter", ref="sr1", connected=False)
    picked = _job(conn, "build")
    assert picked.engine == "openrouter" and picked.metered
    conn.execute(text("UPDATE entitlement_sources SET observed_capacity = :c "
                      "WHERE source_id = 'anthropic_claude_code'"),
                 {"c": json.dumps({"exhausted": False})})
    assert _job(conn, "build").engine == "claude_code"


def test_a_local_model_comes_before_a_metered_key(conn):
    _add(conn, "setup-openrouter", ref="sr1", connected=False)
    _add(conn, "setup-ollama", connected=True, cost_mode="local_capacity")
    assert _job(conn, "chat").engine == "ollama"


def test_a_key_whose_last_check_failed_is_not_auto_picked(conn):
    _add(conn, "setup-openrouter", ref="sr1", connected=False,
         metadata={"last_check": {"ok": False, "state": "key_rejected"}})
    _add(conn, "setup-gemini", ref="sr2", connected=False)
    assert _job(conn, "chat").engine == "gemini"


def test_build_engine_for_and_choose_are_the_resolver(conn):
    _add(conn, "openai_codex_chatgpt")
    _add(conn, "setup-openrouter", ref="sr1", connected=False)
    assert setup_service.build_engine_for(conn, _Settings(), "p") == _job(conn, "build").engine
    assert model_routing.choose(conn, _Settings(), "p").engine == _job(conn, "chat").engine


def test_decisions_are_claude_unless_jev_is_held(conn):
    assert _job(conn, "decisions").engine == ""          # nothing can decide yet
    _add(conn, "anthropic_claude_code")
    picked = _job(conn, "decisions")
    assert (picked.engine, picked.kind) == ("claude_code", "subscription")
