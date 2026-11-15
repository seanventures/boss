"""THE RUN PLANE: how a VM admits, leases, checkpoints and governs a run without a database
(P8a, BOSSAPP-1063; boss-agent-hosts-lld.md §2.7, §2.14.2, §3.13).

A VM never touches Postgres. Its runtime (boss_engine/runtime) calls these functions over HTTP,
through boss-hostd, which authenticates as its host (services/fleet_ca.py). They wrap EXACTLY the
functions the local worker calls — thread_runs.admit, jobs.lease_owned_job,
jobs.require_active_lease, PgCheckpointStore's rows, DecisionInterrupts, the governed budget
gate, the ledger and span sinks, agent_run_worker._settle — so a run is the same run on either
transport (one code path, two transports; P8a-T3).

LEASES. A replica's lease is a job lease whose owner is `replica:<replica id>`, granted with the
job's `fence_epoch` incremented (jobs.lease_owned_job), TTL 30 s by default, renewed by the
runtime's heartbeat (10 s). `next` takes the deployment's next admitted run — or reclaims one
whose lease expired (its last holder died), at epoch + 1.

FENCING (§2.14.2). EVERY call that touches a run carries its lease: the job id, the run token
(the lease token, never stored raw: jobs keep its hash) and the `fence_epoch`. `fence` checks them
with jobs.require_active_lease, FOR UPDATE, in the SAME transaction as the write, and compares the
epoch. A stale holder — reclaimed, handed back, expired, or simply wrong — gets RunPlaneError
`409 fenced`, and nothing it sent is written. Ledger and span rows go through a connection-bound
engine (`_FencedEngine`), so they too commit with the fence or not at all.

A HOST ACTS ONLY FOR ITS OWN REPLICAS: every call names a replica, which must be placed on the
calling host, wanted running, and of a live microvm deployment.
"""
from __future__ import annotations

import contextlib
import json
import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Optional

from sqlalchemy import text

import logging

from ..runtime import wire

log = logging.getLogger(__name__)

LEASE_TTL_SECONDS = 30
LEASE_TTL_MIN, LEASE_TTL_MAX = 5, 120
KINDS = ("agent.run", "agent.resume")
_EVENT_TYPE = re.compile(r"^[a-z][a-z0-9_.]{0,60}$")


class RunPlaneError(Exception):
    """`code` is the stable error string (`fenced`, `replica_not_on_host`, ...); never a token."""

    def __init__(self, code: str, status: int = 400, message: str = "", **extra: Any):
        super().__init__(message or code)
        self.code, self.status, self.message, self.extra = code, status, message or code, extra

    def body(self) -> dict:
        return {"error": self.code, "message": self.message, **self.extra}


def fenced(message: str = "this run's lease is held by someone else now") -> RunPlaneError:
    return RunPlaneError("fenced", 409, message)


_JOBS: dict = {}


def _jobs(settings: Any) -> Any:
    """One JobLeaseService per settings object (it builds four services; the run plane's hot
    path must not rebuild them on every call)."""
    from .jobs import JobLeaseService

    key = id(settings)
    found = _JOBS.get(key)
    if found is None or found.settings is not settings:
        found = _JOBS[key] = JobLeaseService(settings)
    return found


def _ttl(value: Any) -> int:
    try:
        ttl = int(value or LEASE_TTL_SECONDS)
    except (TypeError, ValueError):
        ttl = LEASE_TTL_SECONDS
    return max(LEASE_TTL_MIN, min(LEASE_TTL_MAX, ttl))


def owner_of(replica_id: str) -> str:
    return f"replica:{replica_id}"


# ── Who is calling ──────────────────────────────────────────────────────────────────────────

def replica_for(connection: Any, host_id: str, replica_id: str) -> dict:
    """The replica the call is for: on THIS host, wanted running, of a live microvm deployment."""
    row = connection.execute(text("""
        SELECT r.id, r.host_id, r.deployment_id, r.desired_state, r.slots, r.vm_id,
               d.project_id, d.agent_key, d.environment, d.status AS deployment_status,
               d.runtime, d.version
          FROM agent_replicas r JOIN agent_deployments d ON d.id = r.deployment_id
         WHERE r.id = :r"""), {"r": replica_id}).mappings().first()
    if row is None or row["host_id"] != host_id:
        raise RunPlaneError("replica_not_on_host", 403,
                            "that replica is not placed on this host")
    if (row["desired_state"] != "running" or row["deployment_status"] != "live"
            or row["runtime"] != "microvm"):
        raise RunPlaneError("replica_not_serving", 409,
                            "that replica is not wanted running for a live microvm deployment")
    return dict(row)


@dataclass(frozen=True)
class LeaseRef:
    """What every run-scoped call carries (headers X-Boss-Job, X-Boss-Run-Token,
    X-Boss-Fence-Epoch, X-Boss-Replica)."""
    replica_id: str
    job_id: str
    token: str
    epoch: int


def fence(connection: Any, settings: Any, host_id: str, run_id: str, lease: LeaseRef, *,
          settling: bool = False) -> dict:
    """THE FENCE, in the caller's transaction (module doc). Returns the job row (locked)."""
    from .jobs import LeaseNotFoundError

    replica_for_any(connection, host_id, lease.replica_id)
    jobs = _jobs(settings)
    try:
        row = dict(jobs._load_leased_job_for_update(  # noqa: SLF001 — the same lock the worker takes
            connection, owner_of(lease.replica_id), lease.job_id, lease.token,
            allow_expired=settling))
    except LeaseNotFoundError as exc:
        raise fenced(str(exc)) from exc
    if int(row.get("fence_epoch") or 0) != int(lease.epoch):
        raise fenced(f"epoch {lease.epoch} is stale; the job is at {row.get('fence_epoch')}")
    payload = row.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    if str(payload.get("run_id") or "") != run_id:
        raise fenced("that lease is not this run's")
    return row


#: replica id -> its host. A replica is created on one host and never moves (a re-placement is a
#: NEW replica row), so the pair is immutable and safe to remember for the process's life.
_REPLICA_HOST: dict = {}


def replica_for_any(connection: Any, host_id: str, replica_id: str) -> None:
    """As replica_for, for calls under a lease the replica already holds: a replica being
    drained (wanted stopped) may still settle and hand back what it runs."""
    host = _REPLICA_HOST.get(replica_id)
    if host is None:
        host = connection.execute(text("SELECT host_id FROM agent_replicas WHERE id = :r"),
                                  {"r": replica_id}).scalar()
        if host is not None:
            if len(_REPLICA_HOST) > 100_000:
                _REPLICA_HOST.clear()
            _REPLICA_HOST[replica_id] = host
    if host is None or host != host_id:
        raise RunPlaneError("replica_not_on_host", 403, "that replica is not placed on this host")


# ── Leasing: ONE long-poll per HOST (§3.13.0 and its REVISION, operator 2026-11-14) ──────────
#
#   boss-hostd ──POST /v1/run-plane/hosts/next {wait_ms, state_gen}──► (no replica list: BOSS
#   keeps its own model of every replica) — state_gen ≠ the generation stored for the host →
#   {resync:true}, and the host PUTs its full map once (fleet.put_replicas). Otherwise, in ONE
#   transaction: this host's `serving` replicas (observed, pushed on every change through
#   /observed), each with total_slots (agent_replicas.slots, from the deployment) − busy (its
#   active leases); queued jobs WHERE target IN ('deployment:<d>' …) FOR UPDATE SKIP LOCKED, up
#   to the free slots; each run to the replica of its deployment with the most free slots
#   (round robin on ties), leased to replica:<id> at epoch + 1. Nothing claimable: wait on this
#   process's hub for the host's deployments (≤ wait_ms), then claim again. VMs never poll: the
#   host hands each run to its VM's work inbox (docs/infra/HOST_API.md §4.5).

#: At most this many runs per claim.
CLAIM_MAX = 256
WAIT_MAX_MS = 30_000
#: The one safety sweep per API process (boss-eventing-lld.md §8.2): wakes waiters whose
#: targets have claimable work a lost NOTIFY never announced.
SWEEP_SECONDS = 5.0
WAKE_KINDS = ("agent.run_admitted", "agent.run_queued")

_CANDIDATES = text("""
    SELECT id, project_id, kind, status, payload, allocation_id, resource_units, target,
           attempt, max_attempts
      FROM jobs
     WHERE target = ANY(:targets) AND kind IN ('agent.run', 'agent.resume')
       AND run_after <= now()
       AND (status IN ('queued', 'failed_retryable')
            OR (status IN ('leased', 'running') AND lease_expires_at <= now()))
     ORDER BY CASE status WHEN 'leased' THEN 0 WHEN 'running' THEN 0
                          WHEN 'failed_retryable' THEN 1 ELSE 2 END,
              priority ASC, created_at ASC
     LIMIT :n
     FOR UPDATE SKIP LOCKED""")

#: This host's serving replicas and their free slots — BOSS's own count: the slots the
#: deployment gives a replica, less the leases it holds right now (a lease that ended without
#: its final write frees its slot when it expires).
_HOST_SLOTS = text("""
    SELECT r.id, r.deployment_id, r.slots,
           (SELECT count(*) FROM jobs j WHERE j.lease_owner = 'replica:' || r.id
              AND j.status IN ('leased', 'running') AND j.lease_expires_at > now()) AS busy
      FROM agent_replicas r JOIN agent_deployments d ON d.id = r.deployment_id
     WHERE r.host_id = :h AND r.observed_state = 'serving' AND r.desired_state = 'running'
       AND d.status = 'live' AND d.runtime = 'microvm'
     ORDER BY r.id""")


def _requeue(connection: Any, settings: Any, job: dict, *, reason: str,
             give_back_attempt: bool = False) -> None:
    """Back to queued, the lease cleared and its slot released, claimable now: the next claim
    leases it at epoch + 1. A lease that expired unsettled (its holder died) keeps the attempt
    it spent; a hand-back (the VM never took it) gives it back."""
    from .core import emit_event

    connection.execute(text("""
        UPDATE jobs SET status = 'queued', lease_owner = NULL, lease_token_hash = NULL,
               lease_expires_at = NULL, heartbeat_at = NULL, run_after = now(),
               attempt = CASE WHEN :give THEN GREATEST(attempt - 1, 0) ELSE attempt END,
               updated_at = now()
         WHERE id = :j"""), {"j": job["id"], "give": give_back_attempt})
    _jobs(settings).work_orchestration.release_job_slot(
        connection, project_id=job["project_id"], job_id=job["id"],
        allocation_id=job.get("allocation_id"), resource_units=int(job.get("resource_units") or 1),
        reason=reason)
    emit_event(connection, job["project_id"], "job.lease_reclaimed", "run_plane", "job",
               job["id"], {"kind": job["kind"], "by": "host", "reason": reason})


#: A run whose VM refused it this many times is no longer given its attempt back: refusals
#: then count against max_attempts like any other loss, and the run parks (§2.14.1).
HAND_BACK_FREE = 3


def _park_exhausted(connection: Any, settings: Any, jobs: Any, job: dict, owner: str,
                    lease_token: str, run_id: str) -> None:
    """ATTEMPTS SPENT (leases lost to dead replicas, or refusals past HAND_BACK_FREE): the job
    fails without a retry and the run is recorded failed in words — never reclaimed forever."""
    from . import agent_run_worker, thread_runs

    why = (f"the run lost its executor {int(job.get('attempt') or 0)} times "
           f"(max {int(job.get('max_attempts') or 0)}): its replicas died or refused it")
    if (connection.execute(text("SELECT status FROM agent_runs WHERE id = :r"),
                           {"r": run_id}).scalar() or "") not in thread_runs.ENDED:
        connection.execute(text("UPDATE agent_runs SET status = 'failed', error = :e, "
                                "updated_at = now() WHERE id = :r"), {"r": run_id, "e": why})
        thread_runs.finish(connection, run_id, "failed")
        agent_run_worker._settled_hooks(connection, run_id)
    jobs.fail(connection, owner, job["id"], lease_token,
              {"message": why, "type": "AttemptsExhausted", "code": "attempts_exhausted"},
              retryable=False, retry_delay_seconds=0)


def _lease_one(connection: Any, settings: Any, jobs: Any, job: dict, replica: str,
               ttl: Any) -> Optional[dict]:
    """Lease one claimed job to `replica` (epoch + 1) and start its run. None when the job is
    a no-op (settled, waiting, behind another run) or its attempts are spent (parked)."""
    from . import thread_runs
    from .agent_loop import load_agent_run

    if job["status"] in ("leased", "running"):
        _requeue(connection, settings, job, reason="replica_lease_expired")
    owner = owner_of(replica)
    leased = jobs.lease_owned_job(connection, project_id=job["project_id"], job_id=job["id"],
                                  worker_id=owner, lease_ttl_seconds=_ttl(ttl))
    payload = job["payload"] if isinstance(job["payload"], dict) else json.loads(
        job["payload"] or "{}")
    run_id = str(payload.get("run_id") or "")
    if int(leased["attempt"]) > int(job.get("max_attempts") or 3):
        _park_exhausted(connection, settings, jobs, {**job, "attempt": leased["attempt"]},
                        owner, leased["lease_token"], run_id)
        return None
    run = load_agent_run(connection, run_id)
    status = str((run or {}).get("status") or "")
    if run is None or status in thread_runs.ENDED or (
            job["kind"] == "agent.run" and status in ("awaiting_approval", "pending")):
        # Settled already (a re-delivered job), or waiting on a person / behind another run:
        # the job is done, as agent_run_worker.execute's no-op answers it.
        jobs.complete(connection, owner, job["id"], leased["lease_token"],
                      {"status": status or "missing", "run_id": run_id, "noop": True})
        return None
    if status == "queued":
        _start(connection, run)
    taken = {"run_id": run_id, "job_id": job["id"], "replica_id": replica,
             "epoch": int(leased["fence_epoch"]), "run_token": leased["lease_token"],
             "kind": job["kind"], "lease_expires_at": leased["lease_expires_at"]}
    # The GUEST's handles for this run (P8b S10): `boss_mdl_` bound to (run, epoch, this
    # replica's VM), redeemed only at the central model gateway through the host. The host
    # passes them to the VM with the work item and never writes them (HOST_API.md §4.5).
    taken["handles"] = _guest_handles(connection, settings, job, run_id, replica,
                                      int(leased["fence_epoch"]))
    return taken


def _guest_handles(connection: Any, settings: Any, job: dict, run_id: str, replica: str,
                   epoch: int) -> dict:
    from . import streaming_model_gateway as smg

    try:
        row = connection.execute(text(
            "SELECT r.vm_id, r.org_id, r.deployment_id FROM agent_replicas r WHERE r.id = :r"),
            {"r": replica}).mappings().first()
        if row is None:
            return {}
        with connection.begin_nested():
            return smg.guest_handles(connection, settings, org_id=str(row["org_id"]),
                                     project_id=str(job["project_id"]), run_id=run_id,
                                     epoch=epoch, vm_id=row["vm_id"],
                                     deployment_id=row["deployment_id"])
    except Exception as exc:  # noqa: BLE001 — the run still starts; its model calls fail closed
        log.warning("no model handle for run %s: %s", run_id, type(exc).__name__)
        return {}


_ROUND_ROBIN: dict = {}


def host_replicas(connection: Any, host_id: str) -> dict:
    """{replica_id: {deployment_id, free}} — BOSS's model of the host's serving replicas."""
    return {r["id"]: {"deployment_id": r["deployment_id"],
                      "free": max(0, int(r["slots"] or 0) - int(r["busy"] or 0))}
            for r in connection.execute(_HOST_SLOTS, {"h": host_id}).mappings().all()}


def _pick(replicas: dict, deployment_id: str) -> Optional[str]:
    """The replica of `deployment_id` with the most free slots; ties in turn (round robin)."""
    fits = sorted(rid for rid, r in replicas.items()
                  if r["deployment_id"] == deployment_id and r["free"] > 0)
    if not fits:
        return None
    best = max(replicas[rid]["free"] for rid in fits)
    tied = [rid for rid in fits if replicas[rid]["free"] == best]
    turn = _ROUND_ROBIN.get(deployment_id, 0)
    _ROUND_ROBIN[deployment_id] = turn + 1
    return tied[turn % len(tied)]


def targets_of(replicas: dict, *, free_only: bool = True) -> list:
    return sorted({f"deployment:{r['deployment_id']}" for r in replicas.values()
                   if r["free"] > 0 or not free_only})


def claim_for_host(connection: Any, settings: Any, host_id: str, *,
                   state_gen: Optional[int] = None, ttl: Any = None) -> dict:
    """ONE CLAIM FOR THE WHOLE HOST, in the caller's transaction (module section). Returns
    {"resync": true} when the host's replica-state generation is not the one BOSS stored, else
    {"runs": [{run_id, job_id, replica_id, epoch, run_token, kind, lease_expires_at}],
    "targets": [...]} — the run token is the HOST's to keep in memory and attach to the VM's
    run-plane calls; it never reaches the guest."""

    stored = connection.execute(text(
        "SELECT replica_state_gen FROM agent_hosts WHERE id = :h"), {"h": host_id}).scalar()
    if state_gen is None or stored is None or int(state_gen) != int(stored):
        return {"resync": True, "runs": [], "targets": [], "state_gen": int(stored or 0)}
    offer = host_replicas(connection, host_id)
    targets = targets_of(offer)
    capacity = min(CLAIM_MAX, sum(r["free"] for r in offer.values()))
    runs: list = []
    jobs = _jobs(settings)
    skipped: set = set()
    while targets and len(runs) < capacity:
        batch = [dict(j) for j in connection.execute(_CANDIDATES, {
            "targets": targets, "n": capacity - len(runs) + len(skipped)}).mappings().all()
            if j["id"] not in skipped]
        if not batch:
            break
        for job in batch:
            deployment = str(job["target"]).split(":", 1)[1]
            replica = _pick(offer, deployment)
            if replica is None:
                continue
            try:
                # A SAVEPOINT PER JOB: one job that cannot be leased (a slot policy defers it, a
                # budget state cancels it, its row is broken) is skipped, never the whole
                # claim — a poison job at the head of the queue must not fail every poll.
                with connection.begin_nested():
                    taken = _lease_one(connection, settings, jobs, job, replica, ttl)
            except Exception as exc:  # noqa: BLE001 — skipped; the rest of the claim goes on
                log.warning("job %s skipped in a host claim: %s", job["id"], type(exc).__name__)
                skipped.add(job["id"])
                continue
            if taken is None:
                continue
            offer[replica]["free"] -= 1
            runs.append(taken)
        targets = targets_of(offer)
        if len(batch) < capacity:
            break
    return {"runs": runs, "targets": targets_of(offer, free_only=False)}


class Waiter:
    """One waiting request: an asyncio event set from any thread (the hub's listener, a sweep,
    a handler after its commit). `targets` may grow after registration (the claim names them)."""

    def __init__(self, targets: set, *, host_id: Optional[str] = None, since: int = 0):
        import asyncio

        self.loop = asyncio.get_running_loop()
        self.event = asyncio.Event()
        self.targets = set(targets)
        self.host_id, self.since = host_id, since

    def set(self) -> None:
        try:
            self.loop.call_soon_threadsafe(self.event.set)
        except RuntimeError:  # the loop is gone: the request ended
            pass

    def clear(self) -> None:
        self.event.clear()

    async def wait(self, seconds: float) -> bool:
        import asyncio

        try:
            await asyncio.wait_for(self.event.wait(), max(0.0, seconds))
            return True
        except asyncio.TimeoutError:
            return False


class _HostWaiters:
    """EVERY LONG-POLL WAITING ON THIS PROCESS, as asyncio waits — no thread is held and no
    waiter queries anything on its own (boss-eventing-lld.md §8.2):

      * work waits (hosts/next), by target: woken by the hub (`agent.run_admitted` /
        `agent.run_queued`, filtered by the NOTIFY's deployment_id), by `host:<id>` after a
        commit that moved the host's own state, and by ONE job sweep per process every
        SWEEP_SECONDS (a single query over every waited-on target);
      * desired-state waits (GET …/desired), by host and generation: woken after a commit that
        bumped a host's generation in this process, and by ONE generation sweep per process
        every DESIRED_SWEEP_SECONDS (a single query over every waiting host)."""

    DESIRED_SWEEP_SECONDS = 1.0

    def __init__(self) -> None:
        import threading

        self._lock = threading.Lock()
        self._work: set = set()
        self._desired: set = set()
        self._started = False
        self.sweeps = 0

    def start(self, engine: Any) -> None:
        import threading

        with self._lock:
            if self._started:
                return
            self._started = True
        try:
            from .event_stream import Interest, get_hub

            get_hub(engine).listen(Interest(kinds=WAKE_KINDS), self._on_note)
        except Exception:  # noqa: BLE001 — without the hub the sweeps still wake everyone
            pass
        threading.Thread(target=self._sweep_loop, args=(engine,), name="run-plane-sweep",
                         daemon=True).start()

    # registration
    def register_work(self, engine: Any, targets: set) -> Waiter:
        self.start(engine)
        waiter = Waiter(targets)
        with self._lock:
            self._work.add(waiter)
        return waiter

    def register_desired(self, engine: Any, host_id: str, since: int) -> Waiter:
        self.start(engine)
        waiter = Waiter({f"desired:{host_id}"}, host_id=host_id, since=since)
        with self._lock:
            self._desired.add(waiter)
        return waiter

    def unregister(self, waiter: Waiter) -> None:
        with self._lock:
            self._work.discard(waiter)
            self._desired.discard(waiter)

    def count(self) -> int:
        with self._lock:
            return len(self._work) + len(self._desired)

    # waking
    def _on_note(self, note: dict) -> None:
        if note.get("__resync__"):
            self.wake_all()
            return
        deployment = note.get("deployment_id")
        if deployment:
            self.wake({f"deployment:{deployment}"})

    def wake(self, targets: set) -> None:
        with self._lock:
            hit = [w for w in (*self._work, *self._desired) if w.targets & targets]
        for waiter in hit:
            waiter.set()

    def wake_all(self) -> None:
        with self._lock:
            hit = [*self._work, *self._desired]
        for waiter in hit:
            waiter.set()

    def wanted(self) -> list:
        with self._lock:
            return sorted({t for w in self._work for t in w.targets
                           if t.startswith("deployment:")})

    # the two sweeps (one query each, whatever the number of waiters)
    def sweep(self, engine: Any) -> list:
        targets = self.wanted()
        if not targets:
            return []
        with engine.connect() as c:
            found = [r[0] for r in c.execute(text("""
                SELECT DISTINCT target FROM jobs
                 WHERE target = ANY(:t) AND run_after <= now()
                   AND (status IN ('queued', 'failed_retryable')
                        OR (status IN ('leased', 'running') AND lease_expires_at <= now()))"""),
                {"t": targets}).all()]
        self.sweeps += 1
        if found:
            self.wake(set(found))
        return found

    def sweep_desired(self, engine: Any) -> None:
        with self._lock:
            waiting = list(self._desired)
        if not waiting:
            return
        with engine.connect() as c:
            gens = {r[0]: int(r[1]) for r in c.execute(text(
                "SELECT id, desired_generation FROM agent_hosts WHERE id = ANY(:h) "
                "UNION ALL "
                "SELECT id, desired_generation FROM director_nodes WHERE id = ANY(:h)"),
                {"h": sorted({w.host_id for w in waiting})}).all()}
        for waiter in waiting:
            if gens.get(waiter.host_id, 0) > waiter.since:
                waiter.set()

    def _sweep_loop(self, engine: Any) -> None:
        import time as _time

        last_jobs = 0.0
        while True:
            _time.sleep(self.DESIRED_SWEEP_SECONDS)
            try:
                self.sweep_desired(engine)
                if _time.monotonic() - last_jobs >= SWEEP_SECONDS:
                    last_jobs = _time.monotonic()
                    self.sweep(engine)
            except Exception:  # noqa: BLE001 — the next sweep tries again
                pass


WAITERS = _HostWaiters()


async def host_next(engine: Any, settings: Any, host_id: str, *, state_gen: Optional[int],
                    wait_ms: Any = 0, ttl: Any = None) -> dict:
    """POST /v1/run-plane/hosts/next: claim now; nothing claimable → an asyncio wait (≤ wait_ms)
    for this host's deployments or its own state, then claim again. A stale state_gen →
    {resync: true} at once. The claim runs in the threadpool; the wait holds no thread. The
    waiter is registered BEFORE the claim, so an admission committed during it is not lost."""
    import time as _time

    from starlette.concurrency import run_in_threadpool

    wait = max(0, min(int(wait_ms or 0), WAIT_MAX_MS)) / 1000.0
    deadline = _time.monotonic() + wait
    waiter = WAITERS.register_work(engine, {f"host:{host_id}"}) if wait else None

    def claim() -> dict:
        with engine.begin() as connection:
            return claim_for_host(connection, settings, host_id, state_gen=state_gen, ttl=ttl)

    try:
        while True:
            if waiter is not None:
                waiter.clear()
            out = await run_in_threadpool(claim)
            if out.get("resync"):
                return {"resync": True, "runs": [], "state_gen": out["state_gen"]}
            left = deadline - _time.monotonic()
            if out["runs"] or left <= 0 or waiter is None:
                return {"runs": out["runs"]}
            waiter.targets.update(out["targets"])
            await waiter.wait(left)
    finally:
        if waiter is not None:
            WAITERS.unregister(waiter)


def wake_host(host_id: str) -> None:
    """The host's own state moved (a slot freed, a replica serving): its waiting poll in this
    process claims again. Called AFTER the commit that moved it."""
    WAITERS.wake({f"host:{host_id}"})


def wake_desired(host_ids: Any) -> None:
    """Hosts whose desired generation a commit in this process just bumped."""
    WAITERS.wake({f"desired:{h}" for h in host_ids})


def _start(connection: Any, run: dict) -> dict:
    """A queued run starts: its thread's history first, then its own input (agent_run_worker
    ._drive does exactly this on the local path)."""
    from . import thread_runs
    from .agent_loop import _save_run, _zero_usage, load_agent_run

    run_id = str(run["id"])
    fctx = dict(run["finalize_ctx"] or {})
    messages = list(run["messages"] or [])
    thread_id = fctx.get("thread_id")
    if thread_id:
        history = thread_runs.thread_history(connection, thread_id, before_run=run_id)
        if history:
            head = [m for m in messages[:1] if m.get("role") == "system"]
            messages = head + history + messages[len(head):]
    _save_run(connection, run_id, run["project_id"], run["task_id"], "running", messages, [],
              _zero_usage(), fctx, step=0)
    thread_runs.finish(connection, run_id)  # thread: busy (not ended)
    return load_agent_run(connection, run_id) or run


def _package(connection: Any, run: dict, *, kind: str, lease: dict) -> dict:
    """What the runtime drives a run from: the run, its lease (never its token: the host
    attaches that), the pinned agent version (a deployed agent), and the answers a run that
    already checkpointed may resume with."""
    from .interrupts import answers_for_run

    fctx = dict(run["finalize_ctx"] or {})
    agent = None
    if (fctx.get("runner") or "") == "deployed_agent" and fctx.get("agent_version"):
        from ..agents import versions as V

        row = V.get_version(connection, str(run["project_id"]), str(fctx["agent_key"]),
                            int(fctx["agent_version"]))
        agent = {"agent_key": fctx["agent_key"], "version": int(fctx["agent_version"]),
                 "spec": row["spec"] if isinstance(row["spec"], dict) else json.loads(
                     row["spec"] or "{}"),
                 "compiled_prompt": row.get("compiled_prompt")}
    pending = connection.execute(text(
        "SELECT state->'pending' FROM agent_checkpoints WHERE run_id = :r "
        "ORDER BY seq DESC LIMIT 1"), {"r": run["id"]}).scalar()
    return wire.jsonable({
        "run": {"id": run["id"], "project_id": run["project_id"], "task_id": run["task_id"],
                "status": run["status"], "messages": run["messages"] or [],
                "finalize_ctx": fctx, "thread_id": fctx.get("thread_id"), "kind": kind},
        "lease": lease, "deployment": {"id": fctx.get("deployment_id"),
                                       "agent_key": fctx.get("agent_key"),
                                       "environment": fctx.get("environment")},
        "agent": agent,
        "answers": answers_for_run(connection, pending) if pending else {},
    })


def run_package(connection: Any, settings: Any, host_id: str, run_id: str,
                lease: LeaseRef) -> dict:
    """GET /v1/run-plane/runs/{run}: the package of a run this replica now holds (fenced)."""
    from .agent_loop import load_agent_run

    job = fence(connection, settings, host_id, run_id, lease)
    run = load_agent_run(connection, run_id)
    return _package(connection, run, kind=str(job["kind"]), lease={
        "replica_id": lease.replica_id, "job_id": lease.job_id,
        "fence_epoch": int(lease.epoch), "expires_at": job["lease_expires_at"]})


def hand_back(connection: Any, settings: Any, host_id: str, run_id: str,
              lease: LeaseRef) -> dict:
    """POST /{run}/lease {hand_back: true}: the VM refused the run or died before accepting it.
    The job goes back to the queue at once (no attempt spent) and its deployment's hosts are
    woken; the next claim leases it at epoch + 1, and this lease's writes are fenced from now."""
    from .core import emit_event

    job = fence(connection, settings, host_id, run_id, lease, settling=True)
    refusals = int(connection.execute(text(
        "UPDATE jobs SET payload = payload || jsonb_build_object('hand_backs', "
        "COALESCE((payload->>'hand_backs')::int, 0) + 1) WHERE id = :j "
        "RETURNING (payload->>'hand_backs')::int"), {"j": lease.job_id}).scalar() or 1)
    _requeue(connection, settings, job, reason="handed_back",
             give_back_attempt=refusals <= HAND_BACK_FREE)
    deployment = str(job.get("target") or "").split(":", 1)[-1] if job.get("target") else None
    if not deployment:
        deployment = connection.execute(text(
            "SELECT target FROM jobs WHERE id = :j"), {"j": lease.job_id}).scalar()
        deployment = str(deployment or "").split(":", 1)[-1] or None
    emit_event(connection, str(job["project_id"]), "agent.run_queued", owner_of(lease.replica_id),
               "agent_run", run_id, {"job_id": lease.job_id, "deployment_id": deployment,
                                     "handed_back": True})
    # That replica's slot count is BOSS's own (its live leases): the hand-back released the
    # lease, so the recount is the next claim's — nothing else to correct. (The router wakes the
    # host's waiting poll after the commit: wake_host.)
    return {"handed_back": True, "run_id": run_id}


def admit(connection: Any, settings: Any, host_id: str, replica_id: str, *, caller: str,
          input: Any, thread_id: Optional[str] = None, double_text: Optional[str] = None,
          traceparent: Optional[str] = None, ttl: Any = None) -> dict:
    """AN EXTERNAL CALL LANDED ON A REPLICA (§3.13.2): authenticate the caller's forwarded
    credential, decide `agent:invoke` with the PDP, admit the run (thread_runs.admit: run + job)
    AND lease it to this replica — one transaction. A busy thread answers `pending` with the
    replica that holds the active run (`held_by`), per the thread's double-text policy."""
    from ..identity import authn, authz
    from ..identity.scope import ResolvedScope

    replica = replica_for(connection, host_id, replica_id)
    project_id = str(replica["project_id"])
    try:
        ctx = authn.authenticate(authn.RequestCredentials(
            method="POST", path=f"/v1/agents/{replica['agent_key']}/runs",
            headers={"authorization": str(caller or "")}, cookies={}), db=connection)
    except authn.AuthnError as exc:
        raise RunPlaneError("caller_unauthenticated", 401, exc.code) from exc
    if ctx is None:
        raise RunPlaneError("caller_unauthenticated", 401, "the caller presented no credential")
    org = connection.execute(text(
        "SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
        "WHERE p.id = :p"), {"p": project_id}).scalar()
    decision = authz.check(ctx, "agent:invoke", ResolvedScope.in_project(str(org), project_id),
                           db=connection)
    if not decision.allowed:
        raise RunPlaneError("caller_forbidden", 403,
                            f"the caller may not run this agent ({decision.reason})")
    actor = ctx.principal.id
    from ..identity import lineage

    # The run is the CALLER's (the job it enqueues acts on their behalf), never the host's.
    with lineage.acting(ctx):
        return _admit_as_caller(connection, settings, replica, ctx=ctx, actor=actor,
                                project_id=project_id, input=input, thread_id=thread_id,
                                double_text=double_text, traceparent=traceparent, ttl=ttl)


def _admit_as_caller(connection: Any, settings: Any, replica: dict, *, ctx: Any, actor: str,
                     project_id: str, input: Any, thread_id: Optional[str],
                     double_text: Optional[str], traceparent: Optional[str], ttl: Any) -> dict:
    from . import agent_protocol

    replica_id = str(replica["id"])
    try:
        if thread_id:
            agent_protocol.get_thread(connection, project_id, thread_id)
        else:
            thread_id = agent_protocol.create_thread(
                connection, project_id=project_id, agent_key=replica["agent_key"], actor=actor,
                environment=replica["environment"], double_text=double_text or "enqueue"
            )["thread_id"]
        run = agent_protocol.start_run(connection, project_id=project_id, thread_id=thread_id,
                                       actor=actor, input=input, double_text=double_text,
                                       traceparent=traceparent, trigger="api")
    except agent_protocol.ProtocolError as exc:
        raise RunPlaneError("refused", exc.status, str(exc)) from exc
    row = connection.execute(text(
        "SELECT r.id, r.status, r.job_id, j.status AS job_status FROM agent_runs r "
        "LEFT JOIN jobs j ON j.id = r.job_id WHERE r.id = :r"), {"r": run["run_id"]}
    ).mappings().first()
    if row is None or not row["job_id"] or row["job_status"] != "queued":
        holder = connection.execute(text("""
            SELECT j.lease_owner FROM agent_threads t JOIN agent_runs a ON a.id = t.active_run_id
              JOIN jobs j ON j.id = a.job_id WHERE t.id = :t"""), {"t": thread_id}).scalar()
        return {"run_id": run["run_id"], "thread_id": thread_id, "status": "pending",
                "held_by": holder}
    leased = _jobs(settings).lease_owned_job(connection, project_id=project_id,
                                             job_id=row["job_id"], worker_id=owner_of(replica_id),
                                             lease_ttl_seconds=_ttl(ttl))
    from .agent_loop import load_agent_run

    started = _start(connection, load_agent_run(connection, run["run_id"]))
    package = _package(connection, started, kind="agent.run", lease={
        "replica_id": replica_id, "job_id": leased["id"], "fence_epoch": int(leased["fence_epoch"]),
        "expires_at": leased["lease_expires_at"],
        # The HOST keeps this and strips it before the response reaches the guest (HOST_API.md
        # §4.5): the guest never holds a run token.
        "run_token": leased["lease_token"]})
    return {"run_id": run["run_id"], "thread_id": thread_id, "status": "leased", **package}


# ── The lease: heartbeat, hand back, settle, fail ───────────────────────────────────────────

def heartbeat(connection: Any, settings: Any, host_id: str, run_id: str, lease: LeaseRef, *,
              ttl: Any = None) -> dict:
    """Renew the lease (10 s cadence; TTL 30 s) and answer whether the run should stop at its
    next step boundary (a double-text interrupt, a cancel: thread_runs.stop_requested) — the
    fence, the renewal and the stop flag in ONE statement (P8a-T5). What jobs.heartbeat does to
    the lease row, without its `job.heartbeat` event: on the run plane the row's heartbeat_at
    is the record (a host beats every run every 10 s; §2.15.6 write load)."""
    replica_for_any(connection, host_id, lease.replica_id)
    row = connection.execute(text(f"""
        WITH lease AS ({_LEASE_HELD}),
        beat AS (
          UPDATE jobs SET status = CASE WHEN status = 'leased' THEN 'running' ELSE status END,
                 heartbeat_at = now(), lease_expires_at = now() + make_interval(secs => :ttl),
                 updated_at = now()
           WHERE id IN (SELECT id FROM lease) RETURNING lease_expires_at)
        SELECT (SELECT lease_expires_at FROM beat) AS expires_at,
               (SELECT t.metadata->>'stop_run' FROM agent_runs r
                  JOIN agent_threads t ON t.id = r.thread_id WHERE r.id = :run) AS stop_run"""),
        {**_lease_params(lease, run_id), "ttl": _ttl(ttl)}).mappings().first()
    if row is None or row["expires_at"] is None:
        _refused(connection, settings, host_id, run_id, lease)
    return {"lease_expires_at": row["expires_at"].isoformat(),
            "stop": bool(row["stop_run"]) and row["stop_run"] == run_id}


def settle(connection: Any, settings: Any, host_id: str, run_id: str, lease: LeaseRef,
           outcome: dict) -> dict:
    """The run's drive ended: exactly agent_run_worker._settle (drained → back to the queue with
    the attempt given back; budget_wait → deferred; an unknown outcome → parked; the rest →
    thread finished, trigger told), then the job completed unless it was handed back."""
    from . import agent_run_worker

    fence(connection, settings, host_id, run_id, lease, settling=True)
    jobs = _jobs(settings)
    worker = agent_run_worker.Lease(jobs, worker_id=owner_of(lease.replica_id),
                                    job_id=lease.job_id, lease_token=lease.token)
    result = agent_run_worker._settle(connection, jobs, worker, run_id, dict(outcome or {}),
                                      handed_back=None)
    if not result.get("job_deferred"):
        jobs.complete(connection, owner_of(lease.replica_id), lease.job_id, lease.token, result)
    _revoke_handles(connection, run_id, "run settled")
    return result


def _revoke_handles(connection: Any, run_id: str, reason: str) -> None:
    """The run's model handles die with its lease (P8b S10)."""
    from . import streaming_model_gateway as smg

    smg.revoke_run(connection, run_id, reason)


def fail(connection: Any, settings: Any, host_id: str, run_id: str, lease: LeaseRef,
         error: str) -> dict:
    """The drive raised (a fatal model failure): the run is recorded failed and the thread moves
    on (agent_run_worker.execute's exception path), and the job fails without a retry — a retry
    of a failed run is a no-op."""
    from . import agent_run_worker, thread_runs

    fence(connection, settings, host_id, run_id, lease, settling=True)
    if (connection.execute(text("SELECT status FROM agent_runs WHERE id = :r"),
                           {"r": run_id}).scalar() or "") in ("running", "queued"):
        connection.execute(text("UPDATE agent_runs SET status = 'failed', error = :e, "
                                "updated_at = now() WHERE id = :r"),
                           {"r": run_id, "e": str(error)[:2000]})
    thread_runs.finish(connection, run_id, "failed")
    agent_run_worker._settled_hooks(connection, run_id)
    _jobs(settings).fail(connection, owner_of(lease.replica_id), lease.job_id, lease.token,
                         {"message": str(error)[:500], "type": "RuntimeError"}, retryable=False,
                         retry_delay_seconds=0)
    _revoke_handles(connection, run_id, "run failed")
    return {"status": "failed", "run_id": run_id}


def after_commit(engine: Any, run_id: str) -> None:
    from . import agent_run_worker

    agent_run_worker._after_commit(engine, run_id)


# ── Checkpoints ─────────────────────────────────────────────────────────────────────────────

#: THE FENCE AS ONE SQL PREDICATE, for the two hot writes (a checkpoint, a pending write): what
#: jobs.require_active_lease checks (owner, token hash, leased or running, not expired), plus the
#: epoch and the run, FOR UPDATE — in the SAME statement as the write, so the lease row is locked
#: and re-checked (READ COMMITTED re-evaluates it after any concurrent reclaim) before anything
#: is written. A statement that finds no lease writes nothing, and the precise refusal then comes
#: from `fence` itself. test_p8a_run_plane's agreement test holds the two to the same answers.
_LEASE_HELD = """
    SELECT id FROM jobs
     WHERE id = :job AND lease_owner = :owner AND lease_token_hash = :token_hash
       AND status IN ('leased', 'running') AND lease_expires_at > now()
       AND fence_epoch = :epoch AND payload->>'run_id' = :run
     FOR UPDATE"""


def _lease_params(lease: LeaseRef, run_id: str) -> dict:
    import hashlib

    return {"job": lease.job_id, "owner": owner_of(lease.replica_id), "epoch": int(lease.epoch),
            "token_hash": hashlib.sha256(lease.token.encode("utf-8")).hexdigest(), "run": run_id}


def _refused(connection: Any, settings: Any, host_id: str, run_id: str, lease: LeaseRef) -> None:
    """The one-statement write found no lease: say exactly why (fence raises), never write."""
    fence(connection, settings, host_id, run_id, lease)
    raise fenced()


def put_checkpoint(connection: Any, settings: Any, host_id: str, run_id: str, lease: LeaseRef,
                   row: dict) -> dict:
    """One checkpoint row with the run's agent_runs mirror (what PgCheckpointStore with
    AgentRunRow does on the worker), fenced, in ONE statement (one round trip; P8a-T5)."""
    from ..kernel.state import RunState

    if str(row.get("run")) != run_id:
        raise RunPlaneError("wrong_run", 400, "a run writes only its own checkpoints")
    replica_for_any(connection, host_id, lease.replica_id)
    blob = row["state"] if isinstance(row["state"], str) else json.dumps(row["state"],
                                                                         default=str)
    state = RunState.from_checkpoint(json.loads(blob))
    safe = lambda value: json.dumps(value, default=str)  # noqa: E731
    # The mirror's columns are agent_run_worker.AgentRunRow.save's; the insert is
    # PgCheckpointStore.insert_checkpoint's (test_p8a_run_plane checks both stay in step).
    held = connection.execute(text(f"""
        WITH lease AS ({_LEASE_HELD}),
        mirror AS (
          UPDATE agent_runs SET status = :s, messages = CAST(:m AS JSONB), step = :st,
                 pending = CAST(:p AS JSONB), output_text = :o, usage = CAST(:u AS JSONB),
                 updated_at = now()
           WHERE id = :run AND EXISTS (SELECT 1 FROM lease) RETURNING 1),
        written AS (
          INSERT INTO agent_checkpoints (checkpoint_id, run_id, thread_id, parent_checkpoint_id,
              ns, step, source, phase, status, state, state_bytes, durability, metadata)
          SELECT :id, :run, :thread, :parent, :ns, CAST(:step AS INTEGER), :source, :phase,
                 :status, CAST(:state AS JSONB), CAST(:bytes AS INTEGER), :mode,
                 CAST(:meta AS JSONB)
           WHERE EXISTS (SELECT 1 FROM lease)
          ON CONFLICT (checkpoint_id) DO NOTHING RETURNING 1)
        SELECT EXISTS (SELECT 1 FROM lease)"""),
        {**row, **_lease_params(lease, run_id), "state": blob, "s": state.status,
         "m": safe(state.openai_messages()), "st": int(state.step),
         "p": safe(state.pending) if state.pending else None, "o": state.final_text,
         "u": safe(state.usage.as_adapter_usage())}).scalar()
    if not held:
        _refused(connection, settings, host_id, run_id, lease)
    return {"checkpoint_id": row["id"]}


def put_write(connection: Any, settings: Any, host_id: str, run_id: str, lease: LeaseRef,
              row: dict) -> dict:
    """One pending write (a 'started' marker or a result), fenced, in one statement."""
    if str(row.get("run")) != run_id:
        raise RunPlaneError("wrong_run", 400, "a run writes only its own pending writes")
    replica_for_any(connection, host_id, lease.replica_id)
    held = connection.execute(text(f"""
        WITH lease AS ({_LEASE_HELD}),
        written AS (
          INSERT INTO agent_checkpoint_writes (checkpoint_id, call_id, run_id, step, idx, tool,
              idempotency, idempotency_key, status, result, rendered)
          SELECT :ckpt, :call, :run, CAST(:step AS INTEGER), CAST(:idx AS INTEGER), :tool, :idem,
                 :key, :status, CAST(:result AS JSONB), :rendered
           WHERE EXISTS (SELECT 1 FROM lease)
          ON CONFLICT (checkpoint_id, call_id) DO UPDATE SET
            status = EXCLUDED.status, result = EXCLUDED.result, rendered = EXCLUDED.rendered,
            idempotency = COALESCE(EXCLUDED.idempotency, agent_checkpoint_writes.idempotency),
            idempotency_key = COALESCE(EXCLUDED.idempotency_key,
                                       agent_checkpoint_writes.idempotency_key),
            updated_at = now()
          RETURNING 1)
        SELECT EXISTS (SELECT 1 FROM lease)"""), {**row, **_lease_params(lease, run_id)}).scalar()
    if not held:
        _refused(connection, settings, host_id, run_id, lease)
    return {"call_id": row["call"], "status": row["status"]}


def get_checkpoints(connection: Any, settings: Any, host_id: str, run_id: str, lease: LeaseRef,
                    *, checkpoint_id: Optional[str] = None, ns: str = "",
                    history: bool = False) -> dict:
    fence(connection, settings, host_id, run_id, lease)
    if history:
        rows = connection.execute(text(
            "SELECT checkpoint_id, parent_checkpoint_id, step, source, phase, status, "
            "state_bytes, durability FROM agent_checkpoints WHERE run_id = :r ORDER BY seq"),
            {"r": run_id}).mappings().all()
        return {"history": [dict(r) for r in rows]}
    if checkpoint_id:
        row = connection.execute(text(
            "SELECT checkpoint_id, state FROM agent_checkpoints WHERE checkpoint_id = :c "
            "AND run_id = :r"), {"c": checkpoint_id, "r": run_id}).first()
    else:
        row = connection.execute(text(
            "SELECT checkpoint_id, state FROM agent_checkpoints WHERE run_id = :r AND ns = :ns "
            "ORDER BY seq DESC LIMIT 1"), {"r": run_id, "ns": ns}).first()
    if row is None:
        return {"checkpoint": None}
    return {"checkpoint": {"checkpoint_id": row[0], "state": row[1]}}


def get_writes(connection: Any, settings: Any, host_id: str, run_id: str, lease: LeaseRef,
               checkpoint_id: str) -> dict:
    fence(connection, settings, host_id, run_id, lease)
    rows = connection.execute(text(
        "SELECT w.call_id, w.tool, w.status, w.result, w.rendered, w.idempotency, "
        "w.idempotency_key FROM agent_checkpoint_writes w WHERE w.checkpoint_id = :c "
        "AND w.run_id = :r ORDER BY w.idx"), {"c": checkpoint_id, "r": run_id}
    ).mappings().all()
    return {"writes": {str(r["call_id"]): wire.jsonable(dict(r)) for r in rows}}


# ── Governance: interrupts, budget, ledger, spans, events ───────────────────────────────────

def open_interrupt(connection: Any, settings: Any, host_id: str, run_id: str, lease: LeaseRef,
                   item: dict) -> dict:
    from .interrupts import DecisionInterrupts

    job = fence(connection, settings, host_id, run_id, lease)
    port = DecisionInterrupts(connection, project_id=str(job["project_id"]),
                              task_id=job.get("task_id"))
    return {"decision_id": port.open(SimpleNamespace(run_id=run_id), wire.interrupt_in(item))}


def interrupt_answers(connection: Any, settings: Any, host_id: str, run_id: str,
                      lease: LeaseRef) -> dict:
    from .interrupts import answers_for_run

    fence(connection, settings, host_id, run_id, lease)
    pending = connection.execute(text(
        "SELECT state->'pending' FROM agent_checkpoints WHERE run_id = :r "
        "ORDER BY seq DESC LIMIT 1"), {"r": run_id}).scalar()
    return {"answers": answers_for_run(connection, pending) if pending else {}}


def _gate(connection: Any, settings: Any, project_id: str, run_id: str, skill_id: str) -> Any:
    from .model_routing import governed

    return governed(None, connection, settings, project_id, skill_id=skill_id or
                    "model.agent_turn", ref=run_id)


def budget_reserve(connection: Any, settings: Any, host_id: str, run_id: str, lease: LeaseRef,
                   *, skill_id: str = "") -> dict:
    """One model turn reserved and receipted (GovernedChatAdapter._reserve), fenced."""
    from .model_routing import ModelSpendDeferred, ModelSpendRefused

    job = fence(connection, settings, host_id, run_id, lease)
    try:
        held = _gate(connection, settings, str(job["project_id"]), run_id, skill_id)._reserve(
            connection)
    except ModelSpendDeferred as exc:
        raise RunPlaneError("spend_deferred", 429, str(exc),
                            retry_after_seconds=getattr(exc, "retry_after_seconds", None)) from exc
    except ModelSpendRefused as exc:
        raise RunPlaneError("spend_refused", 402, str(exc)) from exc
    return {"held": list(held)}


def budget_settle(connection: Any, settings: Any, host_id: str, run_id: str, lease: LeaseRef, *,
                  held: list, usage: Optional[dict] = None, failed: Optional[dict] = None,
                  skill_id: str = "") -> dict:
    job = fence(connection, settings, host_id, run_id, lease, settling=True)
    gate = _gate(connection, settings, str(job["project_id"]), run_id, skill_id)
    pair = (str(held[0]), str(held[1]))
    if failed:
        error = RuntimeError(str(failed.get("message") or "the model call failed")[:500])
        if failed.get("not_dispatched"):
            error.provider_not_dispatched = True  # type: ignore[attr-defined]
        gate._fail(pair, error)
        return {"released": True}
    gate._settle_on(connection, pair, usage or {})
    return {"settled": True}


class _FencedEngine:
    """An engine whose every `begin()` is a savepoint on the fenced connection: the ledger and
    span sinks write in their own short transactions on the worker; here they commit with the
    fence or not at all."""

    def __init__(self, connection: Any):
        self._c = connection
        self.dialect = connection.dialect
        self.url = connection.engine.url

    @contextlib.contextmanager
    def begin(self):
        with self._c.begin_nested():
            yield self._c

    connect = begin


def ledger_seq(connection: Any, settings: Any, host_id: str, run_id: str,
               lease: LeaseRef) -> dict:
    from ..kernel.ledger import next_seq

    fence(connection, settings, host_id, run_id, lease)
    return {"seq": next_seq(connection)}


def ledger_write(connection: Any, settings: Any, host_id: str, run_id: str, lease: LeaseRef, *,
                 event: dict, state: dict, meta: Optional[dict] = None) -> dict:
    from ..kernel.ledger import DbSink

    fence(connection, settings, host_id, run_id, lease)
    ev = wire.event_in(event)
    if ev.run_id != run_id:
        raise RunPlaneError("wrong_run", 400, "a run writes only its own ledger")
    who = SimpleNamespace(project_id=str(state.get("project_id") or ""), run_id=run_id,
                          act_id=state.get("act_id"), attempt=int(state.get("attempt") or 1),
                          lineage_id=state.get("lineage_id") or run_id)
    DbSink(_FencedEngine(connection)).write(ev, who, wire.meta_in(meta))
    return {"written": True}


def span_write(connection: Any, settings: Any, host_id: str, run_id: str, lease: LeaseRef,
               span: dict) -> dict:
    from .traces import DbSpanSink

    fence(connection, settings, host_id, run_id, lease)
    item = wire.span_in(span)
    if item.run_id != run_id:
        raise RunPlaneError("wrong_run", 400, "a run writes only its own spans")
    DbSpanSink(_FencedEngine(connection)).write_span(item)
    return {"span_id": item.span_id}


def event_write(connection: Any, settings: Any, host_id: str, run_id: str, lease: LeaseRef, *,
                type: str, data: Optional[dict] = None) -> dict:
    from .core import emit_event

    job = fence(connection, settings, host_id, run_id, lease)
    if not _EVENT_TYPE.match(str(type or "")):
        raise RunPlaneError("bad_event_type", 400, "an event type is lower_snake.dotted")
    event_id = emit_event(connection, str(job["project_id"]), f"agent.run.{type}",
                          owner_of(lease.replica_id), "agent_run", run_id,
                          wire.jsonable(data or {}))
    return {"event_id": event_id}


# ── BOSS-state tools on the control plane (D1a) ─────────────────────────────────────────────

def _server_tools(connection: Any, settings: Any, run: dict) -> Any:
    """The run's BOSS-state tools, as the worker builds them for a deployed agent: exactly the
    agent's grants (AgentStepToolSet), or a test's PARTS_HOOK."""
    from . import bots
    from .agent_loop import AgentStepToolSet

    fctx = dict(run.get("finalize_ctx") or {})
    if bots.PARTS_HOOK is not None:
        from ..agents import versions as V

        row = V.get_version(connection, str(run["project_id"]), str(fctx.get("agent_key")),
                            int(fctx.get("agent_version") or 0))
        return bots.PARTS_HOOK(connection, settings, run, row)["tools"]
    bounded = {**run, "finalize_ctx": {**fctx, "agent_step": {"role_key": fctx.get("agent_key")}}}
    return AgentStepToolSet(connection, settings, bounded, str(run["id"]))


@contextlib.contextmanager
def _as_agent(connection: Any, run: dict):
    """The run's delegated authority (the person it is for, through this agent), as the
    deployed runner drives it on a worker: every tool's identity gate decides against it."""
    from ..identity import catalogue as _catalogue
    from ..identity import runs as _identity_runs
    from .connection_credentials import acting_agent

    fctx = dict(run.get("finalize_ctx") or {})
    key = str(fctx.get("agent_key") or "")
    with _identity_runs.delegated(connection.engine, kind="ag", entity_id=str(run["id"]),
                                  project_id=str(run["project_id"]), role_key=key,
                                  scope=_catalogue.delegable(),
                                  job_id=fctx.get("created_by_job_id")), acting_agent(key):
        yield


def _state_for(connection: Any, run: dict) -> Any:
    from ..kernel.state import RunState

    row = connection.execute(text(
        "SELECT state FROM agent_checkpoints WHERE run_id = :r ORDER BY seq DESC LIMIT 1"),
        {"r": run["id"]}).scalar()
    if row is not None:
        return RunState.from_checkpoint(row if isinstance(row, dict) else json.loads(row))
    return RunState(run_id=str(run["id"]), project_id=str(run["project_id"]),
                    lineage_id=str(run["id"]), system=[], messages=[])


def tool_schemas(connection: Any, settings: Any, host_id: str, run_id: str,
                 lease: LeaseRef) -> dict:
    from .agent_loop import load_agent_run

    fence(connection, settings, host_id, run_id, lease)
    run = load_agent_run(connection, run_id)
    with _as_agent(connection, run):
        state = _state_for(connection, run)
        schemas = _server_tools(connection, settings, run).schemas(step=int(state.step),
                                                                  state=state)
    return {"tools": [wire.schema_out(s) for s in schemas]}


def tool_invoke(connection: Any, settings: Any, host_id: str, run_id: str, lease: LeaseRef, *,
                name: str, call: dict, approved: Optional[dict] = None) -> dict:
    """`POST /tools/{name}/invoke`: a BOSS-state tool runs HERE, on the control plane, under the
    run's delegated authority and the toolset's own gates (D1a) — the VM holds no database."""
    from ..kernel.types import ToolCall
    from .agent_loop import load_agent_run

    fence(connection, settings, host_id, run_id, lease)
    run = load_agent_run(connection, run_id)
    request = ToolCall(id=str(call.get("id") or ""), name=name,
                       arguments=call.get("arguments") if isinstance(call.get("arguments"),
                                                                     dict) else None,
                       raw_arguments=str(call.get("raw_arguments") or ""))
    with _as_agent(connection, run):
        result = _server_tools(connection, settings, run).invoke(
            request, state=_state_for(connection, run), approved=approved)
    return {"result": wire.result_out(result)}


__all__ = ["KINDS", "LEASE_TTL_SECONDS", "LeaseRef", "RunPlaneError", "admit", "after_commit",
           "budget_reserve", "budget_settle", "event_write", "fail", "fence", "fenced",
           "get_checkpoints", "get_writes", "heartbeat", "interrupt_answers", "ledger_seq",
           "ledger_write", "open_interrupt", "owner_of", "put_checkpoint",
           "put_write", "replica_for", "run_package", "settle", "hand_back", "host_next", "claim_for_host", "span_write", "tool_invoke", "tool_schemas"]
