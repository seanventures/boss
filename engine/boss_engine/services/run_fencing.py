"""Fencing for governed run execution.

WHY THIS EXISTS
---------------
Job CLAIMING is sound: `FOR UPDATE SKIP LOCKED` means two workers never claim the same
queued job, and leases + TTL + reclaim handle a dead worker. What was missing is the other
half of a lease: **the holder must stop when it loses it.**

Nothing enforced that. A worker whose lease expired kept executing and kept writing, so a
reclaim produced two live executions of the same run. Measured on job_2fbe88e6: two
`write_tests` finished 29 seconds apart, six agent processes on one task, the run's phase
moved BACKWARDS as the two executions overwrote each other, 89 minutes and $4.44 spent
without producing a line of implementation.

Checking ownership at stage boundaries narrows the window but cannot close it — between two
boundaries a stale executor still writes freely. Correctness needs the write itself to fail.

THE MODEL
---------
Every execution carries a fencing token (the hash of its job lease token). A run row records
the token of its current owner. Then:

  * claiming a run OVERWRITES the owner token — a reclaiming worker fences the previous one
    by the act of starting, with no cooperation needed from it;
  * every state-mutating write is `... WHERE id = :id AND owner_token = :mine`, so a fenced
    executor's writes affect 0 rows;
  * 0 rows raises `RunFenced`, which the pipeline unwinds on.

This is the same pattern `github_branch_leases.py` already uses correctly for branch leases.
It was simply never applied to run execution.

`state_version` increments on every guarded write. It is not needed for correctness here
(the token is the authority) but it makes concurrent clobbering VISIBLE in the data: a run
whose version jumps in bursts had contention, and there is no other way to see that after
the fact.
"""
from __future__ import annotations

import hashlib
from typing import Any, Optional

from ..ports import sql

# Statuses from which a run may never transition again. A cancelled run that later reported
# `blocked`, then `completed`, is what made today's terminal events untrustworthy.
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

# Transitions the pipeline is allowed to make. Anything else is a bug, and silently applying
# it is how a run's phase moved backwards while two executions raced.
_ALLOWED_FROM = {
    "queued": {"running", "cancelled", "failed", "blocked"},
    "running": {"running", "repairing", "blocked", "completed", "failed", "cancelled"},
    "repairing": {"running", "repairing", "blocked", "completed", "failed", "cancelled"},
    "blocked": {"running", "repairing", "completed", "failed", "cancelled"},
}


class RunFenced(Exception):
    """This execution no longer owns the run, so its write was rejected.

    Raised instead of returning a flag on purpose: a fenced executor must unwind, not carry
    on with a write it believes succeeded.
    """

    def __init__(self, run_id: str, where: str) -> None:
        self.run_id, self.where = run_id, where
        super().__init__(
            f"run {run_id} is owned by another execution (rejected at {where}); "
            "this execution has been fenced and must stop"
        )


def hash_token(raw_token: Optional[str]) -> Optional[str]:
    """Hash a lease token. The raw token is never stored, exactly as branch leases do it."""
    if not raw_token:
        return None
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def claim_run(connection, run_id: str, token_hash: Optional[str]) -> bool:
    """Take ownership of a run for this execution. The CURRENT lease wins — provably.

    "Most recent claimer wins, unconditionally" had a hole (BOSSAPP-986): a STALE executor
    whose claim landed late — delayed transaction, slow start — took the token back from the
    worker that legitimately reclaimed the job, and from then on the stale one wrote freely
    while the real owner was fenced. Ownership must be decided by the control plane's epoch,
    not by whoever wrote last.

    The lease grant (JobLeaseService.claim_job / lease_owned_job) increments the job's
    fence_epoch and stamps it onto the run, monotonically. Claiming here consults the LIVE
    lease for this run: an executor whose token is not the current lease's, or whose epoch is
    older than the run's, raises RunFenced — it must stand down before doing any work. The
    claim write itself carries `fence_epoch <= :e` so even a racing stale claim matches 0 rows
    at the database boundary.

    Returns False only when there is nothing to claim (no such run) or no token was supplied
    (single-worker/dev paths keep working unfenced rather than breaking).
    """
    if not token_hash:
        return False
    # THIS EXECUTION'S OWN LEASE, not "the newest live job for the run" (BOSSAPP-1364). Two live
    # rows can name one run: the job a person's approval enqueued, and a stale one from before
    # the run parked (a redelivery joined while the laptop slept, its lease lapsing since).
    # Picking the newest by updated_at let the stale row decide who owns the run.
    live_jobs = connection.execute(
        sql(
            "SELECT id, lease_token_hash, fence_epoch, "
            "       (lease_expires_at IS NULL OR lease_expires_at > now()) AS current "
            "FROM jobs WHERE kind = 'code.task' AND payload->>'run_id' = :run_id "
            "  AND status IN ('leased', 'running')"
        ),
        {"run_id": run_id},
    ).mappings().all()
    if not live_jobs:
        # No live lease (a direct API run, a dev path): the old last-claimer rule stands.
        n = connection.execute(
            sql("UPDATE code_task_runs SET owner_token = :t, updated_at = now() "
                "WHERE id = :id"),
            {"t": token_hash, "id": run_id},
        ).rowcount
        return bool(n)
    mine = next((j for j in live_jobs if j["lease_token_hash"] == token_hash), None)
    if mine is None:
        # The job has been re-leased: this execution's lease is not the current one.
        raise RunFenced(run_id, "claim")
    epoch = int(mine["fence_epoch"] or 0)
    # A run whose epoch is NEWER than this lease was claimed by a successor — unless no
    # successor is alive any more: every other live job for it has an expired lease. That
    # owner is stale (its worker slept or died), and the current lease TAKES OVER instead of
    # being fenced by a ghost. A takeover is a handover like any other: the epoch moves strictly
    # forward, on this job's lease AND on the run, so the ghost's older epoch can never write.
    rival_alive = any(j["id"] != mine["id"] and j["current"] for j in live_jobs)
    run_epoch = connection.execute(
        sql("SELECT fence_epoch FROM code_task_runs WHERE id = :id FOR UPDATE"),
        {"id": run_id}).scalar()
    if run_epoch is None:
        return False
    run_epoch = int(run_epoch or 0)
    if run_epoch > epoch:
        if rival_alive:
            # The run carries a NEWER epoch held by a live lease: claimed by a successor.
            raise RunFenced(run_id, "claim")
        epoch = run_epoch + 1
        bumped = connection.execute(
            sql("UPDATE jobs SET fence_epoch = :e, updated_at = now() "
                "WHERE id = :j AND lease_token_hash = :t"),
            {"e": epoch, "j": mine["id"], "t": token_hash}).rowcount
        if not bumped:
            raise RunFenced(run_id, "claim")
    n = connection.execute(
        sql("UPDATE code_task_runs SET owner_token = :t, fence_epoch = :e, updated_at = now() "
            "WHERE id = :id AND fence_epoch <= :e"),
        {"t": token_hash, "e": epoch, "id": run_id},
    ).rowcount
    if not n:
        raise RunFenced(run_id, "claim")
    return True


#: The owner a run parked on a person holds: nobody. Not NULL — NULL means "never fenced",
#: which every check treats as anyone's (is_owner), and a straggler of the parked execution must
#: not pass for the owner of a run it released.
RELEASED = "released"


def release_run(connection, run_id: str, token_hash: Optional[str]) -> None:
    """A run that parks on a person gives up its owner (BOSSAPP-1364).

    The execution that parked it is over — its job ends done — so nothing may keep owning the
    run while it waits. Only the OWNER releases (or a run nobody owned): a fenced straggler
    cannot release a run a successor holds. Released, the run is owned by nobody: every
    guarded write and every is_owner check fails until the approval's resume claims it.
    """
    if not token_hash:
        return
    connection.execute(
        sql("UPDATE code_task_runs SET owner_token = :released WHERE id = :id "
            "AND (owner_token = :t OR owner_token IS NULL)"),
        {"id": run_id, "t": token_hash, "released": RELEASED})


def is_owner(connection, run_id: str, token_hash: Optional[str]) -> bool:
    """Cheap read-only check, for stage boundaries. An unfenced run (no token recorded, or no
    token supplied) reports True: absence of fencing must never abort legitimate work."""
    if not token_hash:
        return True
    current = connection.execute(
        sql("SELECT owner_token FROM code_task_runs WHERE id = :id"), {"id": run_id}
    ).scalar()
    return current is None or current == token_hash


def assert_owner(connection, run_id: str, token_hash: Optional[str], where: str) -> None:
    if not is_owner(connection, run_id, token_hash):
        raise RunFenced(run_id, where)


def guarded_update(
    connection,
    run_id: str,
    token_hash: Optional[str],
    set_clause: str,
    params: Optional[dict[str, Any]] = None,
    *,
    where: str = "update",
) -> None:
    """Apply a state-mutating write, but only if this execution still owns the run.

    `set_clause` is the SET body without the keyword, e.g. "phase = :p". Bind params go in
    `params`; :id and :t are supplied here.

    Raises RunFenced when the write matches no row, which means the token no longer matches —
    someone else owns this run. That is the whole point: a fenced executor finds out at the
    moment it tries to change something, not at the next stage boundary.
    """
    p = dict(params or {})
    p["id"] = run_id
    if not token_hash:
        # Unfenced (dev / single worker / legacy rows): behave exactly as before.
        connection.execute(sql(f"UPDATE code_task_runs SET {set_clause}, "
                               "state_version = state_version + 1, updated_at = now() "
                               "WHERE id = :id"), p)
        return
    p["t"] = token_hash
    n = connection.execute(
        sql(f"UPDATE code_task_runs SET {set_clause}, "
            "state_version = state_version + 1, updated_at = now() "
            "WHERE id = :id AND owner_token = :t"),
        p,
    ).rowcount
    if not n:
        raise RunFenced(run_id, where)


def transition_allowed(current: Optional[str], nxt: str) -> bool:
    """Is this status change legal? Terminal is forever.

    A cancelled run reported `blocked` and then `completed` today, because every write site
    set status directly with no check. Terminal states must absorb further writes rather than
    be overwritten by an execution that has not noticed it is finished.
    """
    if current is None:
        return True
    if current in TERMINAL_STATUSES:
        return current == nxt  # idempotent re-write of the same terminal state is harmless
    return nxt in _ALLOWED_FROM.get(current, {nxt})


def guarded_status(
    connection, run_id: str, token_hash: Optional[str], status: str,
    extra_set: str = "", params: Optional[dict[str, Any]] = None,
) -> bool:
    """Set a run's status, honouring BOTH the fence and the state machine.

    Returns False when the transition is illegal (already terminal), which callers treat as
    "someone else finished this run" — not as an error. Raises RunFenced when the run belongs
    to another execution.
    """
    current = connection.execute(
        sql("SELECT status FROM code_task_runs WHERE id = :id"), {"id": run_id}
    ).scalar()
    if not transition_allowed(current, status):
        return False
    p = dict(params or {})
    if status == "completed":
        held = template_hold(connection, run_id)
        if held is not None:
            # A BUILD A TEMPLATE GATE STOPPED IS NEVER COMPLETED, whichever path got here. The
            # tester's Stripe apply read "Done · no PR" with Template tests ✕, its application
            # stayed "building" and the workflow walked on to "Ship it". Blocked, with why and
            # what to do next: the workflow step above it fails and nothing downstream runs.
            import json as _json

            status = "blocked"
            extra_set = ("phase='done', error=:tpl_hold_error, finalize_ctx="
                         "COALESCE(finalize_ctx,'{}'::jsonb) || jsonb_build_object("
                         "'template_hold', CAST(:tpl_hold AS JSONB), "
                         "'failing_gates', CAST(:tpl_gates AS JSONB))")
            p = {"tpl_hold_error": held["error"], "tpl_hold": _json.dumps(held),
                 "tpl_gates": _json.dumps(held["gates"])}
    clause = "status = :st" + (f", {extra_set}" if extra_set else "")
    p["st"] = status
    guarded_update(connection, run_id, token_hash, clause, p, where=f"status->{status}")
    # THE TERMINAL TRANSITION records the run's profile and scorecard, in this transaction —
    # not later, from a runner `finally` that slow post-PR work can hold off for minutes
    # (services/run_terminal.py). Never raises; a no-op for a status that is not terminal.
    from .run_terminal import on_status_written

    on_status_written(connection, run_id)
    return True


def template_hold(connection, run_id: str) -> Optional[dict[str, Any]]:
    """The template gate hold for `run_id` (templates.gate.held_run), or None. An error reading
    it is None: the pipeline's own block path still holds, and this check must never be the
    thing that stops an ordinary build finishing."""
    from ..templates.gate import held_run

    try:
        with connection.begin_nested():
            return held_run(connection, run_id)
    except Exception:  # noqa: BLE001
        return None


def build_not_passed(connection, run_id: str) -> Optional[str]:
    """Why a FINISHED build must not be treated as a passed one (None when it passed).

    One rule for every consumer that turns "the build finished" into "ship it" (the workflow
    step that awaited it, deploy_build): a build a template gate stopped, or an attempt whose
    checks failed and whose work went to a repair round, did not pass, whatever its status says.
    """
    held = template_hold(connection, run_id)
    if held is not None:
        return held["error"]
    try:
        with connection.begin_nested():
            row = connection.execute(sql(
                "SELECT status, finalize_ctx->>'review_repair_next_run' AS nxt, "
                "finalize_ctx->'failing_gates' AS failing FROM code_task_runs WHERE id = :i"),
                {"i": run_id}).mappings().first()
    except Exception:  # noqa: BLE001
        return None
    if row is None:
        return None
    if str(row["status"] or "") == "repairing" or row["nxt"]:
        from .plain_words import gate_failed_words as _gate_label

        failing = row["failing"] if isinstance(row["failing"], list) else []
        return ("Blocked: this attempt failed its checks"
                + (f" ({', '.join(_gate_label(str(g)) for g in failing)})" if failing else "")
                + (f"; its repair continued in {row['nxt']}" if row["nxt"] else "")
                + ". It is not a passed build, so nothing ships from it.")
    return None
