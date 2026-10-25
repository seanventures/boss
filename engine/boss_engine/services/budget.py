from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .included_spend import INCLUDED_SQL, MONEY_SQL
from .model_failures import cap_refusal_sentence, cap_used_sentence, resets_sentence
from .core import (
    create_blocking_decision,
    emit_event,
    expires_at,
    make_id,
    now_utc,
    record_policy_eval,
)


# ── EVERY CAP MEASURES ITS OWN PERIOD ─────────────────────────────────────────────────────────
#
# `daily_cap_usd_micros` is a cap on a DAY's money and `monthly_cap_usd_micros` on a MONTH's
# (budget_v0.yaml, the Budget screen's "daily cap" / "monthly cap", the intensity slider's
# "$5 → $500 / day"). Status used to compare the daily cap with every cost receipt the project
# ever had: once an install reached its cap it stayed red for ever — waiting never helped, and
# red holds all non-P0 work, so every install eventually stopped working. The monthly cap was
# shown and never enforced at all.
#
# Now each cap is compared with the money spent and held in ITS period, and the period with the
# highest utilization is the one the budget state, `available`, the job gate, readiness and the
# hold messages speak for. A project has no timezone of its own, so a period's boundary is
# midnight UTC — said wherever a hold is ("It resets at 00:00 UTC").

#: The category a build's reservation and its settlement carry (build_budget.CATEGORY).
BUILD_CATEGORY = "code_task"

#: The timezone budget periods turn over in. Projects carry no timezone of their own.
BUDGET_TIMEZONE = "UTC"

#: (cap column, period) in the order a tie is reported: the day first.
CAP_PERIODS = (("daily_cap_usd_micros", "day"), ("monthly_cap_usd_micros", "month"))

#: How each period is said: (whose budget, the cap's name). "burst" is the per-minute
#: concurrency refusal (connection_spend.py's `refusal: "burst"`) — not a cap period with its
#: own row in `periods`, but a model's "Paused · budget" pill can still bind on it, so it has
#: words too (setup_pills.MODEL["paused_budget"]).
PERIOD_WORDS = {"day": ("today's", "daily cap"), "month": ("this month's", "monthly cap"),
                "burst": ("this burst", "burst cap")}


def budget_period(period: str, now: Optional[datetime] = None) -> dict[str, Any]:
    """The period `now` falls in: its UTC start, its end (when it resets) and the reset said in
    words. The one definition of a budget period's boundary."""
    now = (now or now_utc()).astimezone(timezone.utc)
    if period == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        label = f"00:00 {BUDGET_TIMEZONE}"
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = (start + timedelta(days=32)).replace(day=1)
        label = f"00:00 {BUDGET_TIMEZONE} on {end.day} {end:%B}"
    else:
        raise ValueError(f"Unknown budget period {period!r}.")
    whose, cap_name = PERIOD_WORDS[period]
    return {"period": period, "start": start, "end": end, "resets_at_label": label,
            "whose": whose, "cap_name": cap_name}


def period_spend(connection: Connection, project_id: str, period: str,
                 now: Optional[datetime] = None) -> dict[str, Any]:
    """THE ONE MEASURE OF A PERIOD'S SPEND: the money settled in it (MONEY_SQL — a subscription
    or local call is $0, included_spend.py) plus ALL outstanding reservations. An unresolved
    liability does not disappear at midnight: settlement replaces its hold with period spend.
    Every cap, state, forecast and message reads this."""
    bounds = budget_period(period, now)
    params = {"p": project_id, "start": bounds["start"], "end": bounds["end"]}
    # Both aggregates share one MVCC snapshot. Separate statements can miss a hold
    # consumed between the actual-cost read and the held-cost read.
    totals = connection.execute(
        text(f"SELECT actual.total AS actual, held.total AS held, held.n FROM "
             f"(SELECT COALESCE(SUM({MONEY_SQL}), 0) AS total FROM cost_receipts "
             "WHERE project_id = :p AND created_at >= :start AND created_at < :end) actual "
             "CROSS JOIN (SELECT COALESCE(SUM(amount_usd_micros), 0) AS total, COUNT(*) AS n "
             "FROM budget_reservations WHERE project_id = :p AND status = 'active') held"),
        params).mappings().one()
    return {**bounds, "actual_usd_micros": int(totals["actual"] or 0),
            "reserved_usd_micros": int(totals["held"] or 0),
            "active_reservations_count": int(totals["n"] or 0)}


def state_for_utilization(cap: int, utilization: float, soft: float, orange: float,
                          red: float) -> str:
    if cap == 0:
        return "unknown"
    if utilization >= red:
        return "red"
    if utilization >= orange:
        return "orange"
    if utilization >= soft:
        return "yellow"
    return "green"


def release_budget_holds(connection: Connection, project_id: str) -> int:
    """A CAP CHANGED: every job the budget was holding runs at its next poll, not up to 15
    minutes later (factory test D3). A job still over the new cap is simply held again by the
    same policy check; one that fits now goes. Returns how many were released."""
    released = connection.execute(
        text("UPDATE jobs SET run_after = now(), updated_at = now() "
             "WHERE project_id = :p AND status = 'queued' "
             "AND last_error->>'kind' = 'budget_hold' AND run_after > now()"),
        {"p": project_id},
    )
    return int(getattr(released, "rowcount", 0) or 0)


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
ADMISSION_BUSY_MESSAGE = "Budget admission is busy with another transaction. Retry shortly."
#: How a busy admission is retried before it is answered "busy": a few short pauses, never a
#: blocking wait (a caller may hold its transaction across work, and a wait could queue behind
#: provider I/O or form a lock-order cycle). Two scans started a second apart both stopped
#: "budget refused" on the first collision (brec_6025901347b6445eb9c1).
ADMISSION_RETRY_PAUSES = (0.2, 0.4, 0.8)


def _try_admit(connection: Connection, project_id: str) -> bool:
    """The project's budget-admission lock (transaction-scoped), tried a few times."""
    import time

    for pause in (*ADMISSION_RETRY_PAUSES, None):
        if connection.execute(text(
                "SELECT pg_try_advisory_xact_lock(hashtextextended('boss-budget:' || :p, 0))"),
                {"p": project_id}).scalar_one():
            return True
        if pause is not None:
            time.sleep(pause)
    return False

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


def intensity_envelope(intensity: Any) -> dict[str, Any]:
    """What Pace (operational intensity, 0-100) writes: the ONE mapping, shared by
    `set_intensity` and Govern's preview, so the preview is what will be written (the web used
    to re-implement it with `Math.round` and previewed $253 / 16 workers for $252 / 15).

    `allowed_model_tiers` is written and read by nothing (Govern does not claim it)."""
    intensity = max(0, min(100, int(intensity)))
    f = intensity / 100.0
    daily = int(round(5 + f * 495) * 1_000_000)          # $5 → $500 / day
    if intensity < 34:
        tiers = ["gpt-4o-mini"]                           # low-cost only
    elif intensity < 67:
        tiers = ["gpt-4o-mini", "gpt-4o"]
    else:
        tiers = ["gpt-4o-mini", "gpt-4o", "gpt-5.2-codex"]
    return {
        "operational_intensity": intensity,
        "daily_cap_usd_micros": daily,
        "monthly_cap_usd_micros": daily * 20,
        "burst_cap_usd_micros_per_min": int(round((0.5 + f * 9.5) * 1_000_000)),  # $0.50 → $10
        "max_concurrent_jobs": 1 + round(f * 19),                                # 1 → 20
        "max_workers_total": 1 + round(f * 29),                                  # 1 → 30
        "allowed_model_tiers": tiers,
    }


#: Transient refusals (burst, headroom) are retried every few seconds; one `budget.refused`
#: record per waiting request per this window is evidence enough, not one per retry.
REFUSAL_RECORD_WINDOW_MINUTES = 10


def _write_refusal_record(connection: Connection, project_id: str, refusal: dict[str, Any],
                          data: dict[str, Any]) -> None:
    emit_event(connection, project_id, "budget.refused", "budget_governor",
               "decision" if refusal.get("decision_id") else "budget_policy",
               str(refusal.get("decision_id") or project_id), data)


def record_refusal(connection: Connection, project_id: str, refusal: dict[str, Any], *,
                   category: str, reason: str, amount_usd_micros: int,
                   snapshot: Optional[dict] = None, period: Optional[dict] = None) -> None:
    """`budget.refused`: what a limit stopped, so Govern can say `Stopped 2× · 7d` from a
    record and the Log's `Refusals` view can list it. Written NEXT TO the refusal and never
    instead of it: in a savepoint, with any failure swallowed, so the refusal returned to the
    caller is identical whether or not its record could be written."""
    kind = {"cap_exceeded": "cap"}.get(str(refusal.get("refusal")), str(refusal.get("refusal")))
    try:
        with connection.begin_nested():
            if refusal.get("transient"):
                recent = connection.execute(
                    text("SELECT 1 FROM events WHERE project_id = :p "
                         "AND event_type = 'budget.refused' "
                         "AND created_at > now() - make_interval(mins => :w) "
                         "AND data->>'kind' = :k AND data->>'waiter' = :who LIMIT 1"),
                    {"p": project_id, "w": REFUSAL_RECORD_WINDOW_MINUTES, "k": kind,
                     "who": burst_waiter_key(category, reason)}).first()
                if recent:
                    return
            where = period or snapshot or {}
            _write_refusal_record(connection, project_id, refusal, {
                "kind": kind,
                "period": (period or {}).get("period") or refusal.get("period"),
                "committed_usd_micros": where.get("committed_usd_micros"),
                "cap_usd_micros": where.get("cap_usd_micros"),
                "resets_at": where.get("resets_at"),
                "requested_usd_micros": int(amount_usd_micros),
                "decision_id": refusal.get("decision_id"),
                "category": category,
                "waiter": burst_waiter_key(category, reason),
            })
    except Exception:  # noqa: BLE001 — the record of a refusal is not the refusal
        import logging

        logging.getLogger(__name__).exception("could not record budget.refused")


def policy_version(connection: Connection, project_id: str) -> tuple[str, Optional[str]]:
    """(version, who changed the budget last) — the If-Match a Govern spend edit sends."""
    version = connection.execute(
        text("SELECT version FROM budget_policies WHERE scope_type = 'project' "
             "AND scope_id = :p ORDER BY created_at DESC LIMIT 1"), {"p": project_id}).scalar()
    actor = connection.execute(
        text("SELECT actor FROM events WHERE project_id = :p AND event_type IN "
             "('budget.intensity_set', 'budget.monthly_cap_set') "
             "ORDER BY created_at DESC LIMIT 1"), {"p": project_id}).scalar()
    return (str(version) if version is not None else "none"), actor


class BudgetGovernor:
    def __init__(self, settings: Settings):
        self.settings = settings

    def legacy_model_reconciliation(self, connection: Connection, project_id: str) -> Optional[dict]:
        """Fence identifiable discarded liability without reviving expired authority.

        Only one unresolved example is needed to stop paid admission. The original
        estimate is evidence, never an invoice or a newly active reservation.
        Decision writes participate in the caller's transaction; the row predicate
        remains authoritative even if a refusing caller rolls that transaction back.
        """
        query = text(
            "SELECT r.id AS reservation_id,r.status AS original_reservation_status,r.amount_usd_micros AS original_estimate_usd_micros,"
            "l.id AS lease_id,i.id AS invocation_id,i.receipt_id "
            "FROM budget_reservations r JOIN model_gateway_leases l ON l.budget_reservation_id=r.id AND l.project_id=r.project_id "
            "JOIN model_gateway_invocations i ON i.model_gateway_lease_id=l.id AND i.project_id=r.project_id "
            "JOIN receipts receipt ON receipt.id=i.receipt_id AND receipt.project_id=r.project_id "
            "WHERE r.project_id=:p AND r.status IN ('expired','released') AND r.amount_usd_micros>0 "
            # The old typed owner emits attempt_failed only after _call begins.
            # Do not infer post-contact exposure from arbitrary failed rows or reasons.
            "AND (i.status='in_progress' OR (i.status='failed' AND receipt.status='failed' "
            "AND receipt.dedupe_key='model-gateway:' || r.project_id || ':' || i.dedupe_key "
            "AND EXISTS (SELECT 1 FROM events e WHERE e.project_id=r.project_id "
            "AND e.event_type='typed_decision.attempt_failed' AND e.entity_type='typed_decision_attempt' "
            "AND e.entity_id=i.dedupe_key AND e.data->>'receipt_id'=i.receipt_id "
            # Only the new owner's exact structured proof, persisted atomically in
            # both canonical errors and this event, establishes a TypeSafe 401 refusal — OR a
            # row recorded before that structured proof existed, with the provider's own
            # 401/403 auth rejection as free text ("TypeSafe 401: …authentication_error…", the
            # legacy owner's only record of it). No spend happened either way: a provider's
            # auth rejection is non-acceptance whether it was written down in the new shape or
            # the old one (boss-1 rc10.9 — this exact row blocked every paid connect for good).
            "AND NOT (COALESCE(i.error->'typed_decision_nonacceptance'=jsonb_build_object("
            "'version',1,'project_id',r.project_id,'invocation_id',i.id,'receipt_id',i.receipt_id,"
            "'reservation_id',r.id,'kind','provider_auth_rejected','provider','typesafe','http_status',401) "
            "AND receipt.error->'typed_decision_nonacceptance'=i.error->'typed_decision_nonacceptance' "
            "AND e.data->'typed_decision_nonacceptance'=i.error->'typed_decision_nonacceptance',false) "
            "OR (COALESCE(i.error->>'reason', i.error->>'message', receipt.error->>'reason', "
            "receipt.error->>'message', '') ~* '40[13]' "
            "AND COALESCE(i.error->>'reason', i.error->>'message', receipt.error->>'reason', "
            "receipt.error->>'message', '') ~* 'auth'))))) "
            "AND NOT EXISTS (SELECT 1 FROM cost_receipts c "
            "WHERE c.project_id=r.project_id AND c.reservation_id=r.id AND c.receipt_id=i.receipt_id) LIMIT 1")
        row = connection.execute(query, {"p": project_id}).mappings().first()
        if not row:
            return None
        locked = _try_admit(connection, project_id)
        if not locked:
            return {"status": "denied", "refusal": "admission_busy", "transient": True,
                    "retry_after_seconds": 1, "decision_id": None, "budget_state": "unknown",
                    "message": ADMISSION_BUSY_MESSAGE, "legacy_exposure": dict(row)}
        # A settlement may have committed while the admission lock was acquired.
        row = connection.execute(query, {"p": project_id}).mappings().first()
        if not row:
            return None
        evidence = dict(row)
        decision = connection.execute(text(
            "SELECT id,status FROM decisions WHERE project_id=:p "
            "AND recommendation->>'action'='reconcile_legacy_model_exposure' "
            "AND recommendation->>'invocation_id'=:i LIMIT 1"),
            {"p": project_id, "i": row['invocation_id']}).mappings().first()
        if decision:
            decision_id = decision['id']
            if decision['status'] != 'pending':
                connection.execute(text("UPDATE decisions SET status='pending',resolution_note=NULL,updated_at=now() WHERE id=:id"),
                                   {"id": decision_id})
                emit_event(connection, project_id, 'budget.legacy_reconciliation_reopened', 'budget_governor',
                           'decision', decision_id, {"invocation_id": row['invocation_id']})
        else:
            decision_id = create_blocking_decision(
                connection, project_id, None, "Reconcile historical unresolved model exposure before paid work",
                {**evidence, "action": "reconcile_legacy_model_exposure"},
                {"original_estimate_usd_micros": int(row['original_estimate_usd_micros']), "actual_cost_known": False},
                "legacy_model_outcome_unknown")
            emit_event(connection, project_id, 'budget.legacy_reconciliation_required', 'budget_governor',
                       'budget_reservation', row['reservation_id'], {"decision_id": decision_id, **evidence})
        return {"status": "denied", "refusal": "legacy_reconciliation_required", "transient": False,
                "decision_id": decision_id, "budget_state": "unknown", "legacy_exposure": evidence,
                "message": "Paid work is held for historical unresolved model exposure. Reconcile the recorded attempt; raising a cap or resolving this decision alone does not authorize new spend."}

    def legacy_provider_reconciliation(self, connection: Connection, project_id: str) -> Optional[dict]:
        """Quarantine discarded provider exposure; a failed response is not no contact.

        Whole-ChangeSet settlement uses one cost receipt, not one per operation.
        Only the durable all-not-dispatched owner protocol proves no call. Keep
        the original money/authority states unchanged and return one example.
        """
        query = text("""
            SELECT r.id AS reservation_id, r.status AS original_reservation_status,
                   r.amount_usd_micros AS original_estimate_usd_micros,
                   cs.id AS changeset_id, op.id AS operation_id,
                   rr.id AS resource_receipt_id, rr.receipt_id
            FROM budget_reservations r
            JOIN changesets cs ON cs.reservation_id=r.id AND cs.project_id=r.project_id
            JOIN resource_receipts rr ON rr.changeset_id=cs.id AND rr.project_id=r.project_id
            JOIN changeset_operations op ON op.id=rr.operation_id
                AND op.changeset_id=cs.id AND op.project_id=r.project_id
            JOIN receipts receipt ON receipt.id=rr.receipt_id AND receipt.project_id=r.project_id
            WHERE r.project_id=:p AND r.status IN ('expired','released')
              AND r.amount_usd_micros>0
              AND NOT (op.action IN ('read','sync') AND rr.action IN ('read','sync'))
              AND NOT (
                cs.status='applied'
                AND EXISTS (
                    SELECT 1 FROM cost_receipts cost
                    JOIN resource_receipts paid_rr ON paid_rr.receipt_id=cost.receipt_id
                        AND paid_rr.project_id=cs.project_id AND paid_rr.changeset_id=cs.id
                    JOIN receipts paid_receipt ON paid_receipt.id=paid_rr.receipt_id
                        AND paid_receipt.project_id=cs.project_id
                    WHERE cost.project_id=cs.project_id AND cost.reservation_id=r.id
                      AND cost.category='provider'
                      AND cost.skill_or_model_id='resource_plane.changeset.apply'
                      AND paid_rr.status='succeeded' AND paid_receipt.status='succeeded')
                AND NOT EXISTS (
                    SELECT 1 FROM changeset_operations sibling_op
                    LEFT JOIN resource_receipts sibling ON sibling.operation_id=sibling_op.id
                        AND sibling.changeset_id=cs.id AND sibling.project_id=cs.project_id
                    LEFT JOIN receipts sibling_receipt ON sibling_receipt.id=sibling.receipt_id
                        AND sibling_receipt.project_id=cs.project_id
                    WHERE sibling_op.changeset_id=cs.id AND (
                        sibling_op.project_id<>cs.project_id OR sibling_op.status<>'succeeded'
                        OR sibling.id IS NULL OR sibling.status<>'succeeded'
                        OR sibling_receipt.id IS NULL OR sibling_receipt.status<>'succeeded'))
                AND NOT EXISTS (
                    SELECT 1 FROM resource_receipts extra WHERE extra.changeset_id=cs.id
                    AND NOT EXISTS (SELECT 1 FROM changeset_operations linked
                        WHERE linked.id=extra.operation_id AND linked.changeset_id=cs.id
                          AND linked.project_id=cs.project_id AND extra.project_id=cs.project_id)))
              AND NOT (
                cs.status='deadlettered'
                AND EXISTS (SELECT 1 FROM events e WHERE e.project_id=cs.project_id
                    AND e.entity_type='changeset' AND e.entity_id=cs.id
                    AND e.event_type='changeset.not_dispatched'
                    AND e.data->>'code'='provider_not_dispatched')
                AND NOT EXISTS (
                    SELECT 1 FROM changeset_operations sibling_op
                    LEFT JOIN resource_receipts sibling ON sibling.operation_id=sibling_op.id
                        AND sibling.changeset_id=cs.id AND sibling.project_id=cs.project_id
                    LEFT JOIN receipts sibling_receipt ON sibling_receipt.id=sibling.receipt_id
                        AND sibling_receipt.project_id=cs.project_id
                    WHERE sibling_op.changeset_id=cs.id AND (
                        sibling_op.project_id<>cs.project_id OR sibling_op.status<>'failed'
                        OR sibling.id IS NULL OR sibling.status<>'failed'
                        OR sibling_receipt.id IS NULL OR sibling_receipt.status<>'failed'
                        OR COALESCE(sibling.error->>'code','')<>'provider_not_dispatched'
                        OR COALESCE(sibling_receipt.error->>'code','')<>'provider_not_dispatched'))
                AND NOT EXISTS (
                    SELECT 1 FROM resource_receipts extra WHERE extra.changeset_id=cs.id
                    AND NOT EXISTS (SELECT 1 FROM changeset_operations linked
                        WHERE linked.id=extra.operation_id AND linked.changeset_id=cs.id
                          AND linked.project_id=cs.project_id AND extra.project_id=cs.project_id)))
            LIMIT 1
        """)
        row = connection.execute(query, {"p": project_id}).mappings().first()
        if not row:
            return None
        locked = _try_admit(connection, project_id)
        if not locked:
            return {"status": "denied", "refusal": "admission_busy", "transient": True,
                    "retry_after_seconds": 1, "decision_id": None, "budget_state": "unknown",
                    "message": ADMISSION_BUSY_MESSAGE, "legacy_exposure": dict(row)}
        # A settlement may have committed while the admission lock was acquired.
        row = connection.execute(query, {"p": project_id}).mappings().first()
        if not row:
            return None
        evidence = dict(row)
        decision = connection.execute(text(
            "SELECT id,status FROM decisions WHERE project_id=:p "
            "AND recommendation->>'action'='reconcile_legacy_provider_exposure' "
            "AND recommendation->>'resource_receipt_id'=:i LIMIT 1"),
            {"p": project_id, "i": row['resource_receipt_id']}).mappings().first()
        if decision:
            decision_id = decision['id']
            if decision['status'] != 'pending':
                connection.execute(text("UPDATE decisions SET status='pending',resolution_note=NULL,updated_at=now() WHERE id=:id"),
                                   {"id": decision_id})
                emit_event(connection, project_id, 'budget.legacy_reconciliation_reopened', 'budget_governor',
                           'decision', decision_id, {"resource_receipt_id": row['resource_receipt_id']})
        else:
            decision_id = create_blocking_decision(
                connection, project_id, None, "Reconcile historical unresolved provider exposure before paid work",
                {**evidence, "action": "reconcile_legacy_provider_exposure"},
                {"original_estimate_usd_micros": int(row['original_estimate_usd_micros']), "actual_cost_known": False},
                "legacy_provider_outcome_unknown")
            emit_event(connection, project_id, 'budget.legacy_reconciliation_required', 'budget_governor',
                       'budget_reservation', row['reservation_id'], {"decision_id": decision_id, **evidence})
        return {"status": "denied", "refusal": "legacy_reconciliation_required", "transient": False,
                "decision_id": decision_id, "budget_state": "unknown", "legacy_exposure": evidence,
                "message": "Paid work is held for historical unresolved provider exposure. Reconcile the recorded attempt; raising a cap or resolving this decision alone does not authorize new spend."}

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
                  AND amount_usd_micros = 0
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
                  AND amount_usd_micros = 0
                  AND project_id = :project_id
                RETURNING id, project_id
                """
            )
            query_params["project_id"] = project_id

        rows = connection.execute(query, query_params).mappings().all()

        # Expiry revokes authority; it is not evidence that a provider never accepted work.
        # Keep positive exposure until explicit settlement or a known no-call release. The
        # generic/proof path has no reliable receipt link, so missing job/lease is not proof.
        pending = connection.execute(text(
            "SELECT r.id, r.project_id, r.amount_usd_micros FROM budget_reservations r "
            "WHERE r.status='active' AND r.amount_usd_micros > 0 AND r.expires_at <= :now_utc "
            "AND (CAST(:project_id AS text) IS NULL OR r.project_id=:project_id) "
            "AND NOT EXISTS (SELECT 1 FROM events e WHERE e.entity_id=r.id "
            "AND e.event_type='budget.reservation_reconciliation_required') "
            "FOR UPDATE OF r SKIP LOCKED"),
            {"now_utc": query_params['now_utc'], "project_id": project_id}).mappings().all()
        for row in pending:
            if connection.execute(text(
                    "SELECT 1 FROM events WHERE entity_id=:id "
                    "AND event_type='budget.reservation_reconciliation_required' LIMIT 1"),
                    {"id": row['id']}).first():
                continue
            decision_id = create_blocking_decision(
                connection, row['project_id'], None,
                f"Reconcile expired budget reservation {row['id']}",
                {"action": "verify_provider_outcome_then_settle_or_release", "reservation_id": row['id']},
                {"reserved_usd_micros": int(row['amount_usd_micros'])},
                "budget_outcome_unknown",
            )
            emit_event(connection, row['project_id'], 'budget.reservation_reconciliation_required',
                       'budget_governor', 'budget_reservation', row['id'],
                       {"decision_id": decision_id, "held_usd_micros": int(row['amount_usd_micros'])})

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

    def status_for_project(self, connection: Connection, project_id: str,
                           *, now: Optional[datetime] = None) -> dict:
        """The budget as every gate and screen reads it. Each cap is measured over its own
        period (period_spend); the period nearest its cap — the binding one — supplies the
        top-level numbers (`cap`, `actual`, `reserved`, `available`, `utilization`, state), and
        `periods` carries both. `now` is injectable so a test can stand on either side of a
        reset."""
        now = now or now_utc()
        legacy_hold = self.legacy_model_reconciliation(connection, project_id)
        legacy_summary = ({**legacy_hold['legacy_exposure'], 'decision_id': legacy_hold['decision_id'],
                           'message': "Paid work is held for historical unresolved model exposure. The original estimate is not an invoice; reconcile the recorded attempt."}
                          if legacy_hold else None)
        provider_hold = self.legacy_provider_reconciliation(connection, project_id)
        provider_summary = ({**provider_hold['legacy_exposure'], 'decision_id': provider_hold['decision_id'],
                             'message': "Paid work is held for an earlier provider attempt that may have been accepted. The original estimate is not an invoice; reconcile the specific provider outcome."}
                            if provider_hold else None)
        self.expire_stale_reservations(connection, project_id)
        policy_row = connection.execute(
            text(
                """
                SELECT daily_cap_usd_micros, monthly_cap_usd_micros, soft_cap_pct,
                       auto_lite_mode_pct, freeze_provisioning_pct, freeze_tier2_pct,
                       operational_intensity
                FROM budget_policies
                WHERE scope_type = 'project' AND scope_id = :project_id
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"project_id": project_id},
        ).mappings().first()

        if not policy_row:
            day = budget_period("day", now)
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
                "period": "day",
                "cap_name": day["cap_name"],
                "period_start": day["start"].isoformat(),
                "resets_at": day["end"].isoformat(),
                "resets_at_label": day["resets_at_label"],
                "budget_timezone": BUDGET_TIMEZONE,
                "periods": {},
                "legacy_financial_hold": legacy_summary,
                "legacy_provider_hold": provider_summary,
            }

        soft = float(policy_row["soft_cap_pct"])
        orange = float(policy_row["auto_lite_mode_pct"])
        red = float(policy_row["freeze_provisioning_pct"])
        tier2 = float(policy_row["freeze_tier2_pct"] or 0.95)

        periods: dict[str, dict[str, Any]] = {}
        for column, period in CAP_PERIODS:
            cap = int(policy_row[column] or 0)
            spend = period_spend(connection, project_id, period, now)
            committed = spend["actual_usd_micros"] + spend["reserved_usd_micros"]
            utilization = 0.0 if cap == 0 else committed / cap
            periods[period] = {
                "period": period,
                "cap_usd_micros": cap,
                "actual_usd_micros": spend["actual_usd_micros"],
                "reserved_usd_micros": spend["reserved_usd_micros"],
                "active_reservations_count": spend["active_reservations_count"],
                "committed_usd_micros": committed,
                "available_usd_micros": max(cap - committed, 0) if cap else 0,
                "utilization_ratio": round(utilization, 6),
                "budget_state": state_for_utilization(cap, utilization, soft, orange, red),
                "period_start": spend["start"].isoformat(),
                "resets_at": spend["end"].isoformat(),
                "resets_at_label": spend["resets_at_label"],
                "resets_in_seconds": max(int((spend["end"] - now).total_seconds()), 0),
                "whose": spend["whose"],
                "cap_name": spend["cap_name"],
                "_start": spend["start"],
                "_end": spend["end"],
            }
        # The binding period: the one nearest its cap (a tie goes to the day). A period with no
        # cap set binds only when no period has one.
        capped = [p for p in periods.values() if p["cap_usd_micros"]]
        binding = max(capped, key=lambda p: p["utilization_ratio"]) if capped else periods["day"]
        cap = binding["cap_usd_micros"]
        actual = binding["actual_usd_micros"]
        reserved = binding["reserved_usd_micros"]
        committed = binding["committed_usd_micros"]
        utilization = binding["utilization_ratio"]
        # Enforced (BOSS-280): past this utilization, tier-2+ risk work is refused at reserve().
        tier2_frozen = bool(cap and utilization >= tier2)

        window = {"project_id": project_id, "start": binding["_start"], "end": binding["_end"]}
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
                      AND created_at >= :start AND created_at < :end
                      AND NOT COALESCE(included_in_subscription, false)
                    GROUP BY skill_or_model_id
                    ORDER BY amount_usd_micros DESC, skill_or_model_id ASC
                    LIMIT 5
                    """
                ),
                window,
            ).mappings().all()
        ]

        # WHAT THE SUBSCRIPTIONS DID, AT LIST PRICE (included_spend.py), over the same period.
        # Not money and not in any cap — reported beside the money so it is never dropped.
        included = connection.execute(
            text(
                f"""
                SELECT skill_or_model_id, COALESCE(SUM({INCLUDED_SQL}), 0) AS amount
                FROM cost_receipts
                WHERE project_id = :project_id
                  AND created_at >= :start AND created_at < :end
                  AND COALESCE(included_in_subscription, false)
                GROUP BY skill_or_model_id
                ORDER BY amount DESC, skill_or_model_id ASC
                """
            ),
            window,
        ).mappings().all()
        included_total = sum(int(row["amount"] or 0) for row in included)
        included_drivers = [
            {"skill_or_model_id": row["skill_or_model_id"],
             "amount_usd_micros": int(row["amount"] or 0)}
            for row in included[:5]
        ]

        # The burn rate is a trailing 24 hours (a rate, not a period total).
        burn_window_hours = 24
        recent_actual = int(connection.execute(
            text(
                f"""
                SELECT COALESCE(SUM({MONEY_SQL}), 0) AS total
                FROM cost_receipts
                WHERE project_id = :project_id
                  AND created_at >= :burn_window_start AND created_at <= :now
                """
            ),
            {"project_id": project_id,
             "burn_window_start": now - timedelta(hours=burn_window_hours), "now": now},
        ).scalar_one())
        burn_rate = int(recent_actual / burn_window_hours)
        forecasted_24h = recent_actual + reserved
        # Time to the BINDING cap, within its period. A cap the burn rate would not reach
        # before the period resets is not "reached in N hours": it is not reached at all.
        time_to_cap_seconds: Optional[int] = None
        if cap and committed >= cap:
            time_to_cap_seconds = 0
        elif cap and burn_rate > 0:
            remaining = max(cap - committed, 0)
            seconds = int((remaining / burn_rate) * 3600)
            if seconds < binding["resets_in_seconds"]:
                time_to_cap_seconds = seconds

        for entry in periods.values():
            entry.pop("_start", None)
            entry.pop("_end", None)
        return {
            "budget_state": binding["budget_state"],
            "cap_usd_micros": cap,
            "reserved_usd_micros": reserved,
            "actual_usd_micros": actual,
            "available_usd_micros": binding["available_usd_micros"],
            "committed_usd_micros": committed,
            "burn_approx_usd_micros": committed,
            "burn_window_hours": burn_window_hours,
            "burn_rate_usd_micros_per_hour": burn_rate,
            "forecasted_24h_usd_micros": forecasted_24h,
            "time_to_cap_seconds": time_to_cap_seconds,
            "utilization_ratio": utilization,
            "active_reservations_count": binding["active_reservations_count"],
            "top_cost_drivers": top_cost_drivers,
            "included_in_subscription_usd_micros": included_total,
            "included_cost_drivers": included_drivers,
            "soft_cap_pct": soft,
            "auto_lite_mode_pct": orange,
            "freeze_provisioning_pct": red,
            "freeze_tier2_pct": tier2,
            "tier2_frozen": tier2_frozen,
            "operational_intensity": policy_row["operational_intensity"],
            # Which period the numbers above are, and when it turns over.
            "period": binding["period"],
            "cap_name": binding["cap_name"],
            "whose": binding["whose"],
            "period_start": binding["period_start"],
            "resets_at": binding["resets_at"],
            "resets_at_label": binding["resets_at_label"],
            "resets_in_seconds": binding["resets_in_seconds"],
            "budget_timezone": BUDGET_TIMEZONE,
            "periods": periods,
            "legacy_financial_hold": legacy_summary,
            "legacy_provider_hold": provider_summary,
        }

    def cap_refusal(self, snapshot: dict, amount_usd_micros: int) -> Optional[dict[str, Any]]:
        """Would `amount_usd_micros` more break a cap? None if it fits every period. Otherwise
        the period that refuses it, and how: `cap` when the money ALREADY SPENT in that period
        leaves no room (a person must act, or the period must reset), `headroom` when only other
        work's active reservations fill it (clears by itself). `cap` wins over `headroom`."""
        refusals = []
        for period in (snapshot.get("periods") or {}).values():
            cap = int(period.get("cap_usd_micros") or 0)
            if not cap:
                continue
            if period["committed_usd_micros"] + amount_usd_micros <= cap:
                continue
            on_actual = period["actual_usd_micros"] + amount_usd_micros > cap
            refusals.append({**period, "kind": "cap" if on_actual else "headroom"})
        if not refusals:
            return None
        return next((r for r in refusals if r["kind"] == "cap"), refusals[0])

    def set_intensity(
        self, connection: Connection, scope_type: str, scope_id: str, intensity: int,
        actor: str = "ceo",
    ) -> dict:
        """CEO Operational Intensity slider (0-100) → spend envelope + scaling limits.
        Maps one knob to daily/monthly/burst caps, max concurrency/workers, and allowed model
        tiers, then upserts the budget policy for the scope. Returns the derived envelope."""
        envelope = intensity_envelope(intensity)
        intensity = envelope["operational_intensity"]
        daily = envelope["daily_cap_usd_micros"]
        monthly = envelope["monthly_cap_usd_micros"]
        burst = envelope["burst_cap_usd_micros_per_min"]
        max_concurrent = envelope["max_concurrent_jobs"]
        max_workers = envelope["max_workers_total"]
        tiers = envelope["allowed_model_tiers"]
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
        if scope_type == "project":
            release_budget_holds(connection, scope_id)
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
            # A root allocation is bounded by the project's DAILY cap, as it always was —
            # the cap itself, not what is left of today.
            policy = self.status_for_project(connection, project_id)
            daily_cap = int(((policy.get("periods") or {}).get("day") or {})
                            .get("cap_usd_micros") or policy["cap_usd_micros"] or 0)
            if daily_cap and amount_usd_micros > daily_cap:
                emit_event(
                    connection,
                    project_id,
                    "budget.allocation_denied",
                    actor,
                    "project",
                    project_id,
                    {
                        "requested_usd_micros": amount_usd_micros,
                        "cap_usd_micros": daily_cap,
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
        # Every period's cap must fit the request; the post-reservation numbers are those of
        # the period it leaves nearest its cap.
        after = []
        for period in (snapshot.get("periods") or {}).values():
            cap = int(period.get("cap_usd_micros") or 0)
            if not cap:
                continue
            total_after = period["committed_usd_micros"] + amount_usd_micros
            after.append((total_after / cap, cap, total_after))
        allowed = all(total <= cap for _, cap, total in after)
        if after:
            _, post_cap, post_total = max(after)
            post_available = max(post_cap - post_total, 0)
        else:
            post_cap, post_total, post_available = 0, 0, 0
        post_state = self._state_for_totals(
            post_cap,
            post_total,
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
                # A BUILD'S SETTLEMENT IS NOT A BURST. It books, in one receipt, the money a
                # build spent over its whole run (build_budget.settle_or_release); counted
                # here, a $5 build froze every chat turn and reservation for a minute after it
                # finished. The daily and monthly caps count it in full.
                "  AND category IS DISTINCT FROM :build "
                "ORDER BY created_at"
            ),
            {"p": project_id, "w": BURST_WINDOW_SECONDS, "build": BUILD_CATEGORY},
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
        # Covers every allocation and allocation-free caller. Try rather than wait: some
        # callers retain their transaction across work or already own source/allocation
        # locks. Contention must not form a lock-order cycle or queue behind provider I/O.
        admitted = _try_admit(connection, project_id)
        if not admitted:
            refused = {"status": "denied", "refusal": "admission_busy", "transient": True,
                       "retry_after_seconds": 1, "decision_id": None, "budget_state": "unknown",
                       "message": ADMISSION_BUSY_MESSAGE}
            record_policy_eval(connection, project_id, 'budget.reserve', 'defer', action=category,
                               reason='project admission transaction busy',
                               detail={"requested_usd_micros": amount_usd_micros, "retry_after_seconds": 1})
            record_refusal(connection, project_id, refused, category=category, reason=reason,
                           amount_usd_micros=amount_usd_micros)
            return refused
        self.expire_stale_reservations(connection, project_id)
        snapshot = self.status_for_project(connection, project_id)
        if amount_usd_micros > 0 and (snapshot.get('legacy_financial_hold') or snapshot.get('legacy_provider_hold')):
            hold = snapshot.get('legacy_financial_hold') or snapshot['legacy_provider_hold']
            refused = {"status": "denied", "refusal": "legacy_reconciliation_required", "transient": False,
                       "decision_id": hold['decision_id'], "budget_state": snapshot['budget_state'],
                       "message": hold['message']}
            record_refusal(connection, project_id, refused, category=category, reason=reason,
                           amount_usd_micros=amount_usd_micros, snapshot=snapshot)
            return refused

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
            refused = {
                "status": "denied",
                "refusal": "tier2_frozen",
                "transient": False,
                "decision_id": decision_id,
                "budget_state": snapshot["budget_state"],
                "message": ("Tier-2+ work is frozen at this budget utilization "
                            f"({cap_used_sentence(snapshot)}). {resets_sentence(snapshot)} "
                            "Decision created."),
            }
            record_refusal(connection, project_id, refused, category=category, reason=reason,
                           amount_usd_micros=amount_usd_micros, snapshot=snapshot)
            return refused
        refusal = self.cap_refusal(snapshot, amount_usd_micros)
        if refusal is not None:
            # Every cap is checked against ITS period (period_spend): today's money against the
            # daily cap, this month's against the monthly one. Yesterday's spend never refuses
            # today's work.
            period_detail = {
                "period": refusal["period"],
                "cap_usd_micros": refusal["cap_usd_micros"],
                "actual_usd_micros": refusal["actual_usd_micros"],
                "reserved_usd_micros": refusal["reserved_usd_micros"],
                "resets_at": refusal["resets_at"],
            }
            # OTHER WORK'S RESERVATIONS ARE A MOMENT, NOT A CAP. `reserved` is what active
            # reservations hold right now — a governed code task alone holds $2 while it runs —
            # and it is consumed or released when that work ends. Five runs started together
            # can fill the cap with holds while the money actually spent is far under it ($7.03
            # of $10 free in the incident). Refusing that as `cap_exceeded` opened a blocking
            # decision and told a person to "raise the daily or monthly cap" for something that
            # clears by itself. Only a request the cap refuses on ACTUAL spend needs a person.
            if refusal["kind"] == "headroom":
                reconciliation = connection.execute(text(
                    "SELECT d.id FROM decisions d JOIN budget_reservations r ON "
                    "(d.recommendation->>'reservation_id'=r.id OR d.cost_summary->>'budget_reservation_id'=r.id) "
                    "WHERE d.project_id=:p AND r.project_id=:p AND d.status='pending' AND r.status='active' "
                    "AND d.recommendation->>'action' IN ('verify_provider_outcome_then_settle_or_release', "
                    "'verify_provider_outcome_before_retry') ORDER BY d.created_at LIMIT 1"),
                    {"p": project_id}).first()
                if reconciliation:
                    refused = {"status": "denied", "refusal": "reconciliation_required", "transient": False,
                               "decision_id": reconciliation[0], "budget_state": snapshot['budget_state'],
                               "message": "Budget is held for an unresolved provider outcome. Reconcile the recorded attempt before retrying."}
                    record_policy_eval(connection, project_id, 'budget.reserve', 'deny', action=category,
                                       reason='outstanding exposure requires reconciliation',
                                       detail={"decision_id": reconciliation[0], **period_detail})
                    record_refusal(connection, project_id, refused, category=category, reason=reason,
                                   amount_usd_micros=amount_usd_micros, period=refusal)
                    return refused
                record_policy_eval(
                    connection, project_id, "budget.reserve", "deny", action=category,
                    reason="budget headroom held by active reservations",
                    detail={"requested_usd_micros": amount_usd_micros, **period_detail,
                            "retry_after_seconds": HEADROOM_RETRY_AFTER_SECONDS})
                refused = {
                    "status": "denied",
                    "refusal": "headroom",
                    "transient": True,
                    "retry_after_seconds": HEADROOM_RETRY_AFTER_SECONDS,
                    "decision_id": None,
                    "budget_state": snapshot["budget_state"],
                    "message": HEADROOM_MESSAGE,
                }
                record_refusal(connection, project_id, refused, category=category,
                               reason=reason, amount_usd_micros=amount_usd_micros,
                               period=refusal)
                return refused
            decision_id = create_blocking_decision(
                connection,
                project_id,
                None,
                "Budget reservation denied",
                {"action": "increase_budget_or_defer"},
                {
                    "requested_usd_micros": amount_usd_micros,
                    "available_usd_micros": refusal["available_usd_micros"],
                    **period_detail,
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
                {"requested_usd_micros": amount_usd_micros, "reason": reason,
                 "period": refusal["period"]},
            )
            record_policy_eval(
                connection, project_id, "budget.reserve", "deny",
                action=category, reason="budget cap exceeded",
                detail={"requested_usd_micros": amount_usd_micros, **period_detail,
                        "budget_state": snapshot["budget_state"], "decision_id": decision_id})
            refused = {
                "status": "denied",
                "refusal": "cap_exceeded",
                "transient": False,
                "decision_id": decision_id,
                "budget_state": snapshot["budget_state"],
                "period": refusal["period"],
                "resets_at": refusal["resets_at"],
                # "Budget cap exceeded: today's budget is 98% used ($9.80 of $10). It resets
                # at 00:00 UTC, or raise the daily cap in Govern › Limits. Decision created."
                "message": (f"Budget cap exceeded: "
                            f"{cap_refusal_sentence(refusal, amount_usd_micros)}. "
                            f"{resets_sentence(refusal)} Decision created."),
            }
            record_refusal(connection, project_id, refused, category=category, reason=reason,
                           amount_usd_micros=amount_usd_micros, period=refusal)
            return refused

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
                record_refusal(connection, project_id, refused, category=category,
                               reason=reason, amount_usd_micros=amount_usd_micros,
                               period={"cap_usd_micros": burst_cap, "period": "minute"})
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
                refused = {
                    "status": "denied",
                    "refusal": "allocation_exceeded",
                    "transient": False,
                    "decision_id": decision_id,
                    "budget_state": snapshot["budget_state"],
                    "message": "Budget allocation exceeded. Decision created.",
                }
                record_refusal(connection, project_id, refused, category=category,
                               reason=reason, amount_usd_micros=amount_usd_micros)
                return refused

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
        row = connection.execute(
            text(
                """
                SELECT id, status, category, amount_usd_micros
                FROM budget_reservations
                WHERE id = :reservation_id AND project_id = :project_id
                FOR UPDATE
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
        self._resolve_reconciliation(connection, project_id, reservation_id, reason)
        snapshot = self.status_for_project(connection, project_id)
        return {
            "status": "released",
            "reservation_id": reservation_id,
            "budget_state": snapshot["budget_state"],
            "message": reason,
        }

    def _resolve_reconciliation(self, connection: Connection, project_id: str,
                                reservation_id: str, reason: str) -> None:
        rows = connection.execute(text(
            "UPDATE decisions SET status='resolved', resolution_note=:reason, updated_at=clock_timestamp() "
            "WHERE project_id=:p AND status='pending' "
            "AND recommendation->>'action' IN ('verify_provider_outcome_then_settle_or_release', "
            "'verify_provider_outcome_before_retry') "
            "AND (recommendation->>'reservation_id'=:r OR cost_summary->>'budget_reservation_id'=:r) "
            "AND (recommendation->>'action' != 'verify_provider_outcome_before_retry' OR NOT EXISTS "
            "(SELECT 1 FROM model_gateway_invocations i WHERE i.model_gateway_lease_id=decisions.recommendation->>'lease_id' "
            "AND i.status='in_progress')) "
            "RETURNING id"), {"p": project_id, "r": reservation_id, "reason": reason}).scalars().all()
        if rows:
            emit_event(connection, project_id, 'budget.reservation_reconciled', 'budget_governor',
                       'budget_reservation', reservation_id, {"decision_ids": list(rows), "reason": reason})

    def resolve_legacy_exposure(self, connection: Connection, project_id: str, decision_id: str,
                                *, settle_at: str, actor: str) -> dict[str, Any]:
        """Give a 'reconcile_legacy_model_exposure' / 'reconcile_legacy_provider_exposure'
        decision a REAL ACTION. These never resolve themselves: _resolve_reconciliation only
        watches 'verify_provider_outcome_*' decisions, which close when a worker later settles
        or releases their reservation in the ordinary course of work. A legacy-exposure
        decision's reservation is ALREADY expired or released — nothing will ever settle or
        release it again — so without this the decision (and the admission block with it)
        reopened on every paid attempt forever (boss-1 rc10.9).

        Writes the cost_receipts row the recorded attempt never got — `settle_at="zero"`: the
        provider refused, nothing was spent; `settle_at="estimate"`: the original estimate
        stands, its outcome could not be confirmed either way — and resolves the decision.
        Idempotent: a second call against an already-resolved decision raises, a cost_receipts
        row already written for this reservation+receipt is reused rather than duplicated."""
        if settle_at not in ("zero", "estimate"):
            raise ValueError('settle_at must be "zero" or "estimate".')
        decision = connection.execute(text(
            "SELECT id, status, recommendation FROM decisions WHERE id=:id AND project_id=:p"),
            {"id": decision_id, "p": project_id}).mappings().first()
        if not decision:
            raise ValueError("No such decision.")
        rec = decision["recommendation"]
        rec = rec if isinstance(rec, dict) else (json.loads(rec) if rec else {})
        action = str(rec.get("action") or "")
        if action not in ("reconcile_legacy_model_exposure", "reconcile_legacy_provider_exposure"):
            raise ValueError("This decision is not a legacy-exposure reconciliation.")
        from .decision_actions import OPEN_STATUSES

        if decision["status"] not in OPEN_STATUSES:
            raise ValueError(f"Decision is already {decision['status']}.")
        reservation_id = str(rec.get("reservation_id") or "")
        receipt_id = str(rec.get("receipt_id") or "")
        estimate = int(rec.get("original_estimate_usd_micros") or 0)
        if not reservation_id or not receipt_id:
            raise ValueError("Decision is missing its reservation or receipt.")
        actual = 0 if settle_at == "zero" else estimate
        existing = connection.execute(text(
            "SELECT id FROM cost_receipts WHERE project_id=:p AND reservation_id=:r "
            "AND receipt_id=:rc"), {"p": project_id, "r": reservation_id, "rc": receipt_id}
        ).first()
        if existing is None:
            cost_receipt_id = make_id("cost")
            connection.execute(text(
                "INSERT INTO cost_receipts (id, project_id, reservation_id, receipt_id, "
                "category, skill_or_model_id, estimated_usd_micros, actual_usd_micros, status, "
                "equivalent_usd_micros, included_in_subscription, created_at) "
                "VALUES (:id, :p, :r, :rc, 'model_gateway', 'legacy_reconciliation', :est, "
                ":act, 'actual', NULL, false, clock_timestamp())"),
                {"id": cost_receipt_id, "p": project_id, "r": reservation_id, "rc": receipt_id,
                 "est": estimate, "act": actual})
        else:
            cost_receipt_id = existing[0]
        note = (f"Settled at {'the estimate' if settle_at == 'estimate' else '$0 (provider refused)'} "
               f"by {actor}.")
        connection.execute(text(
            "UPDATE decisions SET status='resolved', resolution_note=:note, "
            "updated_at=clock_timestamp() WHERE id=:id"), {"id": decision_id, "note": note})
        emit_event(connection, project_id, "budget.legacy_exposure_resolved", actor,
                   "budget_reservation", reservation_id,
                   {"decision_id": decision_id, "receipt_id": receipt_id,
                    "cost_receipt_id": cost_receipt_id, "settle_at": settle_at,
                    "actual_usd_micros": actual, "action": action})
        return {"decision_id": decision_id, "cost_receipt_id": cost_receipt_id,
               "actual_usd_micros": actual, "settle_at": settle_at}

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
        allow_expired: bool = False,
    ) -> dict[str, Any]:
        """Settle `reservation_id` with a call's cost. `included_in_subscription` (a subscription,
        licensed seat or local model answered — included_spend.py): `actual_usd_micros` is the
        call's API-equivalent value, recorded as such, and the money settled is $0."""
        if estimated_usd_micros < 0:
            raise ValueError("estimated_usd_micros must be non-negative.")
        if actual_usd_micros is not None and actual_usd_micros < 0:
            raise ValueError("actual_usd_micros must be non-negative.")
        # Settlement reconciles an existing exposure; an expired authorization may not be
        # reused for admission, but its already-incurred cost must still be recorded.
        reservation = connection.execute(text(
            "SELECT id, amount_usd_micros, status FROM budget_reservations "
            "WHERE id=:id AND project_id=:p FOR UPDATE"),
            {"id": reservation_id, "p": project_id}).mappings().first()
        if reservation and reservation['status'] == 'consumed':
            settled = connection.execute(text(
                "SELECT * FROM cost_receipts WHERE reservation_id=:id AND receipt_id=:receipt "
                "AND project_id=:p ORDER BY created_at LIMIT 1"),
                {"id": reservation_id, "receipt": receipt_id, "p": project_id}).mappings().first()
            if settled:
                equivalent = (int(actual_usd_micros if actual_usd_micros is not None
                                  else estimated_usd_micros) if included_in_subscription else None)
                if (settled['estimated_usd_micros'] != estimated_usd_micros
                        or settled['included_in_subscription'] != included_in_subscription
                        or settled['equivalent_usd_micros'] != equivalent
                        or (not included_in_subscription and settled['actual_usd_micros'] != actual_usd_micros)):
                    raise ValueError("Reservation was already settled with different cost evidence.")
                return {"status": "consumed", "reservation_id": reservation_id,
                        "budget_state": self.status_for_project(connection, project_id)['budget_state'],
                        "message": "Reservation already consumed.", "cost_receipt": dict(settled)}
        # `allow_expired`: a call settled after its worker lost the lease
        # (ModelGatewayService.settle_retained_invocation). Only a $0 hold ever expires; the
        # call it admitted was still made, and its usage is still recorded.
        settleable = ('active', 'expired') if allow_expired else ('active',)
        if reservation and reservation['status'] not in settleable:
            reservation = None
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
            allow_expired=allow_expired,
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

    def extend(self, connection: Connection, project_id: str, reservation_id: str,
               expires_in_minutes: int) -> bool:
        """Renew an ACTIVE reservation's expiry: the work it holds for is still running (a
        build at a stage boundary, build_budget.renew). Never extends a settled, released or
        expired one, and never shortens one. True when the hold was renewed."""
        renewed = connection.execute(
            text("UPDATE budget_reservations SET expires_at = GREATEST(expires_at, :at) "
                 "WHERE id = :id AND project_id = :p AND status = 'active'"),
            {"id": reservation_id, "p": project_id, "at": expires_at(expires_in_minutes)})
        return bool(getattr(renewed, "rowcount", 0))

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

        if not row or row["status"] != "active" or row["expires_at"] <= now_utc():
            return None
        if row['amount_usd_micros'] > 0 and (self.legacy_model_reconciliation(connection, project_id)
                or self.legacy_provider_reconciliation(connection, project_id)):
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
        allow_expired: bool = False,
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
        reservation = connection.execute(text(
            "SELECT status FROM budget_reservations WHERE id=:id AND project_id=:p FOR UPDATE"),
            {"id": reservation_id, "p": project_id}).mappings().first()
        if reservation and reservation['status'] == 'consumed':
            prior = connection.execute(text(
                "SELECT * FROM cost_receipts WHERE reservation_id=:id AND project_id=:p "
                "AND receipt_id=:receipt ORDER BY created_at LIMIT 1"),
                {"id": reservation_id, "p": project_id, "receipt": receipt_id}).mappings().first()
            if (prior and prior['estimated_usd_micros'] == estimated_usd_micros
                    and prior['actual_usd_micros'] == actual_usd_micros
                    and prior['included_in_subscription'] == included_in_subscription
                    and prior['equivalent_usd_micros'] == equivalent):
                return prior['id']
            raise ValueError("Reservation was already settled with different cost evidence.")
        if not reservation or reservation['status'] not in (
                ('active', 'expired') if allow_expired else ('active',)):
            raise ValueError("Active budget reservation not found.")
        cost_receipt_id = make_id("cost")
        connection.execute(
            text(
                """
                INSERT INTO cost_receipts (
                    id, project_id, reservation_id, receipt_id, category, skill_or_model_id,
                    estimated_usd_micros, actual_usd_micros, status,
                    equivalent_usd_micros, included_in_subscription, created_at
                )
                VALUES (
                    :id, :project_id, :reservation_id, :receipt_id, :category, :skill_or_model_id,
                    :estimated_usd_micros, :actual_usd_micros, :status,
                    :equivalent_usd_micros, :included, clock_timestamp()
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
        self._resolve_reconciliation(connection, project_id, reservation_id, 'Cost receipt recorded.')
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
        utilization = committed_usd_micros / cap_usd_micros if cap_usd_micros else 0.0
        return state_for_utilization(cap_usd_micros, utilization, soft_cap_pct,
                                     auto_lite_mode_pct, freeze_provisioning_pct)
