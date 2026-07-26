"""Chaos / load fault-injection harness (spec §F: load + chaos testing). Injects controlled faults
against the live primitives and records whether the system recovered or detected them:

- lease_reclaim / worker_crash: lease a job, expire the lease (simulate a crashed worker), and prove
  another worker reclaims it — the durable-execution guarantee under fault.
- deadletter_storm: flood deadlettered jobs and prove the anomaly detector raises a retry-storm
  incident — the detection guarantee under load.

Each run persists to chaos_runs with evidence so resilience is provable, not assumed.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from ..db import begin
from .core import emit_event, make_id
from .jobs import JobLeaseService

SCENARIOS = ("lease_reclaim", "worker_crash", "deadletter_storm")


def _chaos_lease_reclaim(engine, settings, project_id):
    jl = JobLeaseService(settings)
    with begin(engine) as c:
        job = jl.create_job(c, project_id, kind="chaos.noop", payload={"chaos": True})
    with begin(engine) as c:
        leased = jl.claim_job(c, "chaos-worker-a", ["chaos.noop"], 300)
    if not leased or leased["id"] != job["id"]:
        return False, f"initial claim failed (got {leased['id'] if leased else None})", {}
    # Simulate a crashed worker: its lease expires without completion.
    with begin(engine) as c:
        c.execute(
            text("UPDATE jobs SET lease_expires_at = now() - interval '5 seconds' WHERE id = :i"),
            {"i": job["id"]},
        )
    with begin(engine) as c:
        reclaimed = jl.claim_job(c, "chaos-worker-b", ["chaos.noop"], 300)
    recovered = bool(reclaimed and reclaimed["id"] == job["id"])
    if recovered:
        with begin(engine) as c:
            jl.complete(c, "chaos-worker-b", reclaimed["id"], reclaimed["lease_token"],
                        {"chaos": "recovered"})
    detail = (
        f"job {job['id']} leased by worker-a, lease expired (crash), "
        f"reclaimed by worker-b = {recovered}"
    )
    return recovered, detail, {"job_id": job["id"], "reclaimed": recovered}


def _chaos_deadletter_storm(engine, settings, project_id):
    from .anomaly_detector import RETRY_STORM_THRESHOLD, scan
    jl = JobLeaseService(settings)
    job_ids = []
    for _ in range(RETRY_STORM_THRESHOLD):
        with begin(engine) as c:
            job = jl.create_job(c, project_id, kind="chaos.storm", payload={"chaos": True})
        with begin(engine) as c:
            c.execute(
                text("UPDATE jobs SET status = 'deadlettered', created_at = now() WHERE id = :i"),
                {"i": job["id"]},
            )
        job_ids.append(job["id"])
    result = scan(engine, settings, project_id, auto_lite=False)
    detected = any(a["type"] == "retry_storm" for a in result["anomalies"])
    detail = (
        f"injected {len(job_ids)} deadlettered jobs; "
        f"anomaly detector raised retry_storm = {detected}"
    )
    return detected, detail, {"injected_jobs": len(job_ids), "anomalies": result["anomalies"]}


_RUNNERS = {
    "lease_reclaim": _chaos_lease_reclaim,
    "worker_crash": _chaos_lease_reclaim,
    "deadletter_storm": _chaos_deadletter_storm,
}


def run_chaos(engine, settings, project_id: str, scenario: str, actor: str = "chaos") -> dict:
    runner = _RUNNERS.get(scenario)
    if runner is None:
        raise ValueError(f"unknown chaos scenario '{scenario}'; valid: {sorted(_RUNNERS)}")
    recovered, detail, evidence = runner(engine, settings, project_id)
    run_id = make_id("chaos")
    with begin(engine) as c:
        c.execute(
            text(
                """
                INSERT INTO chaos_runs (id, project_id, scenario, injected, recovered, detail, evidence)
                VALUES (:id, :p, :s, true, :rec, :d, CAST(:ev AS JSONB))
                """
            ),
            {"id": run_id, "p": project_id, "s": scenario, "rec": recovered,
             "d": detail, "ev": json.dumps(evidence)},
        )
        emit_event(c, project_id, "chaos.injected", actor, "chaos_run", run_id,
                   {"scenario": scenario, "recovered": recovered})
    return {"id": run_id, "scenario": scenario, "recovered": recovered,
            "detail": detail, "evidence": evidence}


def list_chaos_runs(engine, project_id: str, limit: int = 20) -> list[dict]:
    with begin(engine) as c:
        rows = c.execute(
            text(
                "SELECT id, scenario, injected, recovered, detail, evidence, created_at "
                "FROM chaos_runs WHERE project_id = :p ORDER BY created_at DESC LIMIT :l"
            ),
            {"p": project_id, "l": limit},
        ).mappings().all()
    return [dict(r) for r in rows]
