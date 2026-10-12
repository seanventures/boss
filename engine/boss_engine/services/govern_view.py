"""Govern, as one view: every limit, the value the engine actually uses, what enforces it, and
what it stopped.

The Govern screen made eleven reads on every render (one of them a POST) and showed controls
whose labels said more than the engine did: `Agents at once` has a floor of 6, `Review rounds` 0
reads as 2, token policies are never enforced, and the intensity slider claimed to choose model
tiers that nothing reads. This module answers the screen in one read, and every
`value_effective` is computed by the SAME helper the engine reads (project_settings,
budget.intensity_envelope), so the board and the engine cannot disagree.

Read-only. Nothing here changes what any control enforces.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import project_settings as ps
from .budget import (
    BudgetGovernor,
    budget_period,
    intensity_envelope,
    policy_version,
)
from .included_spend import INCLUDED_SQL, MONEY_SQL

#: The budget state, as the meter says it. One map: the screen never shows `orange`.
STATE_LABELS = {"green": "Under cap", "yellow": "Heads-up", "orange": "Cheaper builds",
                "red": "Frozen", "unknown": "Unknown"}

#: What enforces each control, in plain words (`word`) and as a key the console maps to its
#: tooltip. EVERY control on the board has an entry — a test checks every registry knob and
#: every budget_policies limit against this map — so no control can appear without saying
#: whether it actually stops anything.
ENFORCED: dict[str, tuple[str, str]] = {
    "daily_cap": ("Hard cap", "hard_cap"),
    "monthly_cap": ("Hard cap", "hard_cap"),
    "burst": ("Waits", "waits"),
    "pace": ("Preset", "preset"),
    "code_agent_max_concurrency": ("Review panel", "review_panel"),
    "code_agent_max_turns": ("Each agent", "each_agent"),
    "code_review_inrun_max_rounds": ("Review loop", "review_loop"),
    "captain_leads_projects": ("Every build", "every_build"),
    "captain_auto_improve": ("Captain's own builds", "captain_own_builds"),
    "max_concurrent_jobs": ("Job queue", "job_queue"),
    "max_workers_total": ("Scaling", "scaling"),
    "token_default": ("Advisory", "advisory"),
    "token_per_step": ("Advisory", "advisory"),
}

#: The on/off controls (0 or 1), drawn as a switch.
SWITCHES = frozenset({"captain_leads_projects", "captain_auto_improve"})

#: The four thresholds, as the screen names them (key, label, budget_policies column).
THRESHOLDS = (
    ("soft_cap_pct", "Heads-up"),
    ("auto_lite_mode_pct", "Cheaper builds"),
    ("freeze_provisioning_pct", "No new servers"),
    ("freeze_tier2_pct", "Risky work stops"),
)

#: What the seed writes (bootstrap.seed_budget_policy); a cap still at it reads `Default`.
EVIDENCE_DAYS = 7


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def _policy(connection: Connection, project_id: str) -> dict[str, Any]:
    row = connection.execute(text(
        "SELECT daily_cap_usd_micros, monthly_cap_usd_micros, burst_cap_usd_micros_per_min, "
        "max_concurrent_jobs, max_workers_total, operational_intensity, soft_cap_pct, "
        "auto_lite_mode_pct, freeze_provisioning_pct, freeze_tier2_pct, version "
        "FROM budget_policies WHERE scope_type = 'project' AND scope_id = :p "
        "ORDER BY created_at DESC LIMIT 1"), {"p": project_id}).mappings().first()
    return dict(row) if row else {}


def _evidence(connection: Connection, project_id: str) -> dict[str, int]:
    """What each limit stopped in the last week — counted from RECORDED signals only:
    `budget.reservation_denied` (a cap refusal, with its period), `budget.refused` (burst and
    the other transient refusals) and `code_task.implement_continued` (an agent hit its turn
    budget and was resumed). A limit with no signal has no evidence, never a guessed one."""
    row = connection.execute(text("""
        SELECT
          count(*) FILTER (WHERE event_type = 'budget.reservation_denied'
                           AND data->>'period' = 'day') AS daily_cap,
          count(*) FILTER (WHERE event_type = 'budget.reservation_denied'
                           AND data->>'period' = 'month') AS monthly_cap,
          count(*) FILTER (WHERE event_type = 'budget.refused'
                           AND data->>'kind' = 'burst') AS burst,
          count(*) FILTER (WHERE event_type = 'code_task.implement_continued')
                           AS code_agent_max_turns
        FROM events
        WHERE project_id = :p AND created_at > now() - make_interval(days => :d)
          AND event_type IN ('budget.reservation_denied', 'budget.refused',
                             'code_task.implement_continued')"""),
        {"p": project_id, "d": EVIDENCE_DAYS}).mappings().one()
    return {k: int(v or 0) for k, v in row.items()}


def _evidence_entry(key: str, counts: dict[str, int]) -> Optional[dict[str, Any]]:
    if key not in counts:
        return None
    n = counts[key]
    if key == "code_agent_max_turns":
        word = f"Hit {n}× · 7d" if n else "Never hit"
        log = {"kind": "code_task.implement_continued"}
    elif key == "burst":
        word = f"Held {n}× · 7d" if n else "Never held"
        log = {"kind": "budget.refused"}
    else:
        word = f"Stopped {n}× · 7d" if n else "Never stopped"
        log = {"kind": "budget.reservation_denied"}
    return {"word": word, "count_7d": n, "log": log}


def _money_source(value: int, *, pace_value: Optional[int], seed_value: int) -> str:
    if pace_value is not None and value == pace_value:
        return "pace"
    if value == seed_value:
        return "default"
    return "set"


def _seats_now(connection: Connection, effective: Optional[int]) -> Optional[dict[str, Any]]:
    seats = ps.memory_limited_seats(connection)
    if seats is None or effective is None or seats >= effective:
        return None
    return {"value": seats, "reason": "memory"}


def _knob_control(connection: Connection, settings: Any, project_id: str, key: str,
                  chosen: dict[str, Any], counts: dict[str, int]) -> dict[str, Any]:
    spec = ps.KNOBS[key]
    default = getattr(settings, spec["attr"], None)
    stored = chosen.get(key)
    raw = stored if stored not in (None, "") else default
    note: Optional[dict[str, Any]] = None
    is_set = stored not in (None, "")
    if key == "code_agent_max_concurrency":
        effective = ps.effective_review_lanes(raw)
        # The note explains a value someone SET; the default is just the default (D14c: after
        # Use default it read "set 1 · min 6" from the deployment's 1).
        if is_set and int(raw or ps.REVIEW_LANES_FALLBACK) < ps.REVIEW_LANES_FLOOR:
            note = {"kind": "floor", "stored": raw, "floor": ps.REVIEW_LANES_FLOOR}
    elif key == "code_review_inrun_max_rounds":
        effective = ps.effective_review_rounds(raw)
        if is_set and int(raw or 0) == 0:
            note = {"kind": "reads_as", "stored": 0, "reads_as": effective}
    elif key in SWITCHES:
        effective = 1 if int(raw or 0) else 0
    else:
        effective = int(raw) if raw not in (None, "") else None
    if not is_set:
        note = {"kind": "default"}
    version, _who = ps.version_of(connection, project_id, key)
    word, effect = ENFORCED[key]
    return {
        "key": key,
        "name": ps.GOVERN_NAMES[key],
        "section": "agents",
        "value_effective": effective,
        "value_stored": stored,
        "default": default,
        "source": "set" if key in chosen else "default",
        "unit": "switch" if key in SWITCHES else "count",
        "bounds": {"min": spec.get("min"), "max": spec.get("max"), "step": 1},
        "editable_by": "self",
        "write": {"method": "PUT", "path": f"/v1/settings/{key}"},
        "enforced": {"word": word, "effect_key": effect},
        "evidence": _evidence_entry(key, counts),
        "advisory": False,
        "note": note,
        # What the panel runs RIGHT NOW when memory limits it (the pipeline's own reading):
        # "Review lanes 6 · running 1 now (memory)". None when nothing limits it.
        "running_now": (_seats_now(connection, effective)
                        if key == "code_agent_max_concurrency" else None),
        # How a stored value becomes the effective one, so an edit's preview ("BOSS will use 6")
        # is the engine's arithmetic with the engine's constants, not a copy of them.
        "rule": ({"kind": "floor", "floor": ps.REVIEW_LANES_FLOOR,
                  "fallback": ps.REVIEW_LANES_FALLBACK}
                 if key == "code_agent_max_concurrency" else
                 {"kind": "zero_reads_as", "value": ps.REVIEW_ROUNDS_FALLBACK}
                 if key == "code_review_inrun_max_rounds" else None),
        "version": version,
    }


def build(connection: Connection, settings: Any, project_id: str, *,
          now: Optional[datetime] = None) -> dict[str, Any]:
    """The whole Limits view — meter, every control, thresholds, token guides — in one read."""
    status = BudgetGovernor(settings).status_for_project(connection, project_id, now=now)
    policy = _policy(connection, project_id)
    counts = _evidence(connection, project_id)
    budget_version, _who = policy_version(connection, project_id)
    intensity = policy.get("operational_intensity")
    pace = intensity_envelope(intensity) if intensity is not None else None
    daily = int(policy.get("daily_cap_usd_micros") or 0)
    monthly = int(policy.get("monthly_cap_usd_micros") or 0)
    burst = int(policy.get("burst_cap_usd_micros_per_min") or 0)

    def money(key: str, name: str, value: int, *, unit: str, pace_key: str, seed: int,
              editable_by: str, write: Optional[dict], bounds: Optional[dict]) -> dict:
        word, effect = ENFORCED[key]
        return {
            "key": key, "name": name, "section": "spend",
            "value_effective": value, "value_stored": value, "default": seed,
            "source": _money_source(value, pace_value=(pace or {}).get(pace_key),
                                    seed_value=seed),
            "unit": unit, "bounds": bounds, "editable_by": editable_by, "write": write,
            "enforced": {"word": word, "effect_key": effect},
            "evidence": _evidence_entry(key, counts), "advisory": False, "note": None,
            "version": budget_version,
        }

    controls: list[dict[str, Any]] = [
        money("daily_cap", "Daily cap", daily, unit="usd_micros",
              pace_key="daily_cap_usd_micros", seed=int(settings.daily_cap_usd_micros),
              editable_by="pace", write=None, bounds=None),
        money("monthly_cap", "Monthly cap", monthly, unit="usd_micros",
              pace_key="monthly_cap_usd_micros", seed=int(settings.monthly_cap_usd_micros),
              editable_by="self", write={"method": "PUT", "path": "/v1/setup/budget"},
              bounds={"min": 10_000, "max": 1_000_000_000_000, "step": 1_000_000}),
        money("burst", "Burst", burst, unit="usd_micros_per_min",
              pace_key="burst_cap_usd_micros_per_min",
              seed=int(settings.burst_cap_usd_micros_per_min), editable_by="pace", write=None,
              bounds=None),
        {
            "key": "pace", "name": "Pace", "section": "spend",
            "value_effective": intensity, "value_stored": intensity, "default": None,
            "source": "set" if intensity is not None else "default", "unit": "intensity",
            "bounds": {"min": 0, "max": 100, "step": 1}, "editable_by": "self",
            "write": {"method": "POST", "path": "/v1/budget/set_intensity"},
            "enforced": {"word": ENFORCED["pace"][0], "effect_key": ENFORCED["pace"][1]},
            "evidence": None, "advisory": False, "note": None, "version": budget_version,
        },
    ]
    chosen = ps.get_all(connection, project_id)
    for key in ("code_agent_max_concurrency", "code_agent_max_turns",
                "code_review_inrun_max_rounds", "captain_leads_projects", "captain_auto_improve"):
        controls.append(_knob_control(connection, settings, project_id, key, chosen, counts))
    for key, name in (("max_concurrent_jobs", "Jobs at once"), ("max_workers_total", "Workers")):
        word, effect = ENFORCED[key]
        value = policy.get(key)
        controls.append({
            "key": key, "name": name, "section": "agents",
            "value_effective": int(value) if value is not None else None,
            "value_stored": value, "default": None,
            "source": "pace" if pace and value == pace.get(key) else "default",
            "unit": "count", "bounds": None, "editable_by": "pace", "write": None,
            "enforced": {"word": word, "effect_key": effect}, "evidence": None,
            "advisory": False, "note": None, "version": budget_version,
        })

    utilization = float(status.get("utilization_ratio") or 0)
    thresholds = [
        {"key": key, "label": label,
         "pct": float(status.get(key) if status.get(key) is not None else policy.get(key) or 0),
         "passed": bool(status.get("cap_usd_micros")) and utilization >= float(
             status.get(key) if status.get(key) is not None else 1)}
        for key, label in THRESHOLDS
    ]
    return {
        "project_id": project_id,
        "meter": meter(status),
        "controls": controls,
        "thresholds": thresholds,
        "token_guides": token_guides(connection, project_id),
        "pace": {"value": intensity, "envelope": pace},
        "versions": {"budget": budget_version},
    }


def meter(status: dict[str, Any]) -> dict[str, Any]:
    """The meter: the binding period's money and holds against its cap, the other period
    beneath it, and the subscription figure that is shown and never counted."""
    periods = status.get("periods") or {}
    return {
        "period": status.get("period"),
        "state": status.get("budget_state"),
        "state_label": STATE_LABELS.get(str(status.get("budget_state")), "Unknown"),
        "legacy_financial_hold": status.get("legacy_financial_hold"),
        "legacy_provider_hold": status.get("legacy_provider_hold"),
        "cap_usd_micros": status.get("cap_usd_micros"),
        "actual_usd_micros": status.get("actual_usd_micros"),
        "reserved_usd_micros": status.get("reserved_usd_micros"),
        "committed_usd_micros": status.get("committed_usd_micros"),
        "available_usd_micros": status.get("available_usd_micros"),
        "utilization_ratio": status.get("utilization_ratio"),
        "active_reservations_count": status.get("active_reservations_count"),
        "burn_rate_usd_micros_per_hour": status.get("burn_rate_usd_micros_per_hour"),
        "burn_window_hours": status.get("burn_window_hours"),
        "forecasted_24h_usd_micros": status.get("forecasted_24h_usd_micros"),
        "time_to_cap_seconds": status.get("time_to_cap_seconds"),
        "resets_at": status.get("resets_at"),
        "resets_at_label": status.get("resets_at_label"),
        "included_in_subscription_usd_micros": int(
            status.get("included_in_subscription_usd_micros") or 0),
        "soft_cap_pct": status.get("soft_cap_pct"),
        "auto_lite_mode_pct": status.get("auto_lite_mode_pct"),
        "freeze_provisioning_pct": status.get("freeze_provisioning_pct"),
        "freeze_tier2_pct": status.get("freeze_tier2_pct"),
        "tier2_frozen": status.get("tier2_frozen"),
        "top_cost_drivers": status.get("top_cost_drivers") or [],
        "periods": [
            {k: p.get(k) for k in ("period", "cap_usd_micros", "actual_usd_micros",
                                   "reserved_usd_micros", "committed_usd_micros",
                                   "utilization_ratio", "budget_state", "resets_at",
                                   "resets_at_label")}
            | {"state_label": STATE_LABELS.get(str(p.get("budget_state")), "Unknown")}
            for p in periods.values()
        ],
    }


def token_guides(connection: Connection, project_id: str) -> dict[str, Any]:
    """The token policies — ADVISORY: they shape alerts and tips, and no model call reads them
    (test_token_guides_stay_advisory)."""
    from . import token_efficiency as te

    listed = te.list_policies(connection, project_id)
    policies = listed.get("policies") or []
    default = next((p for p in policies if p.get("stage") == te.PROJECT_DEFAULT_STAGE), None)
    per_step = [p for p in policies if p.get("stage") != te.PROJECT_DEFAULT_STAGE]
    return {
        "advisory": True,
        "enforced": {"word": ENFORCED["token_default"][0],
                     "effect_key": ENFORCED["token_default"][1]},
        "default": default,
        "per_step": per_step,
        "schema_ready": listed.get("schema_ready", True),
    }


# ── Spend: where the money went, one window for every figure ────────────────────────────────

WINDOWS = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30}


def daily_spend(connection: Connection, project_id: str, *, days: int = 30,
                now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Money per UTC day — the SAME definition `period_spend` uses for every cap (MONEY_SQL:
    a subscription call is $0) — plus what subscriptions ran, at list price, beside it."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    end = budget_period("day", now)["end"]
    start = end - timedelta(days=int(days))
    rows = connection.execute(text(f"""
        SELECT date_trunc('day', created_at AT TIME ZONE 'UTC') AS day,
               COALESCE(SUM({MONEY_SQL}), 0) AS money,
               COALESCE(SUM(CASE WHEN COALESCE(included_in_subscription, false)
                                 THEN {INCLUDED_SQL} ELSE 0 END), 0) AS included
        FROM cost_receipts
        WHERE project_id = :p AND created_at >= :start AND created_at < :end
        GROUP BY 1"""), {"p": project_id, "start": start, "end": end}).mappings().all()
    by_day = {r["day"].date(): (int(r["money"] or 0), int(r["included"] or 0)) for r in rows}
    out = []
    for offset in range(int(days)):
        day = (start + timedelta(days=offset)).date()
        money, included = by_day.get(day, (0, 0))
        out.append({"day": day.isoformat(), "money_usd_micros": money,
                    "included_usd_micros": included})
    return out


def cap_preview(connection: Connection, project_id: str, *, period: str, cap_usd_micros: int,
                days: int = 30, now: Optional[datetime] = None) -> dict[str, Any]:
    """Replay the last 30 days of REAL spend against a proposed cap: on how many days would it
    have stopped work? A day cap is compared with each day's money; a month cap with the month's
    money so far, day by day. Read-only."""
    if period not in ("day", "month"):
        raise ValueError("period must be day or month.")
    if int(cap_usd_micros) <= 0:
        raise ValueError("cap must be more than $0.")
    series = daily_spend(connection, project_id, days=days, now=now)
    spent_days = sum(1 for d in series if d["money_usd_micros"] > 0)
    stopped = []
    if period == "day":
        stopped = [d["day"] for d in series if d["money_usd_micros"] > cap_usd_micros]
    else:
        # A month cap counts from the 1st, as the governor does: the window's first month
        # starts with the money spent before the window opened (D6 — it restarted at zero).
        first = datetime.fromisoformat(series[0]["day"]).date()
        lead = (first - first.replace(day=1)).days
        history = daily_spend(connection, project_id, days=days + lead, now=now) if lead else series
        in_window = {d["day"] for d in series}
        running, month = 0, None
        for day in history:
            if day["day"][:7] != month:
                month, running = day["day"][:7], 0
            running += day["money_usd_micros"]
            if day["day"] in in_window and running > cap_usd_micros:
                stopped.append(day["day"])
    return {"period": period, "cap_usd_micros": int(cap_usd_micros),
            "days_considered": len(series), "days_with_spend": spent_days,
            "days_stopped": len(stopped), "stopped_days": stopped,
            "not_enough_history": spent_days < 2, "daily": series}


def spend(connection: Connection, settings: Any, project_id: str, *, window: str = "7d",
          now: Optional[datetime] = None) -> dict[str, Any]:
    """The Spend tab: daily money (30 days), cache, scorecard, savings and drivers over ONE
    window, so the figures stop disagreeing. Money excludes subscription calls; what those ran
    is reported beside it — the totals, by agent and by model."""
    from . import token_efficiency as te
    from .cost_insights import build_insights

    if window not in WINDOWS:
        raise ValueError("window must be 24h, 7d or 30d.")
    hours = WINDOWS[window]
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    totals = connection.execute(text(f"""
        SELECT COALESCE(SUM({MONEY_SQL}), 0) AS money,
               COALESCE(SUM(CASE WHEN COALESCE(included_in_subscription, false)
                                 THEN {INCLUDED_SQL} ELSE 0 END), 0) AS included
        FROM cost_receipts WHERE project_id = :p AND created_at >= :s"""),
        {"p": project_id, "s": since}).mappings().one()
    drivers = [
        {"id": r["skill_or_model_id"], "amount_usd_micros": int(r["amount"] or 0)}
        for r in connection.execute(text(f"""
            SELECT skill_or_model_id, COALESCE(SUM({MONEY_SQL}), 0) AS amount
            FROM cost_receipts
            WHERE project_id = :p AND created_at >= :s
              AND NOT COALESCE(included_in_subscription, false)
            GROUP BY 1 HAVING COALESCE(SUM({MONEY_SQL}), 0) > 0
            ORDER BY 2 DESC, 1 LIMIT 5"""), {"p": project_id, "s": since}).mappings()
    ]
    te.ensure_tables(connection)
    scorecard = te.build_scorecard(connection, project_id, window_hours=hours)
    savings: dict[str, dict[str, Any]] = {}
    for insight in (build_insights(connection, project_id).get("insights") or []):
        savings.setdefault(str(insight.get("kind")), {**insight, "from": "insight"})
    for rec in (te.build_recommendations(connection, project_id).get("recommendations") or []):
        savings.setdefault(str(rec.get("kind")), {**rec, "from": "tokens"})
    status = BudgetGovernor(settings).status_for_project(connection, project_id, now=now)
    return {
        "project_id": project_id, "window": window,
        # Model spend by agent and by model, over the same window: money (subscription calls
        # are $0) with what subscriptions ran beside it as `included` — the old card summed the
        # two at list price and called it spend.
        "by_agent": _model_spend(connection, project_id, since, "role"),
        "by_model": _model_spend(connection, project_id, since, "model"),
        "money_usd_micros": int(totals["money"] or 0),
        "included_usd_micros": int(totals["included"] or 0),
        "daily": daily_spend(connection, project_id, now=now),
        "daily_cap_usd_micros": int(((status.get("periods") or {}).get("day") or {})
                                    .get("cap_usd_micros") or 0),
        "scorecard": scorecard,
        "savings": list(savings.values()),
        "top_drivers": drivers,
        "spent_vs_cap": meter(status),
    }


def _model_spend(connection: Connection, project_id: str, since: datetime,
                 column: str) -> list[dict[str, Any]]:
    """llm_calls grouped by role or model since `since`: calls, money, included. Top 12."""
    assert column in ("role", "model")
    return [
        {"id": str(r["id"]), "calls": int(r["calls"]), "money_usd_micros": int(r["money"] or 0),
         "included_usd_micros": int(r["included"] or 0)}
        for r in connection.execute(text(f"""
            SELECT COALESCE(NULLIF(TRIM({column}), ''), 'unknown') AS id, COUNT(*) AS calls,
                   COALESCE(SUM(CASE WHEN COALESCE(included_in_subscription, false) THEN 0
                                     ELSE cost_usd_micros END), 0) AS money,
                   COALESCE(SUM(CASE WHEN COALESCE(included_in_subscription, false)
                                     THEN cost_usd_micros ELSE 0 END), 0) AS included
            FROM llm_calls WHERE project_id = :p AND created_at >= :s
            GROUP BY 1 ORDER BY 3 DESC, 4 DESC, 1 LIMIT 12"""),
            {"p": project_id, "s": since}).mappings()
    ]


# ── Envelopes ────────────────────────────────────────────────────────────────────────────────

def envelopes(connection: Connection, engine: Any, settings: Any,
              project_id: str) -> dict[str, Any]:
    """Allocations as a tree, the FinOps summary with its honesty states kept as state keys,
    the reconciliations, and the initiative rule as SERVED (never hard-coded in the console)."""
    from .finops_reconciler import (
        INITIATIVE_APPROVAL_THRESHOLD_USD_MICROS,
        FinopsReconcilerService,
    )

    allocations = BudgetGovernor(settings).list_allocations(connection, project_id)
    finops = FinopsReconcilerService(settings)
    summary: Optional[dict[str, Any]]
    try:
        summary = finops.build_summary(engine, project_id, days=30)
    except Exception:  # noqa: BLE001 — "not loaded" is a state the screen says, never $0
        summary = None
    if summary is None:
        state = "not_loaded"
    elif not (summary.get("spend_by_provider") or []):
        state = "no_events"
    else:
        state = "recorded"
    return {
        "project_id": project_id,
        "allocations": [_jsonable(a) for a in allocations],
        "finops": {"state": state, "summary": _jsonable(summary) if summary else None},
        "reconciliations": [_jsonable(r) for r in finops.list_reconciliations(
            engine, project_id, 20)],
        "initiative_rule": {"tier_min": 2,
                            "amount_usd_micros": INITIATIVE_APPROVAL_THRESHOLD_USD_MICROS},
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value
