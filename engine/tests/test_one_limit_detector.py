"""ONE LIMIT DETECTOR (model_router.headroom). Setup's model check kept its own narrower copy
("rate limit" / "usage limit" only), so Claude Code's "You've hit your weekly limit · resets
Nov 27, 7pm (UTC)." read as a bare "Check failed" with no reset (laptop, 2026-11-26) while the
router read the same sentence as a closed weekly window.

The table below feeds every real Claude and Codex limit sentence through Setup's classifier and
through the router's parser and asserts they agree: the enforcement that the two cannot drift
apart again. The other keyword checks (a failed decision call, a settled proof, a build's
failure class, the shepherd) are held to the same corpus.
"""
from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from boss_engine.services import setup_checks
from boss_engine.services.model_router.headroom import (
    MONTHLY, limit_verdict, windows_from_limit_message)

from tests.test_model_router_limits import CORPUS

#: The receipt's exact words (laptop install, 2026-11-26).
RECEIPT = "Claude Code refused: You've hit your weekly limit · resets Nov 27, 7pm (UTC)."
RECEIPT_RESET = datetime(2026, 11, 27, 19, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 11, 26, 9, 0, tzinfo=timezone.utc)

SENTENCES = [message for message, _window, _resets in CORPUS] + [
    RECEIPT,
    "You've hit your weekly limit",
    "Opus weekly limit reached",
    "5-hour limit reached ∙ resets 3am",
    "Claude AI usage limit reached|1764190800",
    "You've hit your usage limit. Upgrade to Plus to continue using Codex, or try again at "
    "Nov 28th, 2026 2:14 AM.",
    "■ You've hit your usage limit. Try again in 3 days 4 hours.",
    "Your credit balance is too low to access the Anthropic API.",
]

NOT_LIMITS = ["API Error: 529 Overloaded", "syntax error near line 3", "claude CLI not found",
              "Traceback: KeyError 'user_id'", "the agent exited non-zero"]


def _expected(windows: dict) -> str:
    return "no_credit" if MONTHLY in windows else "rate_limited"


@pytest.mark.parametrize("message", SENTENCES)
@pytest.mark.parametrize("subscription", [True, False])
def test_setup_and_the_router_read_every_limit_sentence_the_same(message, subscription):
    windows = windows_from_limit_message(message, NOW)
    assert windows, f"the router's parser does not know: {message!r}"
    expected = _expected(windows)
    verdict = limit_verdict(message, NOW)
    assert verdict is not None and verdict.state == expected
    for status in (-1, 200, 400, 429):
        got = setup_checks.classify_model_failure(status, message, subscription=subscription)
        assert got == expected, (status, message, got)


@pytest.mark.parametrize("message", NOT_LIMITS)
def test_a_failure_that_is_not_a_limit_is_not_one_anywhere(message):
    assert windows_from_limit_message(message, NOW) == {}
    assert limit_verdict(message, NOW) is None
    assert setup_checks.classify_model_failure(-1, message) not in ("rate_limited", "no_credit")


def test_the_receipt_is_limited_until_its_own_reset():
    assert setup_checks.classify_model_failure(-1, RECEIPT, subscription=True) == "rate_limited"
    verdict = limit_verdict(RECEIPT, NOW)
    assert verdict.retry_at == RECEIPT_RESET
    assert setup_checks.parse_time(setup_checks._retry_after({}, RECEIPT)).replace(
        year=2026) == RECEIPT_RESET


def test_a_limit_with_no_reset_named_has_no_invented_retry_at():
    """The router plans with the window's span; a person is never shown that guess."""
    verdict = limit_verdict("You've hit your weekly limit", NOW)
    assert verdict.state == "rate_limited" and verdict.retry_at is None
    assert setup_checks._retry_after({}, "You've hit your weekly limit") is None


def test_a_header_retry_after_still_wins():
    retry = setup_checks._retry_after({"retry-after": "30"}, RECEIPT)
    assert retry is not None and setup_checks.parse_time(retry) < RECEIPT_RESET


@pytest.mark.parametrize("message", SENTENCES)
def test_every_other_keyword_check_knows_every_limit_sentence(message):
    from boss_engine.services import task_shepherd
    from boss_engine.services.code_task_service import classify_pipeline_exception
    from boss_engine.services.connection_spend import _not_served
    from boss_engine.services.typed_decision_spend import capacity_after_failure

    assert capacity_after_failure(RuntimeError(message))["capacity_state"] != "available"
    assert _not_served(message)
    assert classify_pipeline_exception(message.lower()) == ("needs_human", "model_rate_limit"), \
        message
    if any(w["source"] == "limit" for w in windows_from_limit_message(message).values()):
        assert task_shepherd.classify(message) == task_shepherd.SPEND_CAP, message


# ── the Setup check itself: state, retry_at and the provider's words ──────────────────────────

def _held_proof(said: str) -> dict:
    """What governed_proof hands back when the CLI's reply could not be settled: BOSS's own
    error in `error`, the CLI's words only in the outcome and summary (the operator's case)."""
    return {"ok": False, "state": "failed", "receipt_id": "rcpt_1",
            "error": "Connection proof needs reconciliation; its financial hold remains.",
            "reconciliation_required": True,
            "outcome": {"kind": "unknown", "provider_error": said,
                        "summary": f"No reply from Claude Code ({said})."},
            "summary": f"No reply from Claude Code ({said})."}


@pytest.mark.parametrize("proof", [
    _held_proof("You've hit your weekly limit · resets Nov 27, 7pm (UTC)"),
    {"ok": False, "receipt_id": "rcpt_2", "error": "HTTP 429", "summary": RECEIPT},
    {"ok": False, "error": "You've hit your weekly limit · resets Nov 27, 7pm (UTC)"},
])
def test_a_subscription_check_on_a_plan_limit_is_limited_with_its_reset(monkeypatch, proof):
    from boss_engine.services import claude_credential, setup_actions, setup_service

    monkeypatch.setattr(setup_service, "governed_proof", lambda *a, **k: dict(proof))
    monkeypatch.setattr(claude_credential, "long_lived_token_info", lambda *a, **k: None)
    got = setup_actions._subscription_proof(SimpleNamespace(engine=object()), None, "proj",
                                            "claude_code", "system", prove_with=lambda: {})
    assert got["state"] == "rate_limited", got
    assert setup_checks.parse_time(got["retry_at"]).replace(year=2026) == RECEIPT_RESET
    reason = setup_actions.check_reason(got)
    assert "weekly limit" in reason and "resets Nov 27" in reason
    assert "reconciliation" not in reason


def test_the_reason_is_short_redacted_and_names_the_limit_line():
    from boss_engine.services.setup_actions import REASON_CHARS, check_reason

    key = "sk-ant-api03-" + "A" * 40
    said = check_reason({"ok": False, "provider_error":
                         f"Welcome banner\nYou've hit your weekly limit · resets 7pm (UTC) {key}"
                         + " pad" * 80})
    assert said.startswith("You've hit your weekly limit")
    assert key not in said and "AAAAAAAAAA" not in said
    assert len(said) <= REASON_CHARS
    assert check_reason({"ok": True, "state": "working", "provider_error": "x"}) is None


def test_a_limited_row_says_what_the_provider_said_and_when_it_resets():
    from boss_engine.services import setup_view

    entry = setup_view._model_entry(
        source_id="anthropic_claude_code", provider="claude_code", kind="subscription",
        model=None, state="rate_limited", checked_at=NOW.isoformat(),
        retry_at=RECEIPT_RESET.isoformat(), source="connected", now=NOW, host="", latency_ms=1,
        reason=RECEIPT)
    pill = entry["pill"]
    assert pill["tone"] == "amber" and pill["label"].startswith("Limited")
    assert pill["at"] and setup_checks.parse_time(pill["at"]) == RECEIPT_RESET
    assert pill["tip"].startswith(RECEIPT), pill["tip"]
    assert entry["reason"] == RECEIPT
    working = setup_view._model_entry(
        source_id="anthropic_claude_code", provider="claude_code", kind="subscription",
        model=None, state="working", checked_at=NOW.isoformat(), retry_at=None,
        source="connected", now=NOW, host="", latency_ms=1, reason="stale words")
    assert "stale words" not in working["pill"]["tip"] and working["reason"] is None


# ── recorded on the source: last_check keeps retry_at and the reason; the next check waits ───

def _database_url() -> str:
    return os.environ.get("DATABASE_URL") or ""


@pytest.fixture(scope="module")
def pg_engine():
    from sqlalchemy import create_engine

    from boss_engine.db import ensure_schema

    url = _database_url()
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if not url or not any(m in name for m in ("test", "testing", "ci", "scratch", "tmp", "boss_run")):
        pytest.skip("DATABASE_URL must name a disposable database")
    engine = create_engine(url, future=True)
    ensure_schema(engine)
    return engine


@pytest.fixture()
def connection(pg_engine):
    from sqlalchemy import text

    with pg_engine.connect() as conn:
        transaction = conn.begin()
        org, ws, proj = (f"org_{uuid.uuid4().hex[:8]}", f"ws_{uuid.uuid4().hex[:8]}",
                         f"proj_{uuid.uuid4().hex[:8]}")
        conn.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'LP')"), {"o": org})
        conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'LP')"),
                     {"w": ws, "o": org})
        conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                          "VALUES (:p, :w, 'LP', :p)"), {"p": proj, "w": ws})
        conn.project_id = proj  # type: ignore[attr-defined]
        try:
            yield conn
        finally:
            transaction.rollback()


def test_the_setup_check_records_retry_at_and_the_reason(connection):
    from sqlalchemy import text

    from boss_engine.services import proof_schedule, setup_actions

    source_id = f"src_{uuid.uuid4().hex[:8]}"
    connection.execute(text(
        "INSERT INTO entitlement_sources (id, project_id, provider, source_id, source_kind,"
        " entitlement_kind, owner_type, owner_id, auth_method, connection_status)"
        " VALUES (:id, :p, 'anthropic_claude_code', :sid, 'subscription', 'quota', 'project',"
        " :p, 'oauth_subscription', 'connected')"),
        {"id": f"ent_{uuid.uuid4().hex[:8]}", "p": connection.project_id, "sid": source_id})
    proof = _held_proof("You've hit your weekly limit · resets Nov 27, 7pm (UTC)")
    proof.update(state="rate_limited", retry_at=RECEIPT_RESET.isoformat(),
                 checked_at=NOW.isoformat())
    setup_actions._record_model_check(connection, connection.project_id, source_id, proof,
                                      "system")
    metadata, capacity = connection.execute(text(
        "SELECT connection_metadata, observed_capacity FROM entitlement_sources "
        "WHERE project_id = :p AND source_id = :s"),
        {"p": connection.project_id, "s": source_id}).one()
    last = metadata["last_check"]
    assert last["state"] == "rate_limited"
    assert setup_checks.parse_time(last["retry_at"]) == RECEIPT_RESET
    assert last["reason"] == "You've hit your weekly limit · resets Nov 27, 7pm (UTC)"
    assert capacity["exhausted"] is True and "weekly limit" in capacity["detail"]
    # The next check is just after the named reset, not on the 1 h backoff.
    schedule = proof_schedule.stored(metadata)
    assert setup_checks.parse_time(schedule["next_at"]) == \
        RECEIPT_RESET + proof_schedule.RESET_GRACE
    assert "resets Nov 27 19:00" in schedule["why"]
