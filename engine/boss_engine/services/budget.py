from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .included_spend import INCLUDED_SQL, MONEY_SQL
from .core import (
    create_blocking_decision,
    emit_event,
    expires_at,
    make_id,
    now_utc,
    record_policy_eval,
)


def burst_retry_after_seconds(frees_in: object) -> int:
    """Seconds until the per-minute window has room again: when enough of the last minute's
    actual spend ages out. Never below 5 (a tight loop of refusals helps nobody) nor above 60
    (the window)."""
    try:
        seconds = int(float(frees_in)) + 1  # type: ignore[arg-type]
    except (TypeError, ValueError):
        seconds = 60
    return max(5, min(60, seconds))


#: A reservation refused only because OTHER work's active reservations fill the cap. Those are
#: consumed or released when that work ends, so the refusal clears by itself; how soon is not
#: knowable here, so the caller looks again in a minute. Read by model_failures.explain.
HEADROOM_MESSAGE = "Budget headroom is held by other work's active reservations. Retry shortly."
HEADROOM_RETRY_AFTER_SECONDS = 60

#: The per-minute cap refusals. Both are `refusal: "burst"`, transient, and read by
#: model_failures.explain as "Waiting for the per-minute budget to free up."
BURST_MESSAGE = "Burst cap exceeded ($/min). Retry shortly."
BURST_QUEUED_MESSAGE = ("Waiting for the per-minute budget: requests that have waited longer "
                        "are served first (burst cap). Retry shortly.")

#: The trailing window the burst cap measures, and how long past its expected return a waiting
#: request keeps its place in line before it is presumed gone.
BURST_WINDOW_SECONDS = 60
BURST_WAITER_GRACE_SECONDS = 30


def burst_waiter_key(category: str, reason: str) -> str:
    """Who is waiting, stable across that request's retries — even when each retry is a fresh
    job, as a deferred discovery stage is. A request with a job is ALSO matched by its job, so
    one whose reason changes between attempts keeps its place. Two requests sharing a key share
    one place in line — harmless."""
    return f"{category}:{reason}"[:500]


class BudgetGovernor:
    def __init__(self, settings: Settings):
        self.settings = settings

    def expire_stale_reservations(
        self, connection: Connection, project_id: Optional[str] = None
    ) -> list[str]:
        query_params: dict[str, Any] = {"now_utc": now_utc()}
        if project_id is None:
            query = text(
                """
                UPDATE budget_reservations
                SET status = 'expired'
                WHERE status = 'active'
                  AND expires_at <= :now_utc
                RETURNING id, project_id
                """
            )
        else:
            query = text(
                """
                UPDATE budget_reservations
                SET status = 'expired'
                WHERE status = 'active'
                  AND expires_at <= :now_utc
                  AND project_id = :project_id
                RETURNING id, project_id
                """
            )
            query_params["project_id"] = project_id

        rows = connection.execute(query, query_params).mappings().all()

        expired_ids: list[str] = []
        for row in rows:
            expired_ids.append(row["id"])
            emit_event(
                connection,
                row["project_id"],
                "budget.reservation_expired",
                "budget_governor",
                "budget_reservation",
                row["id"],
                {},
            )
        return expired_ids

    def status_for_project(self, connection: Connection, project_id: str) -> dict:
        self.expire_stale_reservations(connection, project_id)
        policy_row = connection.execute(
            text(
                """
                SELECT daily_cap_usd_micros, soft_cap_pct, auto_lite_mode_pct,
                       freeze_provisioning_pct, freeze_tier2_pct, operational_intensity
                FROM budget_policies
                WHERE scope_type = 'project' AND scope_id = :project_id
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"project_id": project_id},
        ).mappings().first()

        if not policy_row:
            return {
                "budget_state": "unknown",
                "cap_usd_micros": 0,
                "reserved_usd_micros": 0,
                "actual_usd_micros": 0,
                "available_usd_micros": 0,
                "committed_usd_micros": 0,
                "burn_approx_usd_micros": 0,
                "burn_window_hours": 24,
                "burn_rate_usd_micros_per_hour": 0,
                "forecasted_24h_usd_micros": 0,
                "time_to_cap_seconds": None,
                "utilization_ratio": 0.0,
                "active_reservations_count": 0,
                "top_cost_drivers": [],
                "included_in_subscription_usd_micros": 0,
                "included_cost_drivers": [],
                "soft_cap_pct": 0.7,
                "auto_lite_mode_pct": 0.85,
                "freeze_provisioning_pct": 0.9,
                "freeze_tier2_pct": 0.95,
                "tier2_frozen": False,
            }

        reservation_snapshot = connection.execute(
            text(
                """
                SELECT
                    COALESCE(SUM(amount_usd_micros), 0) AS total,
                    COUNT(*) AS active_count
                FROM budget_reservations
                WHERE project_id = :project_id AND status = 'active'
                """
            ),
            {"project_id": project_id},
        ).mappings().one()
        reserved = int(reservation_snapshot["total"])
        active_reservations_count = int(reservation_snapshot["active_count"])

        actual = connection.execute(
            text(
                f"""
                SELECT COALESCE(SUM({MONEY_SQL}), 0) AS total
                FROM cost_receipts
                WHERE project_id = :project_id
                """
            ),
            {"project_id": project_id},
        ).scalar_one()
        actual = int(actual)

        burn_window_hours = 24
        burn_window_start = now_utc() - timedelta(hours=burn_window_hours)
        recent_actual = connection.execute(
            text(
                f"""
                SELECT COALESCE(SUM({MONEY_SQL}), 0) AS total
                FROM cost_receipts
                WHERE project_id = :project_id
                  AND created_at >= :burn_window_start
                """
            ),
            {
                "project_id": project_id,
                "burn_window_start": burn_window_start,
            },
        ).scalar_one()
        recent_actual = int(recent_actual)

        top_cost_drivers = [
            {
                "skill_or_model_id": row["skill_or_model_id"],
                "amount_usd_micros": int(row["amount_usd_micros"]),
            }
            for row in connection.execute(
                text(
                    f"""
                    SELECT
                        skill_or_model_id,
                        COALESCE(SUM({MONEY_SQL}), 0) AS amount_usd_micros
                    FROM cost_receipts
                    WHERE project_id = :project_id
                      AND NOT COALESCE(included_in_subscription, false)
                    GROUP BY skill_or_model_id
                    ORDER BY amount_usd_micros DESC, skill_or_model_id ASC
                    LIMIT 5
                    """
                ),
                {"project_id": project_id},
            ).mappings().all()
        ]

        # WHAT THE SUBSCRIPTIONS DID, AT LIST PRICE (included_spend.py). Not money and not in
        # any cap — reported beside the money so it is never silently dropped.
        included = connection.execute(
            text(
                f"""
                SELECT skill_or_model_id, COALESCE(SUM({INCLUDED_SQL}), 0) AS amount
                FROM cost_receipts
                WHERE project_id = :project_id
                  AND COALESCE(included_in_subscription, false)
                GROUP BY skill_or_model_id
                ORDER BY amount DESC, skill_or_model_id ASC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        included_total = sum(int(row["amount"] or 0) for row in included)
        included_drivers = [
            {"skill_or_model_id": row["skill_or_model_id"],
             "amount_usd_micros": int(row["amount"] or 0)}
            for row in included[:5]
        ]

        cap = int(policy_row["daily_cap_usd_micros"])
        committed = reserved + actual
        available = max(cap - committed, 0) if cap else 0
        utilization = 0 if cap == 0 else committed / cap
        soft = float(policy_row["soft_cap_pct"])
        orange = float(policy_row["auto_lite_mode_pct"])
        red = float(policy_row["freeze_provisioning_pct"])
        tier2 = float(policy_row["freeze_tier2_pct"] or 0.95)
        # Enforced (BOSS-280): past this utilization, tier-2+ risk work is refused at reserve().
        tier2_frozen = bool(cap and utilization >= tier2)

        budget_state = self._state_for_totals(
            cap,
            committed,
            soft,
            orange,
            red,
        )
        burn_rate = int(recent_actual / burn_window_hours)
        forecasted_24h = recent_actual + reserved
        time_to_cap_seconds: Optional[int] = None
        if cap and committed >= cap:
            time_to_cap_seconds = 0
        elif cap and burn_rate > 0:
            remaining = max(cap - committed, 0)
            time_to_cap_seconds = int((remaining / burn_rate) * 3600)

        return {
            "budget_state": budget_state,
            "cap_usd_micros": cap,
            "reserved_usd_micros": reserved,
            "actual_usd_micros": actual,
            "available_usd_micros": available,
            "committed_usd_micros": committed,
            "burn_approx_usd_micros": committed,
            "burn_window_hours": burn_window_hours,
            "burn_rate_usd_micros_per_hour": burn_rate,
            "forecasted_24h_usd_micros": forecasted_24h,
            "time_to_cap_seconds": time_to_cap_seconds,
            "utilization_ratio": round(utilization, 6),
            "active_reservations_count": active_reservations_count,
            "top_cost_drivers": top_cost_drivers,
            "included_in_subscription_usd_micros": included_total,
            "included_cost_drivers": included_drivers,
            "soft_cap_pct": soft,
            "auto_lite_mode_pct": orange,
            "freeze_provisioning_pct": red,
            "freeze_tier2_pct": tier2,
            "tier2_frozen": tier2_frozen,
            "operational_intensity": policy_row["operational_intensity"],
        }

    def set_intensity(
        self, connection: Connection, scope_type: str, scope_id: str, intensity: int,
        actor: str = "ceo",
    ) -> dict:
        """CEO Operational Intensity slider (0-100) → spend envelope + scaling limits.
        Maps one knob to daily/monthly/burst caps, max concurrency/workers, and allowed model
        tiers, then upserts the budget policy for the scope. Returns the derived envelope."""
        intensity = max(0, min(100, int(intensity)))
        f = intensity / 100.0
        daily = int(round(5 + f * 495) * 1_000_000)          # $5 → $500 / day
        monthly = daily * 20
        burst = int(round((0.5 + f * 9.5) * 1_000_000))      # $0.50 → $10 / min
        max_concurrent = 1 + round(f * 19)                   # 1 → 20
        max_workers = 1 + round(f * 29)                       # 1 → 30
        if intensity < 34:
            tiers = ["gpt-4o-mini"]                           # low-cost only
        elif intensity < 67:
            tiers = ["gpt-4o-mini", "gpt-4o"]
        else:
            tiers = ["gpt-4o-mini", "gpt-4o", "gpt-5.2-codex"]
        params = {
            "st": scope_type, "sid": scope_id, "daily": daily, "monthly": monthly,
            "burst": burst, "mc": max_concurrent, "mw": max_workers,
            "tiers": json.dumps(tiers), "intensity": intensity,
        }
        updated = connection.execute(
            text(
                """
                UPDATE budget_policies
                SET daily_cap_usd_micros = :daily, monthly_cap_usd_micros = :monthly,
                    burst_cap_usd_micros_per_min = :burst, max_concurrent_jobs = :mc,
                    max_workers_total = :mw, allowed_model_tiers = CAST(:tiers AS JSONB),
                    operational_intensity = :intensity, version = version + 1
                WHERE scope_type = :st AND scope_id = :sid
                """
            ),
            params,
        )
        if updated.rowcount == 0:
            connection.execute(
                text(
                    """
                    INSERT INTO budget_policies (
                        id, scope_type, scope_id, daily_cap_usd_micros, monthly_cap_usd_micros,
                        burst_cap_usd_micros_per_min, max_concurrent_jobs, max_workers_total,
                        allowed_model_tiers, operational_intensity)
                    VALUES (:id, :st, :sid, :daily, :monthly, :burst, :mc, :mw,
                            CAST(:tiers AS JSONB), :intensity)
                    """
                ),
                {**params, "id": make_id("bp")},
            )
        emit_event(connection, scope_id, "budget.intensity_set", actor, "budget_policy", scope_id,
                   {"intensity": intensity, "daily_cap_usd_micros": daily,
                    "max_concurrent_jobs": max_concurrent, "max_workers_total": max_workers,
                    "allowed_model_tiers": tiers})
        return {
            "operational_intensity": intensity,
            "daily_cap_usd_micros": daily,
            "monthly_cap_usd_micros": monthly,
            "burst_cap_usd_micros_per_min": burst,
            "max_concurrent_jobs": max_concurrent,
            "max_workers_total": max_workers,
            "allowed_model_tiers": tiers,
        }

    def list_allocations(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, parent_allocation_id, project_id, scope_type, scope_id,
                       amount_usd_micros, period_start, period_end, status, created_at
                FROM budget_allocations
                WHERE project_id = :project_id
                ORDER BY created_at DESC, id ASC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return [self._allocation_snapshot(connection, row) for row in rows]

    def create_allocation(
        self,
        connection: Connection,
        project_id: str,
        scope_type: str,
        scope_id: str,
        amount_usd_micros: int,
        period_start: Any,
        period_end: Any,
        actor: str,
        parent_allocation_id: Optional[str] = None,
    ) -> dict[str, Any]:
        if amount_usd_micros <= 0:
            raise ValueError("amount_usd_micros must be greater than zero.")
        if period_end <= period_start:
            raise ValueError("period_end must be after period_start.")

        if parent_allocation_id:
            parent = self._get_allocation(
                connection, project_id, parent_allocation_id, for_update=True
            )
            parent_snapshot = self._allocation_snapshot(connection, parent)
            if amount_usd_micros > parent_snapshot["available_usd_micros"]:
                emit_event(
                    connection,
                    project_id,
                    "budget.allocation_denied",
                    actor,
                    "budget_allocation",
                    parent_allocation_id,
                    {
                        "requested_usd_micros": amount_usd_micros,
                        "available_usd_micros": parent_snapshot["available_usd_micros"],
                        "reason": "child_allocation_exceeds_parent_available",
                    },
                )
                raise ValueError("Child allocation exceeds parent available budget.")
        else:
            policy = self.status_for_project(connection, project_id)
            if (
                policy["cap_usd_micros"]
                and amount_usd_micros > policy["cap_usd_micros"]
            ):
                emit_event(
                    connection,
                    project_id,
                    "budget.allocation_denied",
                    actor,
                    "project",
                    project_id,
                    {
                        "requested_usd_micros": amount_usd_micros,
                        "cap_usd_micros": policy["cap_usd_micros"],
                        "reason": "root_allocation_exceeds_project_cap",
                    },
                )
                raise ValueError("Root allocation exceeds project budget cap.")

        allocation_id = make_id("bua")
        connection.execute(
            text(
                """
                INSERT INTO budget_allocations (
                    id, parent_allocation_id, project_id, scope_type, scope_id,
                    amount_usd_micros, period_start, period_end, status
                )
                VALUES (
                    :id, :parent_allocation_id, :project_id, :scope_type, :scope_id,
                    :amount_usd_micros, :period_start, :period_end, 'active'
                )
                """
            ),
            {
                "id": allocation_id,
                "parent_allocation_id": parent_allocation_id,
                "project_id": project_id,
                "scope_type": scope_type,
                "scope_id": scope_id,
                "amount_usd_micros": amount_usd_micros,
                "period_start": period_start,
                "period_end": period_end,
            },
        )
        emit_event(
            connection,
            project_id,
            "budget.allocation_created",
            actor,
            "budget_allocation",
            allocation_id,
            {
                "parent_allocation_id": parent_allocation_id,
                "scope_type": scope_type,
                "scope_id": scope_id,
                "amount_usd_micros": amount_usd_micros,
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
            },
        )
        return self._allocation_snapshot(
            connection,
            self._get_allocation(connection, project_id, allocation_id),
        )

    def quote(
        self,
        connection: Connection,
        project_id: str,
        category: str,
        amount_usd_micros: int,
        reason: str,
    ) -> dict[str, Any]:
        snapshot = self.status_for_project(connection, project_id)
        total_after = (
            snapshot["reserved_usd_micros"] + snapshot["actual_usd_micros"] + amount_usd_micros
        )
        allowed = snapshot["cap_usd_micros"] == 0 or total_after <= snapshot["cap_usd_micros"]
        post_available = (
            max(snapshot["cap_usd_micros"] - total_after, 0)
            if snapshot["cap_usd_micros"]
            else 0
        )
        post_state = self._state_for_totals(
            snapshot["cap_usd_micros"],
            total_after,
            snapshot["soft_cap_pct"],
            snapshot["auto_lite_mode_pct"],
            snapshot["freeze_provisioning_pct"],
        )
        return {
            "allowed": allowed,
            "category": category,
            "reason": reason,
            "requested_usd_micros": amount_usd_micros,
            "budget_state": snapshot["budget_state"],
            "post_reservation_budget_state": post_state,
            "cap_usd_micros": snapshot["cap_usd_micros"],
            "available_usd_micros": snapshot["available_usd_micros"],
            "post_reservation_available_usd_micros": post_available,
        }

    def _burst_cap(self, connection: Connection, project_id: str) -> int:
        row = connection.execute(
            text(
                "SELECT burst_cap_usd_micros_per_min FROM budget_policies "
                "WHERE scope_type = 'project' AND scope_id = :p ORDER BY created_at DESC LIMIT 1"
            ),
            {"p": project_id},
        ).mappings().first()
        return int(row["burst_cap_usd_micros_per_min"]) if row else 0

    def burst_window(self, connection: Connection, project_id: str,
                     burst_cap: int) -> dict[str, Any]:
        """The actual spend settled in the trailing minute, and in how many seconds enough of
        it ages out for the window to be under `burst_cap` again."""
        rows = connection.execute(
            text(
                f"SELECT {MONEY_SQL} AS amount, "
                "       EXTRACT(EPOCH FROM (created_at + make_interval(secs => :w) "
                "                           - clock_timestamp())) AS ages_out_in "
                "FROM cost_receipts "
                "WHERE project_id = :p "
                "  AND created_at >= clock_timestamp() - make_interval(secs => :w) "
                "ORDER BY created_at"
            ),
            {"p": project_id, "w": BURST_WINDOW_SECONDS},
        ).mappings().all()
        spent = sum(int(row["amount"] or 0) for row in rows)
        remaining, frees_in = spent, 0.0
        for row in rows:
            if remaining < burst_cap:
                break
            remaining -= int(row["amount"] or 0)
            frees_in = float(row["ages_out_in"] or 0)
        return {"spent_usd_micros": spent, "frees_in": frees_in}

    def _burst_gate(self, connection: Connection, project_id: str, burst_cap: int,
                    amount_usd_micros: int, category: str, key: str,
                    job_id: Optional[str] = None) -> Optional[dict]:
        """None when the request may pass the per-minute cap; otherwise its transient refusal.

        Waiting rows are touched only with SKIP LOCKED: a tick runs its steps (model calls,
        minutes) inside one transaction, and blocking on another request's row would stall
        every reservation on the project behind it. A refusal rolled back by its caller simply
        leaves no row — that request loses its place in line, nothing else."""
        window = self.burst_window(connection, project_id, burst_cap)
        spent = window["spent_usd_micros"]
        live = "retry_at > clock_timestamp() - make_interval(secs => :grace)"
        own = "(waiter_key = :k OR COALESCE(job_id = CAST(:j AS text), false))"
        params = {"p": project_id, "k": key, "j": job_id, "grace": BURST_WAITER_GRACE_SECONDS}
        mine = connection.execute(
            text(f"SELECT MIN(first_refused_at) FROM budget_burst_waiters "
                 f"WHERE project_id = :p AND {own} AND {live}"),
            params).scalar()
        ahead = connection.execute(
            text(f"SELECT EXTRACT(EPOCH FROM (retry_at - clock_timestamp())) AS back_in "
                 f"FROM budget_burst_waiters "
                 f"WHERE project_id = :p AND NOT {own} AND {live} "
                 f"  AND first_refused_at < COALESCE(CAST(:mine AS timestamptz), "
                 f"                                  clock_timestamp()) "
                 f"ORDER BY first_refused_at LIMIT 1"),
            {**params, "mine": mine}).mappings().first()

        if spent < burst_cap and ahead is None:
            # Granted: this request no longer waits.
            connection.execute(
                text(f"DELETE FROM budget_burst_waiters WHERE id IN ("
                     f"  SELECT id FROM budget_burst_waiters "
                     f"  WHERE project_id = :p AND {own} FOR UPDATE SKIP LOCKED)"),
                params)
            return None

        over = spent >= burst_cap
        retry_after = burst_retry_after_seconds(
            window["frees_in"] if over else (ahead or {}).get("back_in"))
        self._record_burst_waiter(connection, project_id, key, job_id, retry_after)
        record_policy_eval(
            connection, project_id, "budget.reserve", "deny", action=category,
            reason=("burst cap exceeded ($/min)" if over
                    else "burst cap: an earlier request is waiting"),
            detail={"burst_cap_usd_micros_per_min": burst_cap,
                    "actual_60s_usd_micros": spent,
                    "requested_usd_micros": amount_usd_micros,
                    "queued_behind_earlier_request": not over,
                    "retry_after_seconds": retry_after})
        return {
            "status": "denied",
            "refusal": "burst",
            "transient": True,
            "retry_after_seconds": retry_after,
            "decision_id": None,
            "message": BURST_MESSAGE if over else BURST_QUEUED_MESSAGE,
        }

    def _record_burst_waiter(self, connection: Connection, project_id: str, key: str,
                             job_id: Optional[str], retry_after: int) -> None:
        own = "(waiter_key = :k OR COALESCE(job_id = CAST(:j AS text), false))"
        params = {"p": project_id, "k": key, "j": job_id, "ra": int(retry_after)}
        refreshed = connection.execute(
            text(f"UPDATE budget_burst_waiters "
                 f"SET retry_at = clock_timestamp() + make_interval(secs => :ra) "
                 f"WHERE id IN (SELECT id FROM budget_burst_waiters "
                 f"             WHERE project_id = :p AND {own} "
                 f"             FOR UPDATE SKIP LOCKED)"),
            params).rowcount
        if not refreshed and not connection.execute(
                text(f"SELECT 1 FROM budget_burst_waiters "
                     f"WHERE project_id = :p AND {own} LIMIT 1"), params).first():
            connection.execute(
                text("INSERT INTO budget_burst_waiters "
                     "(id, project_id, waiter_key, job_id, first_refused_at, retry_at) "
                     "VALUES (:id, :p, :k, :j, clock_timestamp(), "
                     "        clock_timestamp() + make_interval(secs => :ra))"),
                {**params, "id": make_id("bwait")})
        # Rows whose holder never came back.
        connection.execute(
            text("DELETE FROM budget_burst_waiters WHERE id IN ("
                 "  SELECT id FROM budget_burst_waiters WHERE project_id = :p "
                 "  AND retry_at < clock_timestamp() - interval '10 minutes' "
                 "  FOR UPDATE SKIP LOCKED)"),
            {"p": project_id})

    def reserve(
        self,
        connection: Connection,
        project_id: str,
        category: str,
        amount_usd_micros: int,
        reason: str,
        expires_in_minutes: int,
        job_id: Optional[str] = None,
        allocation_id: Optional[str] = None,
        risk_tier: Optional[int] = None,
    ) -> dict:
        self.expire_stale_reservations(connection, project_id)
        snapshot = self.status_for_project(connection, project_id)

        # BOSS-280: freeze_tier2_pct, actually enforced. Past the threshold, HIGH-RISK work
        # (tier >= 2) is refused even while cheaper/lower-risk work still fits under the cap —
        # the last budget headroom is preserved for finishing safe work, not starting risky work.
        if snapshot.get("tier2_frozen") and (risk_tier or 0) >= 2:
            decision_id = create_blocking_decision(
                connection, project_id, None,
                "Tier-2 work frozen by budget policy",
                {"action": "raise_cap_or_wait_for_headroom"},
                {"requested_usd_micros": amount_usd_micros, "risk_tier": risk_tier,
                 "utilization_ratio": snapshot.get("utilization_ratio"),
                 "freeze_tier2_pct": snapshot.get("freeze_tier2_pct")},
                "budget_tier2_frozen",
            )
            record_policy_eval(
                connection, project_id, "budget.reserve", "deny", action=category,
                reason="tier2 frozen (utilization past freeze_tier2_pct)",
                detail={"risk_tier": risk_tier, "decision_id": decision_id,
                        "utilization_ratio": snapshot.get("utilization_ratio")})
            return {
                "status": "denied",
                "refusal": "tier2_frozen",
                "transient": False,
                "decision_id": decision_id,
                "budget_state": snapshot["budget_state"],
                "message": "Tier-2+ work is frozen at this budget utilization. Decision created.",
            }
        total_after = (
            snapshot["reserved_usd_micros"] + snapshot["actual_usd_micros"] + amount_usd_micros
        )

        if snapshot["cap_usd_micros"] and total_after > snapshot["cap_usd_micros"]:
            # OTHER WORK'S RESERVATIONS ARE A MOMENT, NOT A CAP. `reserved` is what active
            # reservations hold right now — a governed code task alone holds $2 while it runs —
            # and it is consumed or released when that work ends. Five runs started together
            # can fill the cap with holds while the money actually spent is far under it ($7.03
            # of $10 free in the incident). Refusing that as `cap_exceeded` opened a blocking
            # decision and told a person to "raise the daily or monthly cap" for something that
            # clears by itself. Only a request the cap refuses on ACTUAL spend needs a person.
            if (snapshot["reserved_usd_micros"] > 0
                    and snapshot["actual_usd_micros"] + amount_usd_micros
                    <= snapshot["cap_usd_micros"]):
                record_policy_eval(
                    connection, project_id, "budget.reserve", "deny", action=category,
                    reason="budget headroom held by active reservations",
                    detail={"requested_usd_micros": amount_usd_micros,
                            "reserved_usd_micros": snapshot["reserved_usd_micros"],
                            "actual_usd_micros": snapshot["actual_usd_micros"],
                            "cap_usd_micros": snapshot["cap_usd_micros"],
                            "retry_after_seconds": HEADROOM_RETRY_AFTER_SECONDS})
                return {
                    "status": "denied",
                    "refusal": "headroom",
                    "transient": True,
                    "retry_after_seconds": HEADROOM_RETRY_AFTER_SECONDS,
                    "decision_id": None,
                    "budget_state": snapshot["budget_state"],
                    "message": HEADROOM_MESSAGE,
                }
            decision_id = create_blocking_decision(
                connection,
                project_id,
                None,
                "Budget reservation denied",
                {"action": "increase_budget_or_defer"},
                {
                    "requested_usd_micros": amount_usd_micros,
                    "available_usd_micros": max(
                        snapshot["cap_usd_micros"]
                        - snapshot["reserved_usd_micros"]
                        - snapshot["actual_usd_micros"],
                        0,
                    ),
                },
                "budget_denied",
            )
            emit_event(
                connection,
                project_id,
                "budget.reservation_denied",
                "budget_governor",
                "decision",
                decision_id,
                {"requested_usd_micros": amount_usd_micros, "reason": reason},
            )
            record_policy_eval(
                connection, project_id, "budget.reserve", "deny",
                action=category, reason="budget cap exceeded",
                detail={"requested_usd_micros": amount_usd_micros,
                        "budget_state": snapshot["budget_state"], "decision_id": decision_id})
            return {
                "status": "denied",
                "refusal": "cap_exceeded",
                "transient": False,
                "decision_id": decision_id,
                "budget_state": snapshot["budget_state"],
                "message": "Budget cap exceeded. Decision created.",
            }

        # Burst cap: limit $/min to absorb sudden spikes (spec §22.4 cost-anomaly controls).
        #
        # IT MEASURES MONEY ACTUALLY SPENT, NOT ESTIMATES. The cap sums the cost receipts
        # settled in the trailing minute (their actual cost, or the estimate where no actual
        # is known). It used to sum the last minute's RESERVATIONS: an estimate is a ceiling,
        # a research stage reserves $2 against a $0.10/min cap, and with subscriptions whose
        # real cost is $0 the whole project got about one model call a minute while every
        # other run sat "waiting for the per-minute budget" for 11+ minutes (boss-1, five
        # bundled examples). Reservations still count in full against the daily, monthly and
        # allocation caps above and below — governance of total spend is unchanged. A call
        # that costs $0 (a subscription, a local model) never fills this window.
        #
        # A REFUSAL HERE CLEARS BY ITSELF, so it says so: `transient` and `retry_after_seconds`
        # tell the caller to wait and try again rather than fail or ask a person.
        #
        # AND IT IS SERVED IN ORDER. A refused request is recorded as waiting; once the window
        # has room, a request is granted only if no request that has waited longer is still
        # live. Busy runs therefore cannot keep a patient one out for ever.
        burst_cap = self._burst_cap(connection, project_id)
        if burst_cap:
            refused = self._burst_gate(
                connection, project_id, burst_cap, amount_usd_micros, category,
                burst_waiter_key(category, reason), job_id)
            if refused is not None:
                refused["budget_state"] = snapshot["budget_state"]
                return refused

        if allocation_id:
            allocation = self._get_allocation(
                connection, project_id, allocation_id, for_update=True
            )
            allocation_snapshot = self._allocation_snapshot(connection, allocation)
            if amount_usd_micros > allocation_snapshot["available_usd_micros"]:
                decision_id = create_blocking_decision(
                    connection,
                    project_id,
                    None,
                    "Budget allocation reservation denied",
                    {"action": "increase_allocation_or_defer"},
                    {
                        "allocation_id": allocation_id,
                        "requested_usd_micros": amount_usd_micros,
                        "available_usd_micros": allocation_snapshot[
                            "available_usd_micros"
                        ],
                    },
                    "budget_denied",
                )
                emit_event(
                    connection,
                    project_id,
                    "budget.allocation_reservation_denied",
                    "budget_governor",
                    "decision",
                    decision_id,
                    {
                        "allocation_id": allocation_id,
                        "requested_usd_micros": amount_usd_micros,
                        "reason": reason,
                    },
                )
                return {
                    "status": "denied",
                    "refusal": "allocation_exceeded",
                    "transient": False,
                    "decision_id": decision_id,
                    "budget_state": snapshot["budget_state"],
                    "message": "Budget allocation exceeded. Decision created.",
                }

        reservation_id = make_id("res")
        connection.execute(
            text(
                """
                INSERT INTO budget_reservations (
                    id, allocation_id, project_id, job_id, category, amount_usd_micros,
                    status, reason, expires_at, created_at
                )
                VALUES (
                    :id, :allocation_id, :project_id, :job_id, :category, :amount_usd_micros,
                    'active', :reason, :expires_at, clock_timestamp()
                )
                """
            ),
            {
                "id": reservation_id,
                "allocation_id": allocation_id,
                "project_id": project_id,
                "job_id": job_id,
                "category": category,
                "amount_usd_micros": amount_usd_micros,
                "reason": reason,
                "expires_at": expires_at(expires_in_minutes),
            },
        )
        emit_event(
            connection,
            project_id,
            "budget.reservation_created",
            "budget_governor",
            "budget_reservation",
            reservation_id,
            {
                "category": category,
                "amount_usd_micros": amount_usd_micros,
                "job_id": job_id,
                "allocation_id": allocation_id,
            },
        )
        record_policy_eval(
            connection, project_id, "budget.reserve", "allow",
            action=category, reason="within budget",
            detail={"amount_usd_micros": amount_usd_micros, "reservation_id": reservation_id})
        fresh = self.status_for_project(connection, project_id)
        return {
            "status": "reserved",
            "reservation_id": reservation_id,
            "decision_id": None,
            "budget_state": fresh["budget_state"],
            "message": "Reservation created.",
        }

    def _get_allocation(
        self,
        connection: Connection,
        project_id: str,
        allocation_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any]:
        lock_clause = "FOR UPDATE" if for_update else ""
        row = connection.execute(
            text(
                f"""
                SELECT id, parent_allocation_id, project_id, scope_type, scope_id,
                       amount_usd_micros, period_start, period_end, status, created_at
                FROM budget_allocations
                WHERE id = :allocation_id AND project_id = :project_id
                {lock_clause}
                """
            ),
            {"allocation_id": allocation_id, "project_id": project_id},
        ).mappings().first()
        if not row or row["status"] != "active":
            raise ValueError("Active budget allocation not found.")
        return dict(row)

    def _allocation_snapshot(
        self, connection: Connection, row: Any
    ) -> dict[str, Any]:
        allocation_id = row["id"]
        child_total = int(
            connection.execute(
                text(
                    """
                    SELECT COALESCE(SUM(amount_usd_micros), 0)
                    FROM budget_allocations
                    WHERE parent_allocation_id = :allocation_id
                      AND status = 'active'
                    """
                ),
                {"allocation_id": allocation_id},
            ).scalar_one()
        )
        reserved = int(
            connection.execute(
                text(
                    """
                    SELECT COALESCE(SUM(amount_usd_micros), 0)
                    FROM budget_reservations
                    WHERE allocation_id = :allocation_id
                      AND status = 'active'
                    """
                ),
                {"allocation_id": allocation_id},
            ).scalar_one()
        )
        actual = int(
            connection.execute(
                text(
                    """
                    SELECT COALESCE(SUM(CASE WHEN COALESCE(cr.included_in_subscription, false)
                                             THEN 0
                                             ELSE COALESCE(cr.actual_usd_micros,
                                                           cr.estimated_usd_micros) END), 0)
                    FROM cost_receipts cr
                    JOIN budget_reservations br ON br.id = cr.reservation_id
                    WHERE br.allocation_id = :allocation_id
                    """
                ),
                {"allocation_id": allocation_id},
            ).scalar_one()
        )
        amount = int(row["amount_usd_micros"])
        committed = child_total + reserved + actual
        return {
            "id": allocation_id,
            "parent_allocation_id": row["parent_allocation_id"],
            "project_id": row["project_id"],
            "scope_type": row["scope_type"],
            "scope_id": row["scope_id"],
            "amount_usd_micros": amount,
            "child_allocated_usd_micros": child_total,
            "reserved_usd_micros": reserved,
            "actual_usd_micros": actual,
            "available_usd_micros": max(amount - committed, 0),
            "period_start": row["period_start"].isoformat(),
            "period_end": row["period_end"].isoformat(),
            "status": row["status"],
            "created_at": row["created_at"].isoformat(),
        }

    def release(
        self,
        connection: Connection,
        project_id: str,
        reservation_id: str,
        reason: str,
    ) -> dict[str, Any]:
        self.expire_stale_reservations(connection, project_id)
        row = connection.execute(
            text(
                """
                SELECT id, status, category, amount_usd_micros
                FROM budget_reservations
                WHERE id = :reservation_id AND project_id = :project_id
                """
            ),
            {"reservation_id": reservation_id, "project_id": project_id},
        ).mappings().first()
        if not row:
            return {
                "status": "not_found",
                "reservation_id": reservation_id,
                "budget_state": self.status_for_project(connection, project_id)["budget_state"],
                "message": "Reservation not found.",
            }
        if row["status"] != "active":
            return {
                "status": row["status"],
                "reservation_id": reservation_id,
                "budget_state": self.status_for_project(connection, project_id)["budget_state"],
                "message": f"Reservation is already {row['status']}.",
            }

        connection.execute(
            text(
                """
                UPDATE budget_reservations
                SET status = 'released'
                WHERE id = :reservation_id
                """
            ),
            {"reservation_id": reservation_id},
        )
        emit_event(
            connection,
            project_id,
            "budget.reservation_released",
            "budget_governor",
            "budget_reservation",
            reservation_id,
            {
                "reason": reason,
                "category": row["category"],
                "amount_usd_micros": int(row["amount_usd_micros"]),
            },
        )
        snapshot = self.status_for_project(connection, project_id)
        return {
            "status": "released",
            "reservation_id": reservation_id,
            "budget_state": snapshot["budget_state"],
            "message": reason,
        }

    def consume(
        self,
        connection: Connection,
        project_id: str,
        reservation_id: str,
        receipt_id: str,
        category: str,
        skill_or_model_id: str,
        estimated_usd_micros: int,
        actual_usd_micros: Optional[int],
        actor: str,
        *,
        included_in_subscription: bool = False,
    ) -> dict[str, Any]:
        """Settle `reservation_id` with a call's cost. `included_in_subscription` (a subscription,
        licensed seat or local model answered — included_spend.py): `actual_usd_micros` is the
        call's API-equivalent value, recorded as such, and the money settled is $0."""
        if estimated_usd_micros < 0:
            raise ValueError("estimated_usd_micros must be non-negative.")
        if actual_usd_micros is not None and actual_usd_micros < 0:
            raise ValueError("actual_usd_micros must be non-negative.")
        reservation = self.assert_active_reservation(connection, project_id, reservation_id)
        if not reservation:
            raise ValueError("Active budget reservation not found.")
        receipt = connection.execute(
            text(
                """
                SELECT id, status
                FROM receipts
                WHERE id = :receipt_id AND project_id = :project_id
                """
            ),
            {"receipt_id": receipt_id, "project_id": project_id},
        ).mappings().first()
        if not receipt:
            raise ValueError("Receipt not found for project.")
        cost_receipt_id = self.settle(
            connection,
            project_id,
            reservation_id,
            receipt_id,
            category,
            skill_or_model_id,
            estimated_usd_micros,
            actual_usd_micros,
            included_in_subscription=included_in_subscription,
        )
        actual_or_estimated = (
            0 if included_in_subscription
            else actual_usd_micros if actual_usd_micros is not None else estimated_usd_micros
        )
        reserved_amount = int(reservation["amount_usd_micros"])
        if actual_or_estimated > reserved_amount:
            emit_event(
                connection,
                project_id,
                "budget.reservation_overrun",
                actor,
                "budget_reservation",
                reservation_id,
                {
                    "receipt_id": receipt_id,
                    "cost_receipt_id": cost_receipt_id,
                    "reserved_usd_micros": reserved_amount,
                    "actual_or_estimated_usd_micros": actual_or_estimated,
                },
            )
        snapshot = self.status_for_project(connection, project_id)
        row = connection.execute(
            text(
                """
                SELECT id, project_id, reservation_id, receipt_id, category,
                       skill_or_model_id, estimated_usd_micros, actual_usd_micros,
                       equivalent_usd_micros, included_in_subscription,
                       status, created_at
                FROM cost_receipts
                WHERE id = :cost_receipt_id
                """
            ),
            {"cost_receipt_id": cost_receipt_id},
        ).mappings().one()
        return {
            "status": "consumed",
            "reservation_id": reservation_id,
            "budget_state": snapshot["budget_state"],
            "message": "Reservation consumed and cost receipt recorded.",
            "cost_receipt": dict(row),
        }

    def forecast(
        self,
        connection: Connection,
        project_id: str,
        horizon_hours: int = 24,
    ) -> dict[str, Any]:
        if horizon_hours < 1 or horizon_hours > 24 * 366:
            raise ValueError("horizon_hours must be between 1 and 8784.")
        snapshot = self.status_for_project(connection, project_id)
        forecasted = (
            snapshot["burn_rate_usd_micros_per_hour"] * horizon_hours
            + snapshot["reserved_usd_micros"]
        )
        return {
            "scope_type": "project",
            "scope_id": project_id,
            "horizon_hours": horizon_hours,
            "budget_state": snapshot["budget_state"],
            "cap_usd_micros": snapshot["cap_usd_micros"],
            "reserved_usd_micros": snapshot["reserved_usd_micros"],
            "actual_usd_micros": snapshot["actual_usd_micros"],
            "available_usd_micros": snapshot["available_usd_micros"],
            "committed_usd_micros": snapshot["committed_usd_micros"],
            "burn_rate_usd_micros_per_hour": snapshot[
                "burn_rate_usd_micros_per_hour"
            ],
            "forecasted_usd_micros": forecasted,
            "time_to_cap_seconds": snapshot["time_to_cap_seconds"],
            "top_cost_drivers": snapshot["top_cost_drivers"],
        }

    def attach_job(
        self,
        connection: Connection,
        project_id: str,
        reservation_id: str,
        job_id: str,
    ) -> None:
        connection.execute(
            text(
                """
                UPDATE budget_reservations
                SET job_id = :job_id
                WHERE id = :reservation_id AND project_id = :project_id
                """
            ),
            {
                "job_id": job_id,
                "reservation_id": reservation_id,
                "project_id": project_id,
            },
        )
        emit_event(
            connection,
            project_id,
            "budget.reservation_attached",
            "budget_governor",
            "budget_reservation",
            reservation_id,
            {"job_id": job_id},
        )

    def assert_active_reservation(
        self, connection: Connection, project_id: str, reservation_id: str
    ) -> Optional[dict]:
        self.expire_stale_reservations(connection, project_id)
        row = connection.execute(
            text(
                """
                SELECT id, amount_usd_micros, status, expires_at
                FROM budget_reservations
                WHERE id = :reservation_id AND project_id = :project_id
                """
            ),
            {"reservation_id": reservation_id, "project_id": project_id},
        ).mappings().first()

        if not row or row["status"] != "active":
            return None
        return dict(row)

    def settle(
        self,
        connection: Connection,
        project_id: str,
        reservation_id: str,
        receipt_id: str,
        category: str,
        skill_or_model_id: str,
        estimated_usd_micros: int,
        actual_usd_micros: Optional[int],
        *,
        included_in_subscription: bool = False,
    ) -> str:
        """Record a cost receipt and consume the reservation.

        MONEY ONLY IN THE MONEY COLUMN. With `included_in_subscription` the call ran on flat-rate
        or local capacity: `actual_usd_micros` (what the provider or the price table says it
        would cost at list price) is kept as `equivalent_usd_micros`, and the money settled is
        $0 — the daily, monthly, allocation and burst caps never see it (included_spend.py)."""
        equivalent: Optional[int] = None
        if included_in_subscription:
            equivalent = int(actual_usd_micros if actual_usd_micros is not None
                             else estimated_usd_micros)
            actual_usd_micros = 0
        cost_receipt_id = make_id("cost")
        connection.execute(
            text(
                """
                INSERT INTO cost_receipts (
                    id, project_id, reservation_id, receipt_id, category, skill_or_model_id,
                    estimated_usd_micros, actual_usd_micros, status,
                    equivalent_usd_micros, included_in_subscription
                )
                VALUES (
                    :id, :project_id, :reservation_id, :receipt_id, :category, :skill_or_model_id,
                    :estimated_usd_micros, :actual_usd_micros, :status,
                    :equivalent_usd_micros, :included
                )
                """
            ),
            {
                "id": cost_receipt_id,
                "project_id": project_id,
                "reservation_id": reservation_id,
                "receipt_id": receipt_id,
                "category": category,
                "skill_or_model_id": skill_or_model_id,
                "estimated_usd_micros": estimated_usd_micros,
                "actual_usd_micros": actual_usd_micros,
                "status": "actual" if actual_usd_micros is not None else "estimated",
                "equivalent_usd_micros": equivalent,
                "included": bool(included_in_subscription),
            },
        )
        connection.execute(
            text(
                """
                UPDATE budget_reservations
                SET status = 'consumed'
                WHERE id = :reservation_id
                """
            ),
            {"reservation_id": reservation_id},
        )
        emit_event(
            connection,
            project_id,
            "budget.reservation_consumed",
            "budget_governor",
            "budget_reservation",
            reservation_id,
            {"receipt_id": receipt_id, "category": category},
        )
        return cost_receipt_id

    def _state_for_totals(
        self,
        cap_usd_micros: int,
        committed_usd_micros: int,
        soft_cap_pct: float,
        auto_lite_mode_pct: float,
        freeze_provisioning_pct: float,
    ) -> str:
        if cap_usd_micros == 0:
            return "unknown"
        utilization = committed_usd_micros / cap_usd_micros
        if utilization >= freeze_provisioning_pct:
            return "red"
        if utilization >= auto_lite_mode_pct:
            return "orange"
        if utilization >= soft_cap_pct:
            return "yellow"
        return "green"
