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

#: An app's own test output or an agent's reply quoting limit-like words: not a provider
#: limit. The callers that read build, test and agent output (strict) must not take them.
APP_OUTPUT = [
    "AssertionError: expected 'Weekly limit reached' to be visible",
    "You've hit your limit of 5 free projects",
    "FAILED tests/test_plan.py::test_banner - expected 'You've hit your weekly limit' banner",
    "Opus weekly limit reached banner renders",
]


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
    # A refusal that names its own cause keeps it unless the words are a plan/credit sentence.
    for status in (401, 403, 404):
        got = setup_checks.classify_model_failure(status, message, subscription=subscription)
        if verdict.plan:
            assert got == expected, (status, message, got)
        else:
            assert got not in ("rate_limited",), (status, message, got)


@pytest.mark.parametrize(("status", "message", "state"), [
    (404, "model gpt-4-429 not found", "model_not_found"),
    (404, "The model `claude-x` does not exist (request 429)", "model_not_found"),
    (401, "invalid x-api-key (trace 429)", "key_rejected"),
    (403, "permission denied for org-429", "key_rejected"),
    (404, "You've hit your weekly limit · resets Nov 27, 7pm (UTC)", "rate_limited"),
])
def test_a_named_refusal_is_not_read_as_a_rate_limit_from_a_stray_429(status, message, state):
    assert setup_checks.classify_model_failure(status, message) == state


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
    assert retry is not None
    parsed = setup_checks.parse_time(retry)
    now = datetime.now(timezone.utc)
    assert now <= parsed <= now + timedelta(seconds=60)


def _callers():
    from boss_engine.services import task_shepherd
    from boss_engine.services.code_task_service import classify_pipeline_exception
    from boss_engine.services.connection_spend import _not_served
    from boss_engine.services.typed_decision_spend import capacity_after_failure

    return {
        "decision_capacity": lambda m: capacity_after_failure(RuntimeError(m))["capacity_state"]
        != "available",
        "build_failure_class": lambda m: classify_pipeline_exception(m.lower())
        == ("needs_human", "model_rate_limit"),
        "shepherd": lambda m: task_shepherd.classify(m) == task_shepherd.SPEND_CAP,
        "proof_settle": _not_served,
    }


#: The callers that read text beyond the provider's own error line (build/test/agent output).
STRICT_CALLERS = ("decision_capacity", "build_failure_class", "shepherd")


@pytest.mark.parametrize("message", SENTENCES)
def test_every_other_keyword_check_knows_every_limit_sentence(message):
    """Every caller takes every sentence the one detector takes; the strict callers take every
    sentence the strict detector takes (a named reset, or a spend/credit limit)."""
    callers = _callers()
    assert callers["proof_settle"](message), message
    strict = limit_verdict(message, strict=True)
    if strict is not None:
        for name in STRICT_CALLERS:
            if name == "shepherd" and not strict.plan:
                continue                 # a bare 429 is left to the shepherd's own phrases
            assert callers[name](message), (name, message)


@pytest.mark.parametrize("message", APP_OUTPUT)
def test_app_or_test_output_is_not_a_limit_for_any_strict_caller(message):
    assert limit_verdict(message, strict=True) is None
    callers = _callers()
    for name in STRICT_CALLERS:
        assert not callers[name](message), (name, message)


@pytest.mark.parametrize("message", NOT_LIMITS)
def test_a_plain_failure_is_not_a_limit_for_the_detector_driven_callers(message):
    callers = _callers()
    assert not callers["shepherd"](message), message
    if "overloaded" not in message.lower():  # an overload is a capacity wait by design
        assert not callers["decision_capacity"](message), message
        assert not callers["build_failure_class"](message), message
    assert not limit_verdict(message, strict=True)


# ── a recorded limit lapses at its reset; a green Setup check reopens it ──────────────────────

def _broker():
    from boss_engine.services.entitlements import EntitlementBrokerService

    return EntitlementBrokerService.__new__(EntitlementBrokerService)


def test_a_failed_calls_limit_lapses_once_its_reset_has_passed():
    from boss_engine.services.typed_decision_spend import capacity_after_failure

    broker = _broker()
    future = capacity_after_failure(RuntimeError(
        "You've hit your weekly limit · resets Nov 27, 7pm (UTC)"))
    assert future["capacity_state"] == "unavailable" and future["capacity_reset_at"]
    ahead = {**future, "capacity_reset_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
    assert broker._capacity_policy_denial({"observed_capacity": ahead}) == "capacity_exhausted"
    passed = {**future, "capacity_reset_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()}
    assert broker._capacity_policy_denial({"observed_capacity": passed}) is None
    minute = capacity_after_failure(RuntimeError("HTTP 429 Too Many Requests"))
    assert minute["capacity_state"] == "rate_limited" and minute["capacity_reset_at"]
    assert broker._capacity_policy_denial({"observed_capacity": {
        **minute, "capacity_reset_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}}) \
        is None
    # A person's own block never lapses on a clock.
    assert broker._capacity_policy_denial({"observed_capacity": {
        "capacity_state": "blocked", "capacity_reset_at": "2020-01-01T00:00:00+00:00"}}) == "capacity_blocked"
    # No reset recorded: the old behaviour (denied until something clears it).
    assert broker._capacity_policy_denial({"observed_capacity": {
        "capacity_state": "unavailable"}}) == "capacity_exhausted"


@pytest.mark.parametrize("message", [
    "Claude AI usage limit reached|1600000000",
    "You've hit your usage limit. Upgrade to Pro or try again at 2020-01-01T00:00:00.",
])
def test_a_reset_already_past_never_lifts_the_denial_at_once(message):
    """A: an old epoch or a past ISO time would store a 2020 reset, the denial would pass at
    once and every decision would go back to the source that just refused it."""
    from boss_engine.services.typed_decision_spend import capacity_after_failure

    got = capacity_after_failure(RuntimeError(message))
    assert got["capacity_state"] == "unavailable", got
    reset = setup_checks.parse_time(got["capacity_reset_at"])
    assert reset > datetime.now(timezone.utc) + timedelta(minutes=5)
    assert _broker()._capacity_policy_denial({"observed_capacity": got}) == "capacity_exhausted"


def test_an_old_reset_never_lifts_a_newer_denial():
    """B: writes merge, so a reset left by an earlier limit used to survive under a newer
    state. One key, written and cleared by the write that sets capacity_state."""
    from boss_engine.services.entitlements import EntitlementBrokerService as Broker

    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    old = {"capacity_state": "unavailable", "capacity_reset_at": past,
           "reset_at": past, "retry_at": past, "rate_limit_reset_at": past}
    newer = Broker.merge_capacity(old, {"capacity_state": "exhausted"})
    assert "capacity_reset_at" not in newer
    assert _broker()._capacity_policy_denial({"observed_capacity": newer}) == "capacity_exhausted"
    # A write that does not set the state keeps the pair together.
    kept = Broker.merge_capacity(old, {"credential_state": "ok"})
    assert kept["capacity_reset_at"] == past
    # A new limit with its own reset lapses after it, and not before.
    soon = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    limited = Broker.merge_capacity(newer, {"capacity_state": "rate_limited",
                                            "capacity_reset_at": soon})
    assert _broker()._capacity_policy_denial({"observed_capacity": limited}) == \
        "capacity_rate_limited"
    lapsed = {**limited, "capacity_reset_at": past}
    assert _broker()._capacity_policy_denial({"observed_capacity": lapsed}) is None


def test_out_of_credit_never_lapses_on_a_clock():
    """C: a spend or credit limit is lifted by a person, not a clock."""
    from boss_engine.services.typed_decision_spend import capacity_after_failure

    for message in ("Your credit balance is too low to access the Anthropic API.",
                    "You've hit your org's monthly spend limit"):
        got = capacity_after_failure(RuntimeError(message))
        assert got == {"capacity_state": "unavailable"}, got
        assert _broker()._capacity_policy_denial({"observed_capacity": got}) == \
            "capacity_exhausted"


def test_a_setup_check_with_a_past_reset_waits_the_probe_window(monkeypatch):
    """A, in Setup: a past retry_at becomes the check time plus the half-open probe window."""
    from boss_engine.services import core, setup_actions
    from boss_engine.services.model_router.headroom import LIMIT_PROBE_S

    seen = []
    monkeypatch.setattr(core, "emit_event", lambda *a, **k: seen.append(a[-1]))
    for retry_at in ("2020-01-01T00:00:00+00:00", None):
        setup_actions._record_model_check(
            object(), "proj", "env-claude_code",
            {"ok": False, "state": "rate_limited", "checked_at": NOW.isoformat(),
             "retry_at": retry_at}, "system")
    assert setup_checks.parse_time(seen[0]["retry_at"]) == NOW + timedelta(seconds=LIMIT_PROBE_S)
    assert seen[1]["retry_at"] is None


def test_a_named_reset_far_off_or_rolled_into_next_year_is_capped():
    from boss_engine.services import proof_schedule

    rolled = NOW.replace(year=2027, month=11, day=24)
    schedule = proof_schedule.after_check(ok=False, state="rate_limited", failures_before=0,
                                          now=NOW, retry_at=rolled.isoformat())
    assert setup_checks.parse_time(schedule["next_at"]) == NOW + proof_schedule.RESET_CAP
    soon = proof_schedule.after_check(ok=False, state="rate_limited", failures_before=0,
                                      now=NOW, retry_at=RECEIPT_RESET.isoformat())
    assert setup_checks.parse_time(soon["next_at"]) == RECEIPT_RESET + proof_schedule.RESET_GRACE


def test_provider_limits_reads_the_reset_from_the_one_parser():
    from boss_engine.services.provider_limits import classify_limit

    signal = classify_limit("You've hit your usage limit. Upgrade to Pro or try again at "
                            "Sep 22nd, 2026 10:06 PM.")
    assert signal is not None and signal.retry_at == datetime(2026, 9, 22, 22, 6,
                                                              tzinfo=timezone.utc)
    iso = classify_limit("Rate limit exceeded; try again at 2026-12-01T10:00:00")
    assert iso is not None and iso.retry_at == datetime(2026, 12, 1, 10, 0, tzinfo=timezone.utc)


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

    multi = check_reason({"ok": False, "provider_error":
                          "You've hit your weekly limit\nAuthorization: Bearer sk-ant-oat01-"
                          + "B" * 40})
    assert "BBBBBBBBBB" not in multi
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


def test_a_green_setup_check_reopens_a_limit_a_failed_call_recorded(connection):
    import json

    from sqlalchemy import text

    from boss_engine.services import setup_actions
    from boss_engine.services.typed_decision_spend import capacity_after_failure

    source_id = f"src_{uuid.uuid4().hex[:8]}"
    failed = capacity_after_failure(RuntimeError(
        "You've hit your weekly limit · resets Nov 27, 7pm (UTC)"))
    failed["capacity_reset_at"] = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    connection.execute(text(
        "INSERT INTO entitlement_sources (id, project_id, provider, source_id, source_kind,"
        " entitlement_kind, owner_type, owner_id, auth_method, connection_status,"
        " observed_capacity)"
        " VALUES (:id, :p, 'anthropic_claude_code', :sid, 'subscription', 'quota', 'project',"
        " :p, 'oauth_subscription', 'connected', CAST(:c AS JSONB))"),
        {"id": f"ent_{uuid.uuid4().hex[:8]}", "p": connection.project_id, "sid": source_id,
         "c": json.dumps(failed)})
    broker = _broker()

    def capacity():
        return connection.execute(text(
            "SELECT observed_capacity FROM entitlement_sources WHERE project_id = :p "
            "AND source_id = :s"), {"p": connection.project_id, "s": source_id}).scalar()

    assert broker._capacity_policy_denial({"observed_capacity": capacity()}) == \
        "capacity_exhausted"
    setup_actions._record_model_check(connection, connection.project_id, source_id,
                                      {"ok": True, "state": "working"}, "system")
    assert capacity()["capacity_state"] == "available"
    assert "capacity_reset_at" not in capacity()
    assert broker._capacity_policy_denial({"observed_capacity": capacity()}) is None
