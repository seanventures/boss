"""A key the provider refused for money says "Out of credit" on every surface (2026-10-25).

THE LIVE BUG. Runs on the Anthropic key failed "Credit balance is too low" while Repo and models
said `setup-anthropic · Working · Available`: the last Test had passed a day earlier, the run's
refusal was folded in as a plain capacity window (or booked on the subscription instead of the
key it ran on), and the pill kept its green.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import claude_credential, provider_limits, setup_pills, setup_view

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
REFUSAL = ("Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
           "'message': 'Your credit balance is too low to access the Anthropic API.'}}")


class _Settings:
    github_token = ""
    target_github_owner = ""
    target_github_repo = ""
    openai_api_key = ""
    openai_base_url = ""
    cli_harnesses_json = ""


@pytest.fixture()
def connection(entitlement_tables, setup_tables):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as conn:
        setup_tables(conn)
        entitlement_tables(conn)
    with engine.begin() as conn:
        yield conn


def _key(conn, *, capacity):
    conn.execute(text(
        "INSERT INTO entitlement_sources (project_id, source_id, provider, auth_method,"
        " connection_status, status, credential_secret_ref_id, connection_metadata,"
        " observed_capacity, updated_at, created_at, cost_mode)"
        " VALUES ('p', 'setup-anthropic', 'anthropic', 'api_key', 'connected', 'active',"
        " 'ref-1', :m, :c, :at, :at, 'metered')"),
        {"m": json.dumps({"last_check": {"ok": True, "state": "working",
                                         "at": "2026-10-24T10:00:00+00:00"}}),
         "c": json.dumps(capacity), "at": NOW})


def test_a_credit_refusal_after_a_good_test_is_out_of_credit():
    state, _at, retry = setup_view.source_state(
        kind="metered", now=NOW, budget_spent=False,
        metadata={"last_check": {"ok": True, "state": "working"}},
        capacity=provider_limits.classify_limit(REFUSAL).as_capacity())
    assert (state, retry) == ("no_credit", None)


def test_a_plain_usage_window_is_still_limited_not_out_of_credit():
    state, _at, _retry = setup_view.source_state(
        kind="subscription", now=NOW, budget_spent=False,
        metadata={"last_check": {"ok": True, "state": "working"}},
        capacity={"exhausted": True, "detail": "You've hit your weekly limit",
                  "retry_at": "2026-10-27T00:00:00+00:00"})
    assert state == "rate_limited"


def test_the_pill_says_out_of_credit_and_the_fix():
    shown = setup_pills.pill("model", "no_credit")
    assert (shown["label"], shown["tone"]) == ("Out of credit", "red")
    assert "top up at the provider" in shown["tip"].lower()


def test_the_models_row_and_the_sources_pill_agree(connection):
    _key(connection, capacity={**provider_limits.classify_limit(REFUSAL).as_capacity(),
                               "capacity_state": "available"})
    models = {m["source_id"]: m
              for m in setup_view.build_basics(connection, _Settings(), "p")["models"]}
    assert models["setup-anthropic"]["pill"]["label"] == "Out of credit"
    assert setup_view.source_pills(connection, _Settings(), "p")["setup-anthropic"]["label"] \
        == "Out of credit"


def test_a_success_after_topping_up_clears_it():
    state, _at, _retry = setup_view.source_state(
        kind="metered", now=NOW, budget_spent=False,
        metadata={"last_check": {"ok": True, "state": "working"}},
        capacity={"exhausted": False, "detail": REFUSAL})
    assert state == "working"


@pytest.mark.parametrize("engine,active,expected,absent", [
    ("claude_code", "setup-anthropic", "setup-anthropic", "anthropic_claude_code"),
    ("claude_code", "anthropic_claude_code", "anthropic_claude_code", None),
    ("claude_agent_sdk", "setup-anthropic", "setup-anthropic", "anthropic_api"),
    ("claude_agent_sdk", "anthropic_claude_code", "anthropic_api", None),
])
def test_a_claude_call_is_booked_on_the_credential_it_ran_on(monkeypatch, engine, active,
                                                             expected, absent):
    monkeypatch.setattr(claude_credential, "active_source_id", lambda *_a, **_k: active)
    ids = provider_limits._paying_sources(object(), _Settings(), "p", engine)
    assert expected in ids
    if absent:
        assert absent not in ids
