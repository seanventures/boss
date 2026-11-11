"""BOSSAPP-1008 (audit BOSSAPP-997, finding 5): connection proofs follow one schedule.

A proof is a whole engine session, so WHEN it runs is policy (proof_schedule.py), not an hourly
beat: 391 proof sessions in one month, 314 dying at the engine's 420 s review timeout, came from
re-proving every non-green subscription every sweep. Covered here:

- a green proof is cached: nothing re-proves it before its TTL;
- a failing connection backs off 1 h → 2 h → 4 h … capped, and a success resets it;
- a proof times out in tens of seconds, and that timeout is a failure, never 420 s;
- the console's explicit Re-check proves now even while the schedule is waiting;
- the settled llm_calls row names its own stage (connection_proof), not model_gateway;
- a proof stops heartbeating its leases at the proof wall, so its job cannot sit
  "running" for hours.

Real, disposable postgres (DATABASE_URL names this run's throwaway database); every provider
answer is scripted — no real CLI, no real network, no real model call.
"""
from __future__ import annotations

import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from boss_engine.db import ensure_schema
from boss_engine.services import proof_schedule, setup_actions, setup_view, subscription_keepalive

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


def _database_url() -> str:
    return os.environ.get("DATABASE_URL") or ""


@pytest.fixture(scope="module")
def pg_engine():
    url = _database_url()
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if not url or not any(m in name for m in ("test", "testing", "ci", "scratch", "tmp", "boss_run")):
        pytest.skip("DATABASE_URL must name a disposable database")
    engine = create_engine(url, future=True)
    ensure_schema(engine)
    return engine


class _Same:
    """An engine whose transactions are savepoints of the test's one rolled-back transaction."""

    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def begin(self):
        with self.connection.begin_nested():
            yield self.connection


@pytest.fixture()
def connection(pg_engine):
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        org, ws, proj = f"org_{uuid.uuid4().hex[:8]}", f"ws_{uuid.uuid4().hex[:8]}", f"proj_{uuid.uuid4().hex[:8]}"
        conn.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'KA')"), {"o": org})
        conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'KA')"), {"w": ws, "o": org})
        conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, 'KA', :p)"),
                     {"p": proj, "w": ws})
        conn.project_id = proj  # type: ignore[attr-defined]
        try:
            yield conn
        finally:
            transaction.rollback()


def _source(conn, source_id: str) -> None:
    conn.execute(text(
        "INSERT INTO entitlement_sources (id, project_id, provider, source_id, source_kind,"
        " entitlement_kind, owner_type, owner_id, auth_method, connection_status)"
        " VALUES (:id, :p, 'openai_codex_chatgpt', :sid, 'subscription', 'quota', 'project',"
        " :p, 'oauth_subscription', 'connected')"),
        {"id": f"ent_{uuid.uuid4().hex[:8]}", "p": conn.project_id, "sid": source_id})


def _schedule(conn, source_id: str) -> dict:
    return proof_schedule.stored(conn.execute(text(
        "SELECT connection_metadata FROM entitlement_sources WHERE project_id = :p AND source_id = :s"),
        {"p": conn.project_id, "s": source_id}).scalar())


def _row(source_id, *, tone="green", next_in_hours=None, checked_hours_ago=1.0, stale=False):
    entry = {"source_id": source_id, "kind": "subscription", "source": "connected",
             "pill": {"tone": tone}, "stale": stale,
             "checked_at": (NOW - timedelta(hours=checked_hours_ago)).isoformat()}
    if next_in_hours is not None:
        entry["next_check_at"] = (NOW + timedelta(hours=next_in_hours)).isoformat()
    return entry


def _asked(monkeypatch, rows):
    monkeypatch.setattr(setup_view, "build", lambda conn, settings, project_id: {"models": rows})
    asked = []
    monkeypatch.setattr(setup_actions, "check_model",
                        lambda conn, settings, project_id, source_id, **k: asked.append(source_id))
    return asked


# ── green is cached; the schedule, not the badge, decides ───────────────────────────────────

def test_a_green_proof_is_cached_until_its_ttl(connection, monkeypatch):
    """A row whose schedule names a later attempt is not re-proved — even a RED one: the
    old rule (every non-green row, every sweep, forever) is exactly what burnt 314 sessions."""
    asked = _asked(monkeypatch, [_row("green-waiting", next_in_hours=3),
                                 _row("red-backing-off", tone="red", next_in_hours=2),
                                 _row("green-due", next_in_hours=-0.1)])
    assert subscription_keepalive._reprove(_Same(connection), None, connection.project_id,
                                           now=NOW) == 1
    assert asked == ["green-due"]


def test_a_row_without_a_schedule_keeps_the_old_rule_once(connection, monkeypatch):
    """Rows written before this policy: non-green or past the green TTL is due; the check it
    triggers writes the schedule, and the backoff takes over from there."""
    asked = _asked(monkeypatch, [_row("fresh-green", checked_hours_ago=1),
                                 _row("old-green", checked_hours_ago=7),
                                 _row("red-legacy", tone="red", checked_hours_ago=0.5)])
    subscription_keepalive._reprove(_Same(connection), None, connection.project_id, now=NOW)
    assert asked == ["old-green", "red-legacy"]


# ── failure backs off exponentially; success resets ─────────────────────────────────────────

def test_a_failing_connection_backs_off_and_a_success_resets(connection):
    source_id = f"src_{uuid.uuid4().hex[:8]}"
    _source(connection, source_id)
    at = NOW
    expected_hours = [1, 2, 4, 8, 16, 24, 24]  # doubling, then the cap holds
    for n, hours in enumerate(expected_hours, start=1):
        setup_actions._record_model_check(
            connection, connection.project_id, source_id,
            {"ok": False, "state": "failed", "checked_at": at.isoformat()}, "system")
        schedule = _schedule(connection, source_id)
        assert schedule["failures"] == n
        next_at = setup_view.parse_time(schedule["next_at"])
        assert next_at == at + timedelta(hours=hours), f"failure {n} must wait {hours} h"
        assert "Re-check" in schedule["why"]
        at = next_at
    setup_actions._record_model_check(
        connection, connection.project_id, source_id,
        {"ok": True, "state": "working", "checked_at": at.isoformat()}, "system")
    schedule = _schedule(connection, source_id)
    assert schedule["failures"] == 0
    assert setup_view.parse_time(schedule["next_at"]) == at + proof_schedule.green_ttl(None)


def test_the_providers_own_retry_at_wins_over_the_backoff(connection):
    """A plan reset ("Try again at …") names the moment itself; waiting the cap instead would
    show the old failure long after the plan came back."""
    source_id = f"src_{uuid.uuid4().hex[:8]}"
    _source(connection, source_id)
    reset_at = NOW + timedelta(hours=5)
    setup_actions._record_model_check(
        connection, connection.project_id, source_id,
        {"ok": False, "state": "rate_limited", "checked_at": NOW.isoformat(),
         "retry_at": reset_at.isoformat()}, "system")
    schedule = _schedule(connection, source_id)
    assert setup_view.parse_time(schedule["next_at"]) == reset_at


def test_bosss_own_not_now_leaves_the_schedule_alone(connection):
    """job_deferred / paused budget is BOSS's queue or money, not the provider's answer: it
    neither resets a green TTL nor advances the backoff."""
    source_id = f"src_{uuid.uuid4().hex[:8]}"
    _source(connection, source_id)
    setup_actions._record_model_check(
        connection, connection.project_id, source_id,
        {"ok": True, "state": "working", "checked_at": NOW.isoformat()}, "system")
    before = _schedule(connection, source_id)
    setup_actions._record_model_check(
        connection, connection.project_id, source_id,
        {"ok": False, "state": "job_deferred", "checked_at": (NOW + timedelta(hours=1)).isoformat()},
        "system")
    assert _schedule(connection, source_id) == before


def test_a_live_rejection_resets_the_schedule_once(connection):
    """Live traffic finding the login rejected clears the schedule (the next sweep re-proves
    now) — but only on the transition, so a login that keeps failing keeps its backoff."""
    from boss_engine.services import model_failures

    source_id = f"src_{uuid.uuid4().hex[:8]}"
    _source(connection, source_id)
    setup_actions._record_model_check(
        connection, connection.project_id, source_id,
        {"ok": True, "state": "working", "checked_at": NOW.isoformat()}, "system")
    assert _schedule(connection, source_id)
    model_failures.mark_login(connection, connection.project_id, source_id, rejected=True)
    assert _schedule(connection, source_id) == {}, "a real failure signal re-proves now"
    setup_actions._record_model_check(
        connection, connection.project_id, source_id,
        {"ok": False, "state": "login_expired", "checked_at": NOW.isoformat()}, "system")
    backoff = _schedule(connection, source_id)
    assert backoff["failures"] == 1
    model_failures.mark_login(connection, connection.project_id, source_id, rejected=True)
    assert _schedule(connection, source_id) == backoff, "still-rejected keeps its backoff"
    model_failures.clear_login_state(connection, connection.project_id, source_id)
    assert _schedule(connection, source_id) == {}, "a fresh credential starts fresh"


# ── the explicit Re-check proves now; the daily sweep waits ─────────────────────────────────

def test_re_check_proves_now_while_the_daily_sweep_waits(connection, monkeypatch):
    waiting = _row("backing-off", tone="red", next_in_hours=20, stale=True)
    view = {"models": [waiting, _row("plain-stale", tone="red", stale=True)],
            "repository": {"repos": []}}
    assert setup_view.stale_targets(view, now=NOW) == ["model:plain-stale"]

    monkeypatch.setattr(setup_view, "build", lambda conn, settings, project_id: view)
    checked = []
    monkeypatch.setattr(setup_actions, "check_model",
                        lambda conn, settings, project_id, source_id, **k: checked.append(source_id))
    setup_actions.run_checks(connection, None, connection.project_id, "model:backing-off")
    assert checked == ["backing-off"], "the console's Re-check never consults the schedule"


# ── the proof session is proof-sized, and a timeout is a failure ────────────────────────────

def test_a_proof_times_out_in_tens_of_seconds_and_counts_as_a_failure(monkeypatch):
    from boss_engine.services import cli_harness, setup_service

    spec = cli_harness.CliHarnessSpec(name="codex", binary="codex")
    assert spec.review_timeout == 420, "the build spec keeps its own budgets"
    monkeypatch.setattr(cli_harness, "load_specs", lambda settings: {"codex": spec})
    seen = {}

    class FakeAgent:
        def __init__(self, settings, used_spec):
            seen["spec"] = used_spec

        def run_role(self, prompt, cwd, *, read_only=False, **_):
            raise cli_harness.CliHarnessUnavailable(
                f"codex did not finish within {seen['spec'].review_timeout}s")

    monkeypatch.setattr(cli_harness, "GenericCliCodeAgent", FakeAgent)
    result = setup_service.prove_coding_agent(None, "codex")
    assert seen["spec"].timeout == setup_service.PROOF_TIMEOUT_SECONDS
    assert seen["spec"].review_timeout == setup_service.PROOF_TIMEOUT_SECONDS
    assert setup_service.PROOF_TIMEOUT_SECONDS < 100, "a proof times out in tens of seconds"
    assert result["ok"] is False, "a timeout is a failure the schedule backs off on"
    assert "did not finish" in result["error"]


# ── governed proofs on real authority: the stage and the wall ───────────────────────────────

def _candidate(p):
    return {"provider": "openai", "source_kind": "api", "entitlement_kind": "api_key",
            "owner_type": "project", "owner_id": p, "cost_mode": "usage_based_api",
            "auth_method": "api_key", "connection_status": "connected", "status": "active",
            "runtime_supported": True, "observed_capacity": {"credential_present": False},
            "supported_models": ["gpt-4o-mini"], "workload_classes_allowed": ["owner_sandbox"],
            "risk_tier_max": 1, "limit_shape": {"max_concurrent": 1}}


@pytest.fixture()
def scope(pg_engine):
    from boss_engine.config import Settings

    project = "proofsched_" + uuid.uuid4().hex[:12]
    with pg_engine.begin() as c:
        c.execute(text("INSERT INTO orgs(id,name) VALUES(:p,:p)"), {"p": project})
        c.execute(text("INSERT INTO workspaces(id,org_id,name) VALUES(:p,:p,:p)"), {"p": project})
        c.execute(text("INSERT INTO projects(id,workspace_id,name,slug) VALUES(:p,:p,:p,:p)"),
                  {"p": project})
        c.execute(text(
            "INSERT INTO budget_policies(id,scope_type,scope_id,daily_cap_usd_micros,"
            "monthly_cap_usd_micros,burst_cap_usd_micros_per_min) "
            "VALUES(:p,'project',:p,1000000,1000000,0)"), {"p": project})
    return pg_engine, project, Settings()


def test_a_settled_proof_row_names_its_stage(scope):
    """The settlement's llm_calls row carries the invocation's declared stage — proofs and
    login checks stop being ledgered as anonymous model_gateway work."""
    from boss_engine.services.setup_service import governed_proof

    e, p, s = scope
    result = governed_proof(
        e, s, p, "setup-openai", "usr_owner", intent="stage-proof",
        candidate=_candidate(p), credential="owned-nonsecret-marker", backend="database",
        capacity_group="owned-connection",
        bound_prove=lambda binding: {"ok": True, "state": "working", "cost_usd_micros": 1000})
    assert result["ok"]
    with e.connect() as c:
        stages = [r[0] for r in c.execute(
            text("SELECT stage FROM llm_calls WHERE project_id=:p"), {"p": p})]
    assert stages == ["connection_proof"]


def test_a_proof_stops_heartbeating_at_its_wall(scope, monkeypatch):
    """Past the proof wall the leases stop being renewed: a hung session can no longer keep
    its connection.proof job "running" for hours (two sat 15 h on boss-1, 11 Nov)."""
    from boss_engine.services import connection_spend
    from boss_engine.services.connection_spend import ConnectionSpend

    e, p, s = scope
    monkeypatch.setattr(connection_spend, "PROOF_WALL_SECONDS", 1)
    spend = ConnectionSpend.for_runner(
        s, engine=e, project_id=p, run_id="owned-setup", step_id="owned", attempt=1,
        actor="usr_owner", worker_id="owned-proof-worker")
    spend.ttl_seconds = 5  # heartbeat every ~1.7 s; without the wall, beats at ~1.7 s, ~3.3 s…
    beats = []
    real = spend._ensure_lease

    def counting(authority):
        beats.append(time.monotonic())
        return real(authority)

    monkeypatch.setattr(spend, "_ensure_lease", counting)
    started = time.monotonic()

    def contact(_):
        time.sleep(4)
        return {"ok": True, "cost_usd_micros": 1000}

    result = spend.prove(s, provider="openai", intent="owned-wall", candidate=_candidate(p),
                         credential="owned-marker", backend="database", call=contact)
    assert result["ok"] is False and result["reconciliation_required"] is True, \
        "an answer from past the wall settles as reconcile, never as a live success"
    late = [b for b in beats[1:] if b - started > 2.5]  # beats[0] is the pre-call heartbeat
    assert not late, "heartbeats must stop at the wall, not while the call still runs"
