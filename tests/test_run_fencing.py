"""Fencing tests, written from the production incident rather than from the happy path.

The incident (job_2fbe88e6): worker-local's lease expired, worker-local-3 reclaimed the job,
and worker-local NEVER STOPPED. Both executed the same run. Two `write_tests` finished 29
seconds apart, six agent processes ran on one task, the run's phase moved backwards, and 89
minutes produced no implementation.

Each test below reproduces a step of that, against a REAL Postgres row rather than a mock —
the whole defect lived in what the database did or did not reject, so mocking it would test
nothing. Run directly (pytest collection wipes the dev database):

    python tests/test_run_fencing.py
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))

from sqlalchemy import create_engine, text  # noqa: E402

from boss_engine.config import Settings  # noqa: E402
from boss_engine.services.run_fencing import (  # noqa: E402
    RunFenced, assert_owner, claim_run, guarded_status, guarded_update, hash_token,
    is_owner, transition_allowed,
)

_R: list[tuple[bool, str, str]] = []
PROJECT = "boss-dogfood"


def check(name: str, got, want, note: str = "") -> None:
    _R.append((got == want, name, f"got={got!r} want={want!r} {note}".strip()))


def check_true(name: str, got, note: str = "") -> None:
    check(name, bool(got), True, note)


def _mk_run(conn, run_id: str) -> None:
    conn.execute(text(
        "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, state, "
        "usage, finalize_ctx) VALUES (:i, :p, 'running', 'queued', 'fencing test', "
        "'{}'::jsonb, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb) "
        "ON CONFLICT (id) DO UPDATE SET status='running', phase='queued', "
        "owner_token=NULL, state_version=0"), {"i": run_id, "p": PROJECT})


def _cleanup(conn, run_id: str) -> None:
    conn.execute(text("DELETE FROM code_task_runs WHERE id = :i"), {"i": run_id})


# --------------------------------------------------------------------------- #
def t_hashing(eng) -> None:
    check("token is hashed, never stored raw", hash_token("secret") == "secret", False)
    check("hashing is stable", hash_token("abc"), hash_token("abc"))
    check("different tokens differ", hash_token("a") == hash_token("b"), False)
    check("no token -> None (unfenced paths keep working)", hash_token(None), None)
    check("empty token -> None", hash_token(""), None)


# --------------------------------------------------------------------------- #
def t_the_incident(eng) -> None:
    """THE case: a reclaiming worker fences the previous executor, which then cannot write."""
    RID = "crun_fencetest_incident"
    w1, w2 = hash_token("lease-worker-local"), hash_token("lease-worker-local-3")
    with eng.begin() as c:
        _mk_run(c, RID)

        # worker-local starts and owns the run.
        check_true("executor 1 claims the run", claim_run(c, RID, w1))
        guarded_update(c, RID, w1, "phase = :p", {"p": "write_tests"})
        check("executor 1 can write while it owns", c.execute(
            text("SELECT phase FROM code_task_runs WHERE id=:i"), {"i": RID}).scalar(),
            "write_tests")

        # Its lease expires; worker-local-3 reclaims the job and starts executing.
        check_true("executor 2 reclaims (most recent claimer wins)", claim_run(c, RID, w2))

        # THE FIX: executor 1 is now fenced. Before this, its write silently succeeded and
        # the two executions overwrote each other's phase.
        try:
            guarded_update(c, RID, w1, "phase = :p", {"p": "implement"})
            check("fenced executor's write is REJECTED", "written", "RunFenced")
        except RunFenced as exc:
            check_true("fenced executor's write is REJECTED", True)
            check_true("the rejection names the run", RID in str(exc))

        check("phase was NOT clobbered by the stale executor", c.execute(
            text("SELECT phase FROM code_task_runs WHERE id=:i"), {"i": RID}).scalar(),
            "write_tests")

        # Executor 2 works normally.
        guarded_update(c, RID, w2, "phase = :p", {"p": "implement"})
        check("the owner can still write", c.execute(
            text("SELECT phase FROM code_task_runs WHERE id=:i"), {"i": RID}).scalar(),
            "implement")

        # Boundary checks agree with the write layer.
        check("is_owner: stale executor", is_owner(c, RID, w1), False)
        check("is_owner: current owner", is_owner(c, RID, w2), True)
        try:
            assert_owner(c, RID, w1, "stage boundary")
            check("assert_owner raises for a stale executor", "passed", "RunFenced")
        except RunFenced:
            check_true("assert_owner raises for a stale executor", True)
        _cleanup(c, RID)


# --------------------------------------------------------------------------- #
def t_terminal_is_forever(eng) -> None:
    """A cancelled run reported `blocked` and then `completed` today. Terminal must absorb."""
    check("running -> completed", transition_allowed("running", "completed"), True)
    check("running -> cancelled", transition_allowed("running", "cancelled"), True)
    check("cancelled -> blocked is ILLEGAL", transition_allowed("cancelled", "blocked"), False)
    check("cancelled -> completed is ILLEGAL",
          transition_allowed("cancelled", "completed"), False)
    check("completed -> failed is ILLEGAL", transition_allowed("completed", "failed"), False)
    check("failed -> running is ILLEGAL", transition_allowed("failed", "running"), False)
    check("re-writing the same terminal state is harmless",
          transition_allowed("cancelled", "cancelled"), True)
    check("unknown current status does not block progress",
          transition_allowed(None, "running"), True)

    RID = "crun_fencetest_terminal"
    tok = hash_token("lease-x")
    with eng.begin() as c:
        _mk_run(c, RID)
        claim_run(c, RID, tok)
        check_true("cancel succeeds", guarded_status(c, RID, tok, "cancelled"))
        check("a later 'completed' is refused, not applied",
              guarded_status(c, RID, tok, "completed"), False)
        check("status stayed cancelled", c.execute(
            text("SELECT status FROM code_task_runs WHERE id=:i"), {"i": RID}).scalar(),
            "cancelled")
        _cleanup(c, RID)


# --------------------------------------------------------------------------- #
def t_unfenced_paths_still_work(eng) -> None:
    """Fencing must never break single-worker/dev runs, or rows created before the column."""
    RID = "crun_fencetest_unfenced"
    with eng.begin() as c:
        _mk_run(c, RID)
        guarded_update(c, RID, None, "phase = :p", {"p": "implement"})
        check("no token -> write still applies", c.execute(
            text("SELECT phase FROM code_task_runs WHERE id=:i"), {"i": RID}).scalar(),
            "implement")
        check("no token -> is_owner is permissive", is_owner(c, RID, None), True)

        # A legacy row with no owner recorded must accept a newly-fenced executor.
        tok = hash_token("lease-late")
        check("legacy row (owner NULL) accepts an executor", is_owner(c, RID, tok), True)
        claim_run(c, RID, tok)
        guarded_update(c, RID, tok, "phase = :p", {"p": "check_tests"})
        check("after claiming, the owner writes", c.execute(
            text("SELECT phase FROM code_task_runs WHERE id=:i"), {"i": RID}).scalar(),
            "check_tests")
        _cleanup(c, RID)


# --------------------------------------------------------------------------- #
def t_version_makes_contention_visible(eng) -> None:
    RID = "crun_fencetest_version"
    tok = hash_token("lease-v")
    with eng.begin() as c:
        _mk_run(c, RID)
        claim_run(c, RID, tok)
        for i in range(3):
            guarded_update(c, RID, tok, "phase = :p", {"p": f"s{i}"})
        check("state_version counts applied writes", c.execute(
            text("SELECT state_version FROM code_task_runs WHERE id=:i"), {"i": RID}).scalar(), 3)
        other = hash_token("lease-other")
        claim_run(c, RID, other)
        try:
            guarded_update(c, RID, tok, "phase = :p", {"p": "ghost"})
        except RunFenced:
            pass
        check("a REJECTED write does not bump the version", c.execute(
            text("SELECT state_version FROM code_task_runs WHERE id=:i"), {"i": RID}).scalar(), 3)
        _cleanup(c, RID)


# --------------------------------------------------------------------------- #
def t_missing_run(eng) -> None:
    with eng.begin() as c:
        check("claiming a nonexistent run reports failure",
              claim_run(c, "crun_does_not_exist", hash_token("t")), False)
        try:
            guarded_update(c, "crun_does_not_exist", hash_token("t"), "phase = :p", {"p": "x"})
            check("writing a nonexistent run raises", "wrote", "RunFenced")
        except RunFenced:
            check_true("writing a nonexistent run raises", True)


# --------------------------------------------------------------------------- #
def main() -> int:
    s = Settings()
    eng = create_engine(s.database_url)
    for fn in (t_hashing, t_the_incident, t_terminal_is_forever,
               t_unfenced_paths_still_work, t_version_makes_contention_visible, t_missing_run):
        try:
            fn(eng)
        except Exception as exc:  # a crash in a test fn is itself a failure
            _R.append((False, f"{fn.__name__} raised", f"{type(exc).__name__}: {exc}"))
    bad = [r for r in _R if not r[0]]
    for ok, name, detail in _R:
        if not ok:
            print(f"  FAIL  {name}\n        {detail}")
    print(f"\n  {len(_R) - len(bad)}/{len(_R)} fencing assertions passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
