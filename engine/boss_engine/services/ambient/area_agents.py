"""Goal-owning area agents: goal, measured milestones, self-recorded readings, heartbeat,
and weekly reports with ambient digest (BOSSAPP-936; parent BOSSAPP-934; absorbs BOSSAPP-907;
relates to BOSSAPP-830).

Each project's six area agents (Shepherd, SRE, Security, FinOps, Release, Infra) own a durable
goal linked to the canonical ``goals`` tree, measurable milestones, append-only readings they
record themselves, periodic heartbeats, and weekly reports (including the ambient A0/resolved
digest) rendered for both the project page and chat.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ._common import clean_text, emit, iso, new_id, now, serialize_row


@dataclass(frozen=True)
class AreaAgentSpec:
    key: str
    title: str
    role_key: str
    area: str
    default_goal_title: str
    default_goal_description: str
    task_classes: tuple[str, ...]
    default_milestones: tuple[dict[str, Any], ...]


AREA_AGENTS: dict[str, AreaAgentSpec] = {
    s.key: s
    for s in (
        AreaAgentSpec(
            key="shepherd",
            title="Shepherd",
            role_key="chief_of_staff",
            area="Delivery & Stalled Work",
            default_goal_title="Keep work flowing with zero silently stalled builds or handoffs",
            default_goal_description=(
                "Watch open builds, PRs, and ambient handoffs; nudge or unstick stalled work "
                "before a person has to chase it."
            ),
            task_classes=("stale_handoff", "workflow_failure", "ci_fix_boss_pr", "todo_triage"),
            default_milestones=(
                {
                    "title": "Stalled handoffs resolved within 2 hours",
                    "metric_key": "stalled_handoff_median_minutes",
                    "unit": "minutes",
                    "target_value": 120.0,
                    "comparator": "lte",
                },
                {
                    "title": "Unanswered agent escalations kept at zero",
                    "metric_key": "stale_escalations_open",
                    "unit": "items",
                    "target_value": 0.0,
                    "comparator": "lte",
                },
            ),
        ),
        AreaAgentSpec(
            key="sre",
            title="SRE",
            role_key="sre",
            area="Reliability & Incidents",
            default_goal_title="Keep staging and production healthy with fast reversible mitigation",
            default_goal_description=(
                "Detect failing health checks, red main, and perf regressions early; apply "
                "reversible staging mitigations within policy."
            ),
            task_classes=("incident_response", "incident_staging_mitigation", "red_main", "perf_regression"),
            default_milestones=(
                {
                    "title": "Mean time to mitigate incidents under 15 minutes",
                    "metric_key": "mttm_minutes",
                    "unit": "minutes",
                    "target_value": 15.0,
                    "comparator": "lte",
                },
                {
                    "title": "Zero unacknowledged high-severity alerts",
                    "metric_key": "unacked_high_alerts",
                    "unit": "alerts",
                    "target_value": 0.0,
                    "comparator": "lte",
                },
            ),
        ),
        AreaAgentSpec(
            key="security",
            title="Security",
            role_key="security_reviewer",
            area="Security & Advisories",
            default_goal_title="Patch high-severity advisories quickly without leaking secrets",
            default_goal_description=(
                "Triage dependency CVEs and code security findings; open verified draft patch PRs "
                "while enforcing secret boundaries."
            ),
            task_classes=("security_patch", "dependency_upgrade"),
            default_milestones=(
                {
                    "title": "Zero open critical/high CVEs older than 72 hours",
                    "metric_key": "open_high_cves_over_72h",
                    "unit": "advisories",
                    "target_value": 0.0,
                    "comparator": "lte",
                },
            ),
        ),
        AreaAgentSpec(
            key="finops",
            title="FinOps",
            role_key="finops_sentinel",
            area="Budget & Token Efficiency",
            default_goal_title="Keep ambient and build spend predictable inside daily caps",
            default_goal_description=(
                "Monitor cost anomalies, model routing efficiency, and ambient envelope headroom."
            ),
            task_classes=("cost_anomaly",),
            default_milestones=(
                {
                    "title": "Daily ambient spend stays within 85% of envelope cap",
                    "metric_key": "ambient_envelope_utilization_pct",
                    "unit": "percent",
                    "target_value": 85.0,
                    "comparator": "lte",
                },
            ),
        ),
        AreaAgentSpec(
            key="release",
            title="Release",
            role_key="release_manager",
            area="Releases & PR Hygiene",
            default_goal_title="Keep every release candidate verified, documented, and ready for review",
            default_goal_description=(
                "Track release notes, flaky tests, and PR readiness so human merge gates are fast."
            ),
            task_classes=("release_notes", "flaky_test", "missing_tests", "doc_drift"),
            default_milestones=(
                {
                    "title": "Flaky tests quarantined or fixed within 24 hours",
                    "metric_key": "open_flaky_tests",
                    "unit": "tests",
                    "target_value": 0.0,
                    "comparator": "lte",
                },
            ),
        ),
        AreaAgentSpec(
            key="infra",
            title="Infra",
            role_key="infra_steward",
            area="Infrastructure & System Health",
            default_goal_title="Keep hosts, sandboxes, and IaC drift-free and healthy",
            default_goal_description=(
                "Monitor host doctor checks, disk/memory headroom, and IaC drift; surface drift "
                "early with reversible fixes."
            ),
            task_classes=("system_health", "iac_drift", "ci_setup"),
            default_milestones=(
                {
                    "title": "System doctor checks passing",
                    "metric_key": "doctor_failing_checks",
                    "unit": "checks",
                    "target_value": 0.0,
                    "comparator": "lte",
                },
            ),
        ),
    )
}

VALID_COMPARATORS = frozenset({"lte", "gte", "eq"})
VALID_HEARTBEAT_STATUSES = frozenset({"ok", "attention", "degraded", "blocked"})
VALID_GOAL_STATUSES = frozenset({"active", "achieved", "paused", "dropped"})
VALID_PERIODS = frozenset({"weekly", "daily_digest"})


def area_spec(area_key: str) -> AreaAgentSpec:
    key = str(area_key or "").strip().lower()
    spec = AREA_AGENTS.get(key)
    if spec is None:
        raise ValueError(
            f"unknown area agent {area_key!r} (expected one of {', '.join(AREA_AGENTS)})"
        )
    return spec


def _evaluate_milestone_status(
    value: float,
    target: float,
    comparator: str,
    baseline: Optional[float] = None,
) -> str:
    if comparator == "lte":
        if value <= target:
            return "achieved"
        if baseline is not None and value < baseline:
            return "on_track"
        return "at_risk"
    if comparator == "gte":
        if value >= target:
            return "achieved"
        if baseline is not None and value > baseline:
            return "on_track"
        return "at_risk"
    # eq
    if abs(value - target) <= 1e-9:
        return "achieved"
    return "on_track" if baseline is not None and abs(value - target) < abs(baseline - target) else "at_risk"


def _ensure_linked_goal(
    connection: Connection,
    project_id: str,
    *,
    title: str,
    description: str,
    owner: str,
    actor: str,
) -> Optional[str]:
    """Create a canonical row in ``goals`` (BOSSAPP-830) if the table is present."""
    try:
        with connection.begin_nested():
            goal_id = new_id("goal")
            connection.execute(
                text(
                    "INSERT INTO goals (id, project_id, title, description, status, owner, created_by) "
                    "VALUES (:id, :p, :t, :d, 'active', :o, :a)"
                ),
                {
                    "id": goal_id,
                    "p": project_id,
                    "t": clean_text(title, 200),
                    "d": clean_text(description, 1000),
                    "o": owner,
                    "a": actor,
                },
            )
            return goal_id
    except Exception:  # noqa: BLE001 — installs without goals table still persist area_agent_goals
        return None


def create_area_goal(
    connection: Connection,
    project_id: str,
    area_key: str,
    *,
    title: str,
    description: str = "",
    cadence_hours: int = 24,
    milestones: Optional[list[Mapping[str, Any]]] = None,
    actor: str = "boss",
) -> dict[str, Any]:
    spec = area_spec(area_key)
    cadence = max(1, min(int(cadence_hours), 24 * 30))
    linked_goal_id = _ensure_linked_goal(
        connection,
        project_id,
        title=title,
        description=description,
        owner=f"agent:{spec.role_key}",
        actor=actor,
    )
    ag_id = new_id("aag")
    next_hb = now() + timedelta(hours=cadence)
    connection.execute(
        text(
            "INSERT INTO area_agent_goals "
            "(id, project_id, area_key, role_key, goal_id, title, description, status, "
            "cadence_hours, next_heartbeat_at, created_by) "
            "VALUES (:id, :p, :ak, :rk, :gid, :t, :d, 'active', :ch, :nh, :a)"
        ),
        {
            "id": ag_id,
            "p": project_id,
            "ak": spec.key,
            "rk": spec.role_key,
            "gid": linked_goal_id,
            "t": clean_text(title, 240),
            "d": clean_text(description, 1200),
            "ch": cadence,
            "nh": next_hb,
            "a": actor,
        },
    )
    created_milestones: list[dict[str, Any]] = []
    for m in milestones or []:
        created_milestones.append(
            add_milestone(
                connection,
                project_id,
                ag_id,
                title=str(m["title"]),
                metric_key=str(m["metric_key"]),
                target_value=float(m["target_value"]),
                unit=str(m.get("unit") or ""),
                comparator=str(m.get("comparator") or "lte"),
                baseline_value=float(m["baseline_value"]) if m.get("baseline_value") is not None else None,
                due_at=m.get("due_at"),
            )
        )
    emit(
        connection,
        project_id,
        "ambient.area_goal_created",
        actor,
        "area_agent_goal",
        ag_id,
        {"area_key": spec.key, "role_key": spec.role_key, "goal_id": linked_goal_id, "title": title},
    )
    row = connection.execute(
        text("SELECT * FROM area_agent_goals WHERE id = :id"), {"id": ag_id}
    ).mappings().first()
    out = serialize_row(row)
    out["milestones"] = created_milestones
    return out


def add_milestone(
    connection: Connection,
    project_id: str,
    area_goal_id: str,
    *,
    title: str,
    metric_key: str,
    target_value: float,
    unit: str = "",
    comparator: str = "lte",
    baseline_value: Optional[float] = None,
    due_at: Optional[Any] = None,
) -> dict[str, Any]:
    comp = str(comparator or "lte").strip().lower()
    if comp not in VALID_COMPARATORS:
        raise ValueError(f"comparator must be one of {sorted(VALID_COMPARATORS)}")
    goal = connection.execute(
        text("SELECT id, project_id FROM area_agent_goals WHERE id = :id AND project_id = :p"),
        {"id": area_goal_id, "p": project_id},
    ).mappings().first()
    if goal is None:
        raise LookupError(f"no area agent goal {area_goal_id} in project {project_id}")
    ms_id = new_id("aam")
    connection.execute(
        text(
            "INSERT INTO area_agent_milestones "
            "(id, area_goal_id, project_id, title, metric_key, unit, target_value, "
            "baseline_value, current_value, comparator, status, due_at) "
            "VALUES (:id, :ag, :p, :t, :mk, :u, :tv, :bv, :bv, :c, 'pending', :due)"
        ),
        {
            "id": ms_id,
            "ag": area_goal_id,
            "p": project_id,
            "t": clean_text(title, 240),
            "mk": clean_text(metric_key, 120),
            "u": clean_text(unit, 40),
            "tv": float(target_value),
            "bv": float(baseline_value) if baseline_value is not None else None,
            "c": comp,
            "due": due_at,
        },
    )
    row = connection.execute(
        text("SELECT * FROM area_agent_milestones WHERE id = :id"), {"id": ms_id}
    ).mappings().first()
    return serialize_row(row)


def seed_default_area_goals(
    connection: Connection,
    project_id: str,
    *,
    actor: str = "boss",
) -> list[dict[str, Any]]:
    """Idempotently seed goals and measured milestones for all six area agents."""
    existing = {
        str(r["area_key"]): str(r["id"])
        for r in connection.execute(
            text(
                "SELECT id, area_key FROM area_agent_goals "
                "WHERE project_id = :p AND status = 'active'"
            ),
            {"p": project_id},
        ).mappings()
    }
    created: list[dict[str, Any]] = []
    for key, spec in AREA_AGENTS.items():
        if key in existing:
            continue
        created.append(
            create_area_goal(
                connection,
                project_id,
                key,
                title=spec.default_goal_title,
                description=spec.default_goal_description,
                milestones=list(spec.default_milestones),
                actor=actor,
            )
        )
    return created


def record_reading(
    connection: Connection,
    project_id: str,
    milestone_id: str,
    *,
    value: float,
    recorded_by: Optional[str] = None,
    note: str = "",
    evidence: Optional[list[Mapping[str, Any]]] = None,
) -> dict[str, Any]:
    """Record a self-collected reading on a milestone and recompute milestone + goal progress."""
    ms = connection.execute(
        text(
            "SELECT m.*, g.area_key, g.role_key FROM area_agent_milestones m "
            "JOIN area_agent_goals g ON g.id = m.area_goal_id "
            "WHERE m.id = :id AND m.project_id = :p"
        ),
        {"id": milestone_id, "p": project_id},
    ).mappings().first()
    if ms is None:
        raise LookupError(f"no milestone {milestone_id} in project {project_id}")
    val = float(value)
    actor = recorded_by or f"agent:{ms['role_key']}"
    reading_id = new_id("aar")
    connection.execute(
        text(
            "INSERT INTO area_agent_readings "
            "(id, milestone_id, area_goal_id, project_id, area_key, value, note, evidence, recorded_by) "
            "VALUES (:id, :m, :ag, :p, :ak, :v, :n, CAST(:e AS JSONB), :rb)"
        ),
        {
            "id": reading_id,
            "m": milestone_id,
            "ag": ms["area_goal_id"],
            "p": project_id,
            "ak": ms["area_key"],
            "v": val,
            "n": clean_text(note, 500),
            "e": json.dumps(list(evidence or [])),
            "rb": actor,
        },
    )
    prev_baseline = float(ms["baseline_value"]) if ms["baseline_value"] is not None else None
    new_status = _evaluate_milestone_status(
        val, float(ms["target_value"]), str(ms["comparator"]), baseline=prev_baseline
    )
    baseline = prev_baseline if prev_baseline is not None else val
    achieved_at = now() if new_status == "achieved" and ms["achieved_at"] is None else ms["achieved_at"]
    connection.execute(
        text(
            "UPDATE area_agent_milestones SET baseline_value = COALESCE(baseline_value, :bv), "
            "current_value = :v, status = :st, achieved_at = :aa, updated_at = now() "
            "WHERE id = :id"
        ),
        {"bv": baseline, "v": val, "st": new_status, "aa": achieved_at, "id": milestone_id},
    )
    emit(
        connection,
        project_id,
        "ambient.area_reading_recorded",
        actor,
        "area_agent_milestone",
        milestone_id,
        {
            "reading_id": reading_id,
            "area_key": ms["area_key"],
            "metric_key": ms["metric_key"],
            "value": val,
            "target_value": float(ms["target_value"]),
            "status": new_status,
        },
    )
    row = connection.execute(
        text("SELECT * FROM area_agent_readings WHERE id = :id"), {"id": reading_id}
    ).mappings().first()
    updated_ms = connection.execute(
        text("SELECT * FROM area_agent_milestones WHERE id = :id"), {"id": milestone_id}
    ).mappings().first()
    return {"reading": serialize_row(row), "milestone": serialize_row(updated_ms)}


def _open_items_for_area(connection: Connection, project_id: str, spec: AreaAgentSpec) -> int:
    return int(
        connection.execute(
            text(
                "SELECT count(*) FROM work_items WHERE project_id = :p AND ambient "
                "AND task_class = ANY(:tc) AND lifecycle IN ('proposed', 'claimed', 'in_progress', 'needs_you')"
            ),
            {"p": project_id, "tc": list(spec.task_classes)},
        ).scalar()
        or 0
    )


def run_heartbeat(
    connection: Connection,
    project_id: str,
    area_key: str,
    *,
    actor: Optional[str] = None,
    readings: Optional[list[Mapping[str, Any]]] = None,
    status: Optional[str] = None,
    summary: Optional[str] = None,
    proposed_actions: Optional[list[Mapping[str, Any]]] = None,
) -> dict[str, Any]:
    """Run one area agent's heartbeat: record self-collected readings, evaluate milestones,
    persist heartbeat row, and emit an audit event."""
    spec = area_spec(area_key)
    goal = connection.execute(
        text(
            "SELECT * FROM area_agent_goals WHERE project_id = :p AND area_key = :ak "
            "AND status = 'active' ORDER BY created_at DESC LIMIT 1"
        ),
        {"p": project_id, "ak": spec.key},
    ).mappings().first()
    if goal is None:
        seeded = create_area_goal(
            connection,
            project_id,
            spec.key,
            title=spec.default_goal_title,
            description=spec.default_goal_description,
            milestones=list(spec.default_milestones),
            actor=actor or f"agent:{spec.role_key}",
        )
        goal = connection.execute(
            text("SELECT * FROM area_agent_goals WHERE id = :id"), {"id": seeded["id"]}
        ).mappings().first()

    who = actor or f"agent:{spec.role_key}"
    ms_rows = connection.execute(
        text("SELECT * FROM area_agent_milestones WHERE area_goal_id = :ag ORDER BY created_at"),
        {"ag": goal["id"]},
    ).mappings().all()
    by_metric = {str(m["metric_key"]): str(m["id"]) for m in ms_rows}
    by_id = {str(m["id"]): str(m["id"]) for m in ms_rows}

    recorded: list[dict[str, Any]] = []
    for item in readings or []:
        ms_id = by_id.get(str(item.get("milestone_id") or "")) or by_metric.get(
            str(item.get("metric_key") or "")
        )
        if not ms_id:
            continue
        recorded.append(
            record_reading(
                connection,
                project_id,
                ms_id,
                value=float(item["value"]),
                recorded_by=who,
                note=str(item.get("note") or "heartbeat reading"),
                evidence=list(item.get("evidence") or []),
            )
        )

    # Reload milestones after readings
    refreshed = connection.execute(
        text("SELECT * FROM area_agent_milestones WHERE area_goal_id = :ag ORDER BY created_at"),
        {"ag": goal["id"]},
    ).mappings().all()
    at_risk = [m for m in refreshed if m["status"] == "at_risk"]
    achieved = [m for m in refreshed if m["status"] == "achieved"]
    open_items = _open_items_for_area(connection, project_id, spec)

    computed_status = status
    if computed_status is None:
        if at_risk:
            computed_status = "attention"
        elif open_items > 5:
            computed_status = "degraded"
        else:
            computed_status = "ok"
    if computed_status not in VALID_HEARTBEAT_STATUSES:
        raise ValueError(f"status must be one of {sorted(VALID_HEARTBEAT_STATUSES)}")

    hb_summary = clean_text(
        summary
        or (
            f"{spec.title} heartbeat: {len(achieved)}/{len(refreshed)} milestones achieved, "
            f"{len(at_risk)} at risk, {open_items} open ambient items."
        ),
        600,
    )
    hb_id = new_id("aah")
    ts = now()
    next_hb = ts + timedelta(hours=int(goal["cadence_hours"] or 24))
    connection.execute(
        text(
            "INSERT INTO area_agent_heartbeats "
            "(id, area_goal_id, project_id, area_key, status, summary, readings_count, "
            "open_items_count, proposed_actions, created_at) "
            "VALUES (:id, :ag, :p, :ak, :st, :sum, :rc, :oi, CAST(:pa AS JSONB), :ts)"
        ),
        {
            "id": hb_id,
            "ag": goal["id"],
            "p": project_id,
            "ak": spec.key,
            "st": computed_status,
            "sum": hb_summary,
            "rc": len(recorded),
            "oi": open_items,
            "pa": json.dumps(list(proposed_actions or [])),
            "ts": ts,
        },
    )
    connection.execute(
        text(
            "UPDATE area_agent_goals SET last_heartbeat_at = :ts, next_heartbeat_at = :nh, "
            "updated_at = now() WHERE id = :id"
        ),
        {"ts": ts, "nh": next_hb, "id": goal["id"]},
    )
    emit(
        connection,
        project_id,
        "ambient.area_heartbeat",
        who,
        "area_agent_heartbeat",
        hb_id,
        {
            "area_key": spec.key,
            "area_goal_id": goal["id"],
            "status": computed_status,
            "readings_count": len(recorded),
            "open_items_count": open_items,
        },
    )
    row = connection.execute(
        text("SELECT * FROM area_agent_heartbeats WHERE id = :id"), {"id": hb_id}
    ).mappings().first()
    return {
        "heartbeat": serialize_row(row),
        "readings": recorded,
        "milestones": [serialize_row(m) for m in refreshed],
    }


def _collect_ambient_digest(
    connection: Connection,
    project_id: str,
    since: datetime,
    task_classes: Optional[tuple[str, ...]] = None,
) -> dict[str, Any]:
    """Absorbs the BOSSAPP-907 ambient digest: observed A0 items, agent-resolved items,
    and items waiting on a person within the window."""
    params: dict[str, Any] = {"p": project_id, "s": since}
    tc_filter = ""
    if task_classes:
        tc_filter = " AND task_class = ANY(:tc)"
        params["tc"] = list(task_classes)
    rows = connection.execute(
        text(
            "SELECT id, title, task_class, lifecycle, autonomy_level, severity, resolved_by, updated_at "
            f"FROM work_items WHERE project_id = :p AND ambient AND updated_at >= :s{tc_filter} "
            "ORDER BY updated_at DESC LIMIT 100"
        ),
        params,
    ).mappings().all()
    observed = []
    resolved_by_agents = []
    needs_you = []
    for r in rows:
        entry = {
            "work_item_id": r["id"],
            "title": r["title"],
            "task_class": r["task_class"],
            "lifecycle": r["lifecycle"],
            "autonomy_level": r["autonomy_level"],
        }
        if r["lifecycle"] == "needs_you":
            needs_you.append(entry)
        elif r["lifecycle"] == "resolved" and not str(r["resolved_by"] or "").startswith("person:"):
            resolved_by_agents.append(entry)
        elif r["autonomy_level"] == "A0":
            observed.append(entry)
    return {
        "observed_a0": observed,
        "resolved_by_agents": resolved_by_agents,
        "needs_you": needs_you,
        "counts": {
            "observed_a0": len(observed),
            "resolved_by_agents": len(resolved_by_agents),
            "needs_you": len(needs_you),
            "total": len(rows),
        },
    }


def generate_weekly_report(
    connection: Connection,
    project_id: str,
    *,
    area_key: Optional[str] = None,
    period: str = "weekly",
    days: int = 7,
    actor: str = "boss",
    post_to_chat: bool = False,
) -> dict[str, Any]:
    """Compile a weekly report (or daily digest) for one area agent or the whole project crew,
    proposing follow-up goals when milestones are achieved or at risk, and optionally recording
    a deduped chat delivery receipt."""
    if period not in VALID_PERIODS:
        raise ValueError(f"period must be one of {sorted(VALID_PERIODS)}")
    spec = area_spec(area_key) if area_key else None
    end_ts = now()
    start_ts = end_ts - timedelta(days=max(1, min(int(days), 90)))

    goal_params: dict[str, Any] = {"p": project_id}
    area_clause = ""
    if spec is not None:
        area_clause = " AND area_key = :ak"
        goal_params["ak"] = spec.key
    goals = connection.execute(
        text(
            f"SELECT * FROM area_agent_goals WHERE project_id = :p AND status = 'active'{area_clause} "
            "ORDER BY area_key, created_at DESC"
        ),
        goal_params,
    ).mappings().all()

    area_sections: list[dict[str, Any]] = []
    proposed_goals: list[dict[str, Any]] = []
    total_readings = 0
    total_heartbeats = 0

    for g in goals:
        g_spec = AREA_AGENTS.get(str(g["area_key"]))
        milestones = connection.execute(
            text("SELECT * FROM area_agent_milestones WHERE area_goal_id = :ag ORDER BY created_at"),
            {"ag": g["id"]},
        ).mappings().all()
        readings_in_window = int(
            connection.execute(
                text(
                    "SELECT count(*) FROM area_agent_readings "
                    "WHERE area_goal_id = :ag AND recorded_at >= :s"
                ),
                {"ag": g["id"], "s": start_ts},
            ).scalar()
            or 0
        )
        hb_rows = connection.execute(
            text(
                "SELECT id, status, summary, created_at FROM area_agent_heartbeats "
                "WHERE area_goal_id = :ag AND created_at >= :s ORDER BY created_at DESC LIMIT 20"
            ),
            {"ag": g["id"], "s": start_ts},
        ).mappings().all()
        total_readings += readings_in_window
        total_heartbeats += len(hb_rows)

        achieved_count = sum(1 for m in milestones if m["status"] == "achieved")
        at_risk_ms = [m for m in milestones if m["status"] == "at_risk"]
        if milestones and achieved_count == len(milestones):
            proposed_goals.append(
                {
                    "area_key": g["area_key"],
                    "reason": "all_milestones_achieved",
                    "proposed_title": f"Tighten {g_spec.area if g_spec else g['area_key']} targets for next cycle",
                }
            )
        for m in at_risk_ms:
            proposed_goals.append(
                {
                    "area_key": g["area_key"],
                    "reason": "milestone_at_risk",
                    "milestone_id": m["id"],
                    "proposed_title": f"Remediate {m['title']} (current={m['current_value']}, target={m['target_value']})",
                }
            )
        area_sections.append(
            {
                "area_key": g["area_key"],
                "title": g_spec.title if g_spec else g["area_key"],
                "role_key": g["role_key"],
                "goal_title": g["title"],
                "milestones": [serialize_row(m) for m in milestones],
                "achieved_milestones": achieved_count,
                "total_milestones": len(milestones),
                "readings_count": readings_in_window,
                "heartbeats_count": len(hb_rows),
                "latest_heartbeat": serialize_row(hb_rows[0]) if hb_rows else None,
            }
        )

    digest = _collect_ambient_digest(
        connection,
        project_id,
        start_ts,
        task_classes=spec.task_classes if spec else None,
    )

    heading = (
        f"## {spec.title} Weekly Report" if spec else "## Area Agents Weekly Report & Digest"
    )
    lines = [
        heading,
        f"Window: {start_ts.date().isoformat()} to {end_ts.date().isoformat()} UTC",
        "",
    ]
    for sec in area_sections:
        hb_status = (sec["latest_heartbeat"] or {}).get("status", "none")
        lines.append(
            f"- **{sec['title']}** ({sec['achieved_milestones']}/{sec['total_milestones']} milestones achieved, "
            f"{sec['readings_count']} readings, {sec['heartbeats_count']} heartbeats, latest={hb_status}): "
            f"{sec['goal_title']}"
        )
    lines.extend(
        [
            "",
            "### Ambient Digest",
            f"- Resolved by agents: {digest['counts']['resolved_by_agents']}",
            f"- Observed (A0 digest): {digest['counts']['observed_a0']}",
            f"- Waiting on a person (Needs you): {digest['counts']['needs_you']}",
        ]
    )
    if proposed_goals:
        lines.extend(["", "### Proposed Next Goals"])
        for pg in proposed_goals:
            lines.append(f"- [{pg['area_key']}] {pg['proposed_title']} ({pg['reason']})")
    markdown = "\n".join(lines)
    summary = clean_text(
        f"{len(area_sections)} area agents reported ({total_heartbeats} heartbeats, "
        f"{total_readings} readings; digest: {digest['counts']['resolved_by_agents']} resolved, "
        f"{digest['counts']['observed_a0']} observed, {digest['counts']['needs_you']} needs-you).",
        600,
    )

    report_id = new_id("aarpt")
    chat_ref: Optional[str] = None
    posted = False
    if post_to_chat:
        chat_ref = _record_chat_delivery_receipt(
            connection,
            project_id=project_id,
            report_id=report_id,
            area_key=spec.key if spec else "all",
            window_end=end_ts,
            markdown=markdown,
        )
        posted = True

    connection.execute(
        text(
            "INSERT INTO area_agent_reports "
            "(id, project_id, area_key, period, window_start, window_end, summary, markdown, "
            "metrics, proposed_goals, digest_items, posted_to_chat, chat_message_ref, created_by) "
            "VALUES (:id, :p, :ak, :per, :ws, :we, :sum, :md, CAST(:met AS JSONB), "
            "CAST(:pg AS JSONB), CAST(:dig AS JSONB), :pc, :cr, :a)"
        ),
        {
            "id": report_id,
            "p": project_id,
            "ak": spec.key if spec else "all",
            "per": period,
            "ws": start_ts,
            "we": end_ts,
            "sum": summary,
            "md": markdown,
            "met": json.dumps(
                {
                    "areas": area_sections,
                    "total_readings": total_readings,
                    "total_heartbeats": total_heartbeats,
                }
            ),
            "pg": json.dumps(proposed_goals),
            "dig": json.dumps(digest),
            "pc": posted,
            "cr": chat_ref,
            "a": actor,
        },
    )
    emit(
        connection,
        project_id,
        "ambient.area_weekly_report",
        actor,
        "area_agent_report",
        report_id,
        {
            "area_key": spec.key if spec else "all",
            "period": period,
            "posted_to_chat": posted,
            "chat_message_ref": chat_ref,
            "proposed_goals": len(proposed_goals),
        },
    )
    row = connection.execute(
        text("SELECT * FROM area_agent_reports WHERE id = :id"), {"id": report_id}
    ).mappings().first()
    return serialize_row(row)


def _record_chat_delivery_receipt(
    connection: Connection,
    *,
    project_id: str,
    report_id: str,
    area_key: str,
    window_end: datetime,
    markdown: str,
) -> str:
    """Record a governed Receipt for posting the weekly report to the project's chat feed."""
    from ..receipts import ReceiptService

    tenancy = connection.execute(
        text(
            "SELECT w.org_id, p.workspace_id FROM projects p "
            "JOIN workspaces w ON w.id = p.workspace_id WHERE p.id = :p"
        ),
        {"p": project_id},
    ).mappings().first()
    dedupe_key = f"area_report:{project_id}:{area_key}:{window_end.date().isoformat()}:{report_id}"
    if tenancy is not None:
        receipt, _ = ReceiptService().record_completed(
            connection,
            str(tenancy["org_id"]),
            str(tenancy["workspace_id"]),
            project_id,
            dedupe_key,
            "area_agent.weekly_report_chat",
            f"project:{project_id}:chat",
            {"report_id": report_id, "area_key": area_key, "markdown_chars": len(markdown)},
            provider_ref=f"chat:{project_id}:{report_id}",
        )
        return str(receipt["id"])
    return f"chat:{project_id}:{report_id}"


def list_reports(
    connection: Connection,
    project_id: str,
    *,
    area_key: Optional[str] = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"p": project_id, "n": max(1, min(int(limit), 100))}
    clause = ""
    if area_key:
        clause = " AND area_key = :ak"
        params["ak"] = str(area_key).strip().lower()
    rows = connection.execute(
        text(
            f"SELECT * FROM area_agent_reports WHERE project_id = :p{clause} "
            "ORDER BY created_at DESC LIMIT :n"
        ),
        params,
    ).mappings().all()
    return [serialize_row(r) for r in rows]


def project_overview(connection: Connection, project_id: str) -> dict[str, Any]:
    """Project-page and chat read model for all six area agents: goals, milestones,
    latest self-recorded readings, heartbeats, and weekly reports."""
    goals = connection.execute(
        text(
            "SELECT * FROM area_agent_goals WHERE project_id = :p AND status = 'active' "
            "ORDER BY area_key, created_at DESC"
        ),
        {"p": project_id},
    ).mappings().all()
    by_area: dict[str, dict[str, Any]] = {}
    for g in goals:
        ak = str(g["area_key"])
        if ak in by_area:
            continue
        milestones = connection.execute(
            text("SELECT * FROM area_agent_milestones WHERE area_goal_id = :ag ORDER BY created_at"),
            {"ag": g["id"]},
        ).mappings().all()
        recent_readings = connection.execute(
            text(
                "SELECT * FROM area_agent_readings WHERE area_goal_id = :ag "
                "ORDER BY recorded_at DESC LIMIT 10"
            ),
            {"ag": g["id"]},
        ).mappings().all()
        latest_hb = connection.execute(
            text(
                "SELECT * FROM area_agent_heartbeats WHERE area_goal_id = :ag "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"ag": g["id"]},
        ).mappings().first()
        latest_report = connection.execute(
            text(
                "SELECT * FROM area_agent_reports WHERE project_id = :p AND area_key IN (:ak, 'all') "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"p": project_id, "ak": ak},
        ).mappings().first()
        by_area[ak] = {
            "goal": serialize_row(g),
            "milestones": [serialize_row(m) for m in milestones],
            "recent_readings": [serialize_row(r) for r in recent_readings],
            "latest_heartbeat": serialize_row(latest_hb) if latest_hb else None,
            "latest_report": serialize_row(latest_report) if latest_report else None,
        }

    agents_out = []
    for key, spec in AREA_AGENTS.items():
        data = by_area.get(key)
        agents_out.append(
            {
                "area_key": spec.key,
                "title": spec.title,
                "role_key": spec.role_key,
                "area": spec.area,
                "configured": data is not None,
                **(
                    data
                    if data is not None
                    else {
                        "goal": None,
                        "milestones": [],
                        "recent_readings": [],
                        "latest_heartbeat": None,
                        "latest_report": None,
                    }
                ),
            }
        )
    return {
        "project_id": project_id,
        "area_agents": agents_out,
        "recent_reports": list_reports(connection, project_id, limit=10),
        "generated_at": iso(now()),
    }


__all__ = [
    "AREA_AGENTS",
    "AreaAgentSpec",
    "area_spec",
    "seed_default_area_goals",
    "create_area_goal",
    "add_milestone",
    "record_reading",
    "run_heartbeat",
    "generate_weekly_report",
    "list_reports",
    "project_overview",
]
