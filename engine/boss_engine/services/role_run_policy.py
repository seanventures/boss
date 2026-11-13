"""Per-agent run policy, enforced where a job is claimed (BOSSAPP-857).

`max_concurrent_jobs` is a PROJECT envelope, so one runaway role could take all of it. A role
may now carry its own `run_policy` (roles.run_policy JSONB):

    {"max_concurrent": 2, "max_daily_runs": 40, "max_daily_cost_usd": 25}

  * max_concurrent      jobs of this role leased/running at once;
  * max_daily_runs      jobs of this role claimed since 00:00 UTC (role_job_claims);
  * max_daily_cost_usd  metered model spend attributed to this role since 00:00 UTC (llm_calls,
                        the same attribution llm_telemetry uses: agent_acts.role_key, else
                        llm_calls.role; subscription-included calls cost nothing).

A job belongs to a role when it was enqueued with one (jobs.role_key, or `role_key` in its
payload). JobLeaseService.claim_job asks `hold_for` after the budget gate: a held job stays
queued with a later run_after and a `last_error.kind = "role_run_policy"` saying why, exactly as
a budget hold does. A role with an empty policy (the default) is never asked anything: the claim
path is unchanged, and no claim is recorded.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

POLICY_KEYS = ("max_concurrent", "max_daily_runs", "max_daily_cost_usd")
HOLD_KIND = "role_run_policy"
#: A concurrency hold is re-asked soon (a slot frees when a job ends); a daily one at 00:00 UTC.
CONCURRENCY_RETRY_SECONDS = 30


class RunPolicyError(ValueError):
    """A policy the caller can fix, phrased for them."""


def normalise(raw: Any) -> dict[str, Any]:
    """A stored policy from what a person typed. Empty/None clears it. Unknown keys refused."""
    if raw in (None, "", {}):
        return {}
    if not isinstance(raw, Mapping):
        raise RunPolicyError("run_policy must be an object")
    unknown = sorted(set(raw) - set(POLICY_KEYS))
    if unknown:
        raise RunPolicyError(f"unknown run_policy keys {unknown}; known: {list(POLICY_KEYS)}")
    out: dict[str, Any] = {}
    for key in ("max_concurrent", "max_daily_runs"):
        value = raw.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise RunPolicyError(f"{key} must be a whole number of at least 1")
        out[key] = value
    cost = raw.get("max_daily_cost_usd")
    if cost is not None:
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost <= 0:
            raise RunPolicyError("max_daily_cost_usd must be a positive amount")
        out["max_daily_cost_usd"] = float(cost)
    return out


def policy_for(connection: Connection, project_id: str, role_key: str) -> dict[str, Any]:
    row = connection.execute(
        text("SELECT run_policy FROM roles WHERE project_id = :p AND role_key = :r"),
        {"p": project_id, "r": role_key}).first()
    value = row[0] if row is not None else None
    if isinstance(value, str):
        value = json.loads(value or "{}")
    return dict(value or {})


def set_policy(connection: Connection, project_id: str, role_key: str, raw: Any) -> dict:
    policy = normalise(raw)
    done = connection.execute(
        text("UPDATE roles SET run_policy = CAST(:v AS JSONB) "
             "WHERE project_id = :p AND role_key = :r RETURNING role_key"),
        {"v": json.dumps(policy), "p": project_id, "r": role_key}).first()
    if done is None:
        raise RunPolicyError(f"no agent {role_key!r} in this project")
    return policy


def role_of(candidate: Mapping[str, Any]) -> Optional[str]:
    """The role a job runs as: its column, else the payload's `role_key`."""
    role = candidate.get("role_key")
    if not role:
        payload = candidate.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        role = payload.get("role_key") if isinstance(payload, Mapping) else None
    role = str(role or "").strip()
    return role or None


@dataclass(frozen=True)
class Hold:
    limit: str
    message: str
    until: datetime

    def last_error(self) -> dict[str, Any]:
        return {"message": self.message, "kind": HOLD_KIND, "limit": self.limit}


def _day_start(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def usage_today(connection: Connection, project_id: str, role_key: str, *,
                exclude_job: Optional[str] = None, now: Optional[datetime] = None
                ) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    since = _day_start(now)
    running = connection.execute(
        text("SELECT count(*) FROM jobs WHERE project_id = :p "
             "AND COALESCE(role_key, payload->>'role_key') = :r "
             "AND status IN ('leased', 'running') AND lease_expires_at > :now "
             "AND (CAST(:j AS TEXT) IS NULL OR id <> :j)"),
        {"p": project_id, "r": role_key, "now": now, "j": exclude_job}).scalar() or 0
    runs = connection.execute(
        text("SELECT count(*) FROM role_job_claims WHERE project_id = :p AND role_key = :r "
             "AND claimed_at >= :since AND (CAST(:j AS TEXT) IS NULL OR job_id <> :j)"),
        {"p": project_id, "r": role_key, "since": since, "j": exclude_job}).scalar() or 0
    spent = connection.execute(
        text("SELECT COALESCE(SUM(c.cost_usd_micros), 0) FROM llm_calls c "
             "LEFT JOIN agent_acts a ON a.act_id = c.act_id "
             "WHERE c.project_id = :p AND COALESCE(a.role_key, c.role) = :r "
             "AND NOT COALESCE(c.included_in_subscription, false) AND c.created_at >= :since"),
        {"p": project_id, "r": role_key, "since": since}).scalar() or 0
    return {"running": int(running), "runs_today": int(runs), "cost_usd_micros_today": int(spent)}


def hold_for(connection: Connection, project_id: str, role_key: str, job_id: str,
             policy: Mapping[str, Any], *, now: Optional[datetime] = None) -> Optional[Hold]:
    """Why this role's job must wait, or None. Nothing is read when the policy is empty."""
    if not policy:
        return None
    now = now or datetime.now(timezone.utc)
    use = usage_today(connection, project_id, role_key, exclude_job=job_id, now=now)
    tomorrow = _day_start(now) + timedelta(days=1)
    cap = policy.get("max_concurrent")
    if cap and use["running"] >= int(cap):
        return Hold("max_concurrent",
                    f"Held by {role_key}'s run policy: {use['running']} of its {cap} concurrent "
                    "jobs are running; it starts when one ends.",
                    now + timedelta(seconds=CONCURRENCY_RETRY_SECONDS))
    cap = policy.get("max_daily_runs")
    if cap and use["runs_today"] >= int(cap):
        return Hold("max_daily_runs",
                    f"Held by {role_key}'s run policy: it has run {use['runs_today']} of its "
                    f"{cap} jobs today; it starts at 00:00 UTC.", tomorrow)
    cap = policy.get("max_daily_cost_usd")
    if cap and use["cost_usd_micros_today"] >= int(float(cap) * 1_000_000):
        return Hold("max_daily_cost_usd",
                    f"Held by {role_key}'s run policy: it has spent "
                    f"${use['cost_usd_micros_today'] / 1_000_000:.2f} of its ${float(cap):.2f} "
                    "daily model budget; it starts at 00:00 UTC.", tomorrow)
    return None


def defer(connection: Connection, job_id: str, hold: Hold) -> None:
    connection.execute(
        text("UPDATE jobs SET status = 'queued', run_after = :ra, "
             "last_error = CAST(:le AS JSONB), updated_at = now() WHERE id = :id"),
        {"ra": hold.until, "le": json.dumps(hold.last_error()), "id": job_id})


def record_claim(connection: Connection, project_id: str, role_key: str, job_id: str) -> None:
    """Count a claim toward the role's daily runs. A reclaim of the same job counts once."""
    connection.execute(
        text("INSERT INTO role_job_claims (job_id, project_id, role_key) VALUES (:j, :p, :r) "
             "ON CONFLICT (job_id) DO NOTHING"),
        {"j": job_id, "p": project_id, "r": role_key})
