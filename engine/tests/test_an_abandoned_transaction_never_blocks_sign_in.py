"""BOSSAPP-1456 (P0): one failed background step left a transaction open and every sign-in hung.

2026-11-24 on the laptop: a capability-runner tick's connection sat `idle in transaction` for
minutes holding row locks on identity_delegation_grants. A second session waited on them while
holding the org's audit-chain lock (audit.record's `pg_advisory_xact_lock`); the first sign-in
waited on that lock while holding its rate-limit row (`INSERT INTO identity_rate_limits … ON
CONFLICT`), and every later sign-in queued on the row. POST /v1/auth/mfa/totp never answered
until the backend was killed by hand; the tick's next statement then raised
PendingRollbackError, logged as "skipped run (non-fatal)" and "rolled back run (its step only)".

What this pins, against the real test Postgres:

  1. a tick whose transaction is LOST mid-step (its session ended under it) rolls that run back
     whole and gives the connection back: nothing is left idle in transaction, the claim is
     released, its siblings still run, the failure is said in words a person can act on, and a
     sign-in right after it does not wait;
  2. background work never holds the org's audit-chain lock while it works or waits: a tick that
     wrote a security event and then sits (asks a model, waits on a row) does not hold up a
     sign-in — and without `db.background()` the same shape does (the hazard, measured);
  3. a deferred event keeps the chain whole and follows its savepoint (rolled back = gone);
  4. every background transaction, on any connection, waits a bounded time for a lock and is
     ended when it idles past the limit; a tighter limit already on the session is kept;
  5. the shared helper refuses to carry on in a lost transaction, and no handler in the runner
     (or the other background savepoint handlers) swallows a savepoint rollback again.
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, PendingRollbackError

from boss_engine import db as boss_db
from boss_engine.identity import audit, ratelimit
from boss_engine.identity.audit import Outcome, SecurityEvent, Stream
from boss_engine.services import failure_words as fw
from boss_engine.services import workflow_capability_runner as runner

from .test_workflow_capability_runner import (  # noqa: F401 - fixtures used by name
    TEST_DB,
    Ctx,
    RecordingRegistry,
    _publish,
    _start,
    _steps,
    eng,
    project,
)

ROOT = Path(__file__).resolve().parents[2]
#: A sign-in that waits longer than this on a lock fails here instead of hanging the suite.
SIGN_IN_WAIT = "3s"


def _audit_ready(engine) -> bool:
    with engine.connect() as c:
        return bool(c.execute(text("SELECT to_regclass('identity_security_events') IS NOT NULL "
                                   "AND to_regclass('identity_rate_limits') IS NOT NULL")).scalar())


@pytest.fixture()
def org(eng):  # noqa: F811
    if not _audit_ready(eng):
        pytest.skip("boss_test has no identity tables")
    return f"org_1456_{uuid.uuid4().hex[:10]}"


def _sign_in(engine, org_id: str) -> float:
    """What POST /v1/auth/mfa/totp writes, in one request transaction: the rate-limit row, then
    its security event under the org's chain lock. Fails (LockNotAvailable) instead of hanging."""
    started = time.monotonic()
    with engine.begin() as c:
        c.execute(text(f"SET LOCAL lock_timeout = '{SIGN_IN_WAIT}'"))
        ratelimit.hit("mfa.user", f"usr_{uuid.uuid4().hex[:8]}", limit=10, window_seconds=3600, db=c)
        audit.record(SecurityEvent(org_id=org_id, event_type="auth.mfa.succeeded",
                                   outcome=Outcome.SUCCESS, details={"method": "totp"}), db=c)
    return time.monotonic() - started


def _background_event(org_id: str) -> SecurityEvent:
    return SecurityEvent(org_id=org_id, event_type="delegation.used_first", outcome=Outcome.SUCCESS,
                         stream=Stream.LINEAGE, target_type="delegation_grant", target_id="dlg_1456")


def _chain(engine, org_id: str) -> list[dict]:
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT seq, event_type, prev_hash, hash FROM identity_security_events "
            "WHERE org_id = :o ORDER BY seq"), {"o": org_id}).mappings()]


def _idle_in_transaction(engine, app: str) -> int:
    with engine.connect() as c:
        return int(c.execute(text(
            "SELECT count(*) FROM pg_stat_activity WHERE application_name = :a "
            "AND state LIKE 'idle in transaction%'"), {"a": app}).scalar())


# ── 1. a lost transaction inside a tick ──────────────────────────────────────────────────────

class EndsItsOwnSession:
    """The `cost_check` step's tool. For the run named "lost" it ends the tick's own database
    session from the side while the tick sits idle in its transaction — what Postgres's idle
    limit, or an operator's pg_terminate_backend, did on 2026-11-24."""

    def __init__(self, admin, app: str):
        self.name, self.kind, self.description, self.parameters = "get_costs", "write", "x", {}
        self.admin, self.app = admin, app
        self.sent: list[str] = []
        self.ended = 0

    def handler(self, ctx, *, project_id, actor, args):
        name = str(args.get("since") or "")
        self.sent.append(name)
        if name == "lost" and not self.ended:
            with self.admin.begin() as c:
                self.ended = len(c.execute(text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE application_name = :a AND state LIKE 'idle in transaction%'"),
                    {"a": self.app}).all())
        return {"spend_usd": 0}


def _three_runs(engine, project_id):
    runs = {}
    with engine.begin() as c:
        for name in ("first", "lost", "third"):
            _, version = _publish(c, project_id, f"l1456_{name}", [
                {"id": "n", "kind": "capability",
                 "binding": {"capability": "cost_check", "inputs": {"since": name}}, "next": []},
            ], "n")
            runs[name] = _start(c, project_id, version["id"], {})["id"]
            c.execute(text("UPDATE workflow_runs SET updated_at = now() - make_interval(secs => :s) "
                           "WHERE id = :r"), {"s": {"first": 30, "lost": 20, "third": 10}[name],
                                              "r": runs[name]})
    return runs


@pytest.fixture()
def tick_engine():
    app = f"tick1456_{uuid.uuid4().hex[:8]}"
    engine = create_engine(TEST_DB, future=True, connect_args={"options": f"-c application_name={app}"})
    yield engine, app
    engine.dispose()


def test_a_lost_transaction_rolls_its_run_back_whole_and_strands_nothing(
        eng, project, org, monkeypatch, tick_engine):  # noqa: F811
    engine, app = tick_engine
    tool = EndsItsOwnSession(eng, app)
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([tool]))
    runs = _three_runs(eng, project)

    result = runner.tick_per_step(engine, Ctx(eng), project_id=project)

    assert tool.ended == 1, "the test must really have ended the tick's session mid-step"
    by_run = {p["run_id"]: p for p in result["performed"]}
    lost = by_run[runs["lost"]]
    assert lost["outcome"] == "rolled_back", result
    assert boss_db.TransactionLost.WHAT in lost["error"], lost
    # Its claim went with it: back to `ready`, to run again on the next tick.
    with eng.connect() as c:
        status = {name: _steps(c, run_id)["n"]["status"] for name, run_id in runs.items()}
    assert status == {"first": "succeeded", "lost": "ready", "third": "succeeded"}, status
    # Nothing the tick opened is left idle inside a transaction, holding locks.
    assert _idle_in_transaction(eng, app) == 0
    # And a sign-in right after it does not wait on anything the tick left behind.
    assert _sign_in(eng, org) < 2.0


def test_the_lost_transaction_is_said_in_words_a_person_can_act_on():
    assert fw.problems(what=boss_db.TransactionLost.WHAT, action=boss_db.TransactionLost.ACTION) == []
    lost = boss_db.TransactionLost(PendingRollbackError("x", None, None))
    assert str(lost).startswith(boss_db.TransactionLost.WHAT)
    assert "`PendingRollbackError`" in str(lost)


# ── 2. the audit-chain lock: background work never holds it while it works ────────────────────

def _hold_an_event_open(engine, org_id: str, *, in_background: bool):
    """A tick's transaction that wrote a security event and then sits (a model call, a row wait)."""
    wrote, release, errors = threading.Event(), threading.Event(), []

    def tick():
        try:
            ctx = boss_db.background() if in_background else _nothing()
            with ctx:
                with engine.begin() as c:
                    audit.record(_background_event(org_id), db=c)
                    wrote.set()
                    release.wait(15)
        except Exception as error:  # noqa: BLE001 — surfaced by the test
            errors.append(error)
            wrote.set()

    thread = threading.Thread(target=tick, daemon=True)
    thread.start()
    assert wrote.wait(10)
    return release, thread, errors


class _nothing:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_tick_that_wrote_a_security_event_never_holds_up_a_sign_in(eng, org):  # noqa: F811
    release, thread, errors = _hold_an_event_open(eng, org, in_background=True)
    try:
        took = _sign_in(eng, org)
    finally:
        release.set()
        thread.join(15)
    assert not errors, errors
    assert took < 2.0, f"a sign-in waited {took:.1f}s on a tick"
    chain = _chain(eng, org)
    # The sign-in went first; the tick's event was chained at the tick's commit, onto it.
    assert [r["event_type"] for r in chain] == ["auth.mfa.succeeded", "delegation.used_first"]
    assert [r["seq"] for r in chain] == [1, 2]
    assert bytes(chain[1]["prev_hash"]) == bytes(chain[0]["hash"])


def test_without_background_the_same_tick_holds_every_sign_in(eng, org):  # noqa: F811
    """The hazard, measured: a transaction that wrote an event holds the org's chain lock until
    it ends, so a sign-in waits for it (here: fails at its lock_timeout instead of hanging)."""
    release, thread, errors = _hold_an_event_open(eng, org, in_background=False)
    try:
        with pytest.raises(OperationalError) as waited:
            _sign_in(eng, org)
    finally:
        release.set()
        thread.join(15)
    assert not errors, errors
    assert "lock timeout" in str(waited.value).lower()


# ── 3. a deferred event follows its transaction and its savepoint ─────────────────────────────

def test_a_deferred_event_follows_its_savepoint_and_its_transaction(eng, org):  # noqa: F811
    with boss_db.background():
        with eng.begin() as c:
            kept = audit.record(_background_event(org), db=c)
            assert kept.deferred
            point = c.begin_nested()
            audit.record(SecurityEvent(org_id=org, event_type="delegation.revoked",
                                       outcome=Outcome.SUCCESS, stream=Stream.LINEAGE), db=c)
            point.rollback()                                   # gone with its savepoint
            with c.begin_nested():
                audit.record(SecurityEvent(org_id=org, event_type="delegation.narrowed",
                                           outcome=Outcome.SUCCESS, stream=Stream.LINEAGE), db=c)
        with pytest.raises(RuntimeError):
            with eng.begin() as c:
                audit.record(SecurityEvent(org_id=org, event_type="delegation.created",
                                           outcome=Outcome.SUCCESS, stream=Stream.LINEAGE), db=c)
                raise RuntimeError("the work failed")          # gone with its transaction
    chain = _chain(eng, org)
    assert [r["event_type"] for r in chain] == ["delegation.used_first", "delegation.narrowed"]
    assert bytes(chain[1]["prev_hash"]) == bytes(chain[0]["hash"])


def test_a_refused_event_is_refused_at_once_not_at_commit(eng, org):  # noqa: F811
    with boss_db.background():
        with eng.begin() as c:
            with pytest.raises(audit.AuditWriteRefused):
                audit.record(SecurityEvent(org_id=org, event_type="not.a.type",
                                           outcome=Outcome.SUCCESS), db=c)


# ── 4. the safety net on every background transaction ────────────────────────────────────────

def _limits(c) -> tuple[str, str]:
    return (str(c.execute(text("SHOW lock_timeout")).scalar()),
            str(c.execute(text("SHOW idle_in_transaction_session_timeout")).scalar()))


def test_every_background_transaction_has_bounded_waits(eng):  # noqa: F811
    with eng.begin() as c:
        baseline = _limits(c)                                  # a request's: the server's own
        assert baseline[0] == "0"
    with boss_db.background():
        with eng.begin() as c:
            assert _limits(c) == ("30s", "5min")
        with eng.connect() as c:                               # autobegun, any connection
            assert _limits(c) == ("30s", "5min")
    with eng.begin() as c:
        assert _limits(c) == baseline                          # transaction-local: gone after
    tight = create_engine(TEST_DB, future=True, connect_args={
        "options": "-c lock_timeout=1000 -c idle_in_transaction_session_timeout=1500"})
    try:
        with boss_db.background():
            with tight.begin() as c:
                assert _limits(c) == ("1s", "1500ms")          # a tighter limit is kept
    finally:
        tight.dispose()


def test_a_background_wait_on_a_lock_ends_instead_of_hanging(eng, monkeypatch):  # noqa: F811
    monkeypatch.setattr(boss_db, "BACKGROUND_LOCK_TIMEOUT_SECONDS", 1)
    key = int(uuid.uuid4().int % (2 ** 62))
    with eng.connect() as holder:
        holder.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
        started = time.monotonic()
        with pytest.raises(OperationalError) as waited:
            with boss_db.background():
                with eng.begin() as c:
                    c.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
        holder.rollback()
    assert time.monotonic() - started < 5
    assert "lock timeout" in str(waited.value).lower()


# ── 5. the shared helper, and no handler swallows a savepoint rollback again ─────────────────

def test_the_helper_refuses_to_carry_on_in_a_lost_transaction():
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        point = c.begin_nested()
        boss_db.release_failed_savepoint(point, ValueError("a bad row"), connection=c)
        assert not point.is_active and c.in_transaction()     # rolled back; the work goes on
        point = c.begin_nested()
        with pytest.raises(boss_db.TransactionLost):
            boss_db.release_failed_savepoint(point, PendingRollbackError("gone", None, None),
                                             connection=c)


SWEPT = ("services/workflow_capability_runner.py", "services/agent_acts.py", "services/core.py",
         "services/autonomy_gate.py", "services/included_spend.py", "services/skill_runtime.py",
         "services/workflow_precheck_agent.py")


def swallowed_rollbacks(source: str) -> list[int]:
    """Lines of `except` handlers that call `<name>.rollback()` by hand and do not end in `raise`:
    a handler that rolls back and carries on, whatever state the transaction is in."""
    import ast

    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ExceptHandler):
            continue
        calls = [c for stmt in node.body for c in ast.walk(stmt)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                 and c.func.attr == "rollback" and isinstance(c.func.value, ast.Name)]
        if calls and not isinstance(node.body[-1], ast.Raise):
            found.append(node.lineno)
    return found


@pytest.mark.parametrize("module", SWEPT)
def test_no_background_handler_rolls_a_savepoint_back_and_carries_on(module):
    source = (ROOT / "engine" / "boss_engine" / module).read_text(encoding="utf-8")
    found = swallowed_rollbacks(source)
    assert not found, (f"{module} lines {found}: use db.release_failed_savepoint, which raises "
                       "TransactionLost when the transaction cannot carry on")


def test_the_guard_finds_what_it_guards_against():
    before = """
try:
    work()
except Exception:  # noqa: BLE001 - a comment on the handler
    try:
        point.rollback()
    except Exception:  # noqa: BLE001
        pass
    log.warning("carried on")
try:
    work()
except Exception:  # noqa: BLE001
    point.rollback()
    return None
try:
    work()
except BaseException:
    point.rollback()
    raise
"""
    assert swallowed_rollbacks("def f():\n" + "\n".join("    " + l for l in before.splitlines())) \
        == [5, 13]


def test_the_tick_runs_as_background_work():
    import inspect

    assert "_db.background()" in inspect.getsource(runner.tick_per_step)
    main = (ROOT / "apps/api/app/main.py").read_text(encoding="utf-8")
    start = main.index("def run_delivery_capabilities(")
    body = main[start:main.index("\n@app.", start)]
    assert "with background():" in body


# ── review round (P1s): every path that could still strand, loop or hold ──────────────────────

class EndsItsOwnSessionEveryTime(EndsItsOwnSession):
    def handler(self, ctx, *, project_id, actor, args):
        self.ended = 0
        return super().handler(ctx, project_id=project_id, actor=actor, args=args)


def test_a_step_that_loses_its_transaction_every_time_stops_and_says_why(
        eng, project, monkeypatch, tick_engine):  # noqa: F811
    """A cause that repeats (a step that always idles past the limit) must not re-run the step,
    its messages and its spend, on every tick for ever with nobody told."""
    engine, app = tick_engine
    tool = EndsItsOwnSessionEveryTime(eng, app)
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([tool]))
    with eng.begin() as c:
        _, version = _publish(c, project, "l1456_always", [
            {"id": "n", "kind": "capability",
             "binding": {"capability": "cost_check", "inputs": {"since": "lost"}}, "next": []},
        ], "n")
        run_id = _start(c, project, version["id"], {})["id"]
    outcomes = []
    for _ in range(runner.LOST_TRANSACTION_LIMIT):
        with eng.begin() as c:
            c.execute(text("UPDATE workflow_step_runs SET run_after = now() - interval '1 second' "
                           "WHERE workflow_run_id = :r AND status = 'ready'"), {"r": run_id})
        result = runner.tick_per_step(engine, Ctx(eng), project_id=project)
        outcomes += [p["outcome"] for p in result["performed"] if p["run_id"] == run_id]
    assert outcomes == ["rolled_back"] * (runner.LOST_TRANSACTION_LIMIT - 1) + ["failed"], outcomes
    with eng.connect() as c:
        step = _steps(c, run_id)["n"]
    what, action = runner.lost_transaction_words(runner.LOST_TRANSACTION_LIMIT)
    assert step["status"] in ("failed", "deadlettered"), step      # terminal: not retried
    assert what in str(step["last_error"]), step
    assert fw.problems(what=what, action=action) == []
    assert tool.sent.count("lost") == runner.LOST_TRANSACTION_LIMIT
    assert _idle_in_transaction(eng, app) == 0


def test_a_savepoint_whose_release_failed_is_rolled_back_not_carried_on(eng):  # noqa: F811
    """`commit()` inside the try: RELEASE fails on an aborted transaction, SQLAlchemy marks the
    savepoint inactive, and a plain rollback() sends nothing — the transaction stayed aborted."""
    ran = []
    with eng.begin() as c:
        point = c.begin_nested()
        boss_db.at_commit(c, lambda conn: ran.append("inside the failed savepoint"))
        try:
            try:
                c.execute(text("SELECT 1/0"))
            except Exception:  # noqa: BLE001 — swallowed inside, as the callers' lookups did
                pass
            point.commit()
        except Exception as error:  # noqa: BLE001
            boss_db.release_failed_savepoint(point, error, connection=c)
        assert c.execute(text("SELECT 1")).scalar() == 1      # the transaction carries on
        boss_db.at_commit(c, lambda conn: ran.append("after it"))
    assert ran == ["after it"]


def _fails(conn):
    raise RuntimeError("the append failed")


def test_a_failed_commit_callable_ends_the_database_transaction(eng):  # noqa: F811
    """Commit-as-you-go: a failing at_commit callable must not leave the old transaction's writes
    open on the connection for its next commit to keep."""
    with eng.connect() as c:
        c.execute(text("CREATE TEMP TABLE t1456 (v int)"))
        c.commit()
        c.execute(text("INSERT INTO t1456 VALUES (1)"))
        boss_db.at_commit(c, _fails)
        with pytest.raises(RuntimeError):
            c.commit()
        c.rollback()
        c.execute(text("INSERT INTO t1456 VALUES (2)"))
        c.commit()
        assert sorted(c.execute(text("SELECT v FROM t1456")).scalars()) == [2]


def test_a_token_never_waits_on_its_grant_row_while_holding_the_chain_lock():
    """The incident's request path: `token.issued` took the chain lock, then mark_first_use
    waited FOR UPDATE on a grant row a tick held. The row is touched first now, never waited on."""
    import inspect

    from boss_engine.identity import grants, tokens

    mint = inspect.getsource(tokens.mint_run_token)
    assert mint.index("grants.mark_first_use(db, grant)") < mint.index('_issue(db, claims, "token.issued")')
    assert "FOR UPDATE SKIP LOCKED" in inspect.getsource(grants.mark_first_use)


def test_a_request_is_told_to_send_again_a_tick_that_it_retries():
    assert fw.problems(what=boss_db.TransactionLost.WHAT,
                       action=boss_db.TransactionLost.REQUEST_ACTION) == []
    assert boss_db.TransactionLost.REQUEST_ACTION in str(boss_db.TransactionLost(ValueError()))
    with boss_db.background():
        assert boss_db.TransactionLost.ACTION in str(boss_db.TransactionLost(ValueError()))
