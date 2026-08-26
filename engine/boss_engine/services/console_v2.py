"""The data behind Console v2, shaped the way the screens actually read it.

WHY THIS IS ONE MODULE AND NOT EIGHTY-TWO FEEDS
-----------------------------------------------
The v1 console fetches 82 endpoints per render and shows nothing until the slowest answers.
Measured on goldeneye: TTFB 24s on the overview area, and 13.2s of it was a single audit
export that only one area displayed. Rendering all fifty areas cost 1ms — the waiting was
everything.

So v2 does the opposite: one screen, one call, returning exactly what that screen renders.
The budget is TTFB <= 2s and it is enforced by a test, not by intention.

THE TRANSLATION
---------------
The artifact talks about a company employing agents on projects. BOSS's nouns are different,
so the mapping is written down here once rather than being re-derived in each screen:

    artifact          BOSS
    ---------------   ---------------------------------------------------
    company           org / project (the console's current scope)
    project           an APP — the lineage of code-task runs that build one thing
    stage             DISCOVERY -> BUILD -> REVIEW -> SHIPPED
    lead              the agent that owns the app
    steps             turns recorded across the lineage
    spend             sum of the lineage's llm cost
    gate              a pending decision

An app is a LINEAGE, not a run. That distinction has already caused real damage once: asked
to deploy an app, BOSS judged deployability from a single cancelled run and refused, while a
completed run sat in the same lineage. Everything here groups by lineage for that reason.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text as sql

# Stage vocabulary, in order. Progress is derived from position, so the order is meaningful.
STAGES = ("DISCOVERY", "BUILD", "REVIEW", "SHIPPED")
STAGE_PCT = {"DISCOVERY": 22, "BUILD": 64, "REVIEW": 41, "SHIPPED": 100}

# Status vocabulary the v2 screens use. Deliberately NOT BOSS's raw run statuses: the artifact
# speaks in terms an operator recognises, and mapping once here keeps that consistent.
RUNNING, GATED, DONE, STUCK, IDLE = "RUNNING", "GATED", "DONE", "STUCK", "IDLE"

_DONE_STATES = ("completed", "succeeded", "passed")
_FAILED_STATES = ("failed", "error")


def _lineage_key(row: dict) -> str:
    """Which app a run belongs to.

    finalize_ctx.root_run_id is the real lineage marker; app_id is the newer one; a run with
    neither is its own app. Falling back to the title would merge unrelated apps that happen
    to share a prefix, which is how one app's deployment once appeared on another's card.
    """
    ctx = row.get("finalize_ctx") or {}
    return str(ctx.get("app_id") or ctx.get("root_run_id") or row["id"])


def _clean_title(raw: str) -> str:
    """A product name, never a document type.

    A card once read "Product Requirements Document" — what the artifact IS, not what the
    product is called. Strip the decoration and the boilerplate; if nothing survives, say so
    rather than labelling every app with the name of a document.
    """
    import re

    t = (raw or "").strip()
    t = re.sub(r"^[\s\W_]*(?:\ud83d[\udcc4-\udcdd]|\U0001F4C4|\U0001F4DD)?[\s\W_]*", "", t)
    t = re.sub(r"^(?:PRD|Product Requirements Document|Spec|Specification)\s*[:\-–—]\s*", "",
               t, flags=re.I)
    t = re.sub(r"\s*[-–—]\s*(?:PRD|Product Requirements Document)\b.*$", "", t, flags=re.I)
    t = t.split("\n")[0].strip()
    if re.fullmatch(r"(?:product requirements document|prd|spec|specification|design|overview)"
                    r"[\s:–—-]*", t, flags=re.I):
        return ""
    return t[:80]


def _stage_of(runs: list[dict]) -> tuple[str, str]:
    """(stage, status) for an app, from what EXISTS rather than a status column.

    Derived from artefacts because a status field can be stale while the work has moved on —
    a deployment run means it shipped, a PR means it is in review, a code run means it is
    building. The furthest-along signal wins.
    """
    any_pr = any(r.get("pull_request_url") for r in runs)
    any_running = any(str(r.get("status")) == "running" for r in runs)
    any_done = any(str(r.get("status")) in _DONE_STATES for r in runs)
    all_failed = runs and all(str(r.get("status")) in _FAILED_STATES for r in runs)

    if any_done and any_pr:
        return "SHIPPED", DONE
    if any_pr:
        return "REVIEW", GATED if not any_running else RUNNING
    if any_running:
        return "BUILD", RUNNING
    if all_failed:
        return "BUILD", STUCK
    if any_done:
        return "BUILD", DONE
    return "DISCOVERY", IDLE


def company_overview(connection, project_id: str, *, limit: int = 40) -> dict[str, Any]:
    """Everything the v2 shell and Projects screen render, in one query set."""
    rows = [dict(r) for r in connection.execute(
        sql(
            """
            SELECT id, title, status, phase, spec, usage, finalize_ctx,
                   pull_request_url, pull_request_number, created_at, updated_at
            FROM code_task_runs
            WHERE project_id = :p
              AND created_at > now() - interval '90 days'
            ORDER BY created_at DESC
            LIMIT 400
            """
        ),
        {"p": project_id},
    ).mappings().all()]

    # Group into apps (lineages), newest first by most recent activity.
    apps: dict[str, list[dict]] = {}
    for row in rows:
        apps.setdefault(_lineage_key(row), []).append(row)

    projects = []
    for key, runs in apps.items():
        runs.sort(key=lambda r: r["created_at"], reverse=True)
        title = ""
        for r in runs:                       # first run whose title survives cleaning
            title = _clean_title(str(r.get("title") or ""))
            if title:
                break
        stage, status = _stage_of(runs)
        micros = sum(int((r.get("usage") or {}).get("cost_usd_micros") or 0) for r in runs)
        steps = sum(int((r.get("state") or {}).get("turns") or 0) for r in runs) or len(runs)
        spec = runs[0].get("spec") or {}
        projects.append({
            "id": key,
            "title": title or "Untitled app",
            "description": str(spec.get("instructions") or "")[:160],
            "status": status,
            "stage": stage,
            "pct": STAGE_PCT.get(stage, 0),
            "runs": len(runs),
            "steps": steps,
            "spend_usd": round(micros / 1_000_000, 2),
            "repo": f"{spec.get('owner') or '?'}/{spec.get('repo') or '?'}",
            "latest_run_id": runs[0]["id"],
            "pull_request_number": next(
                (r.get("pull_request_number") for r in runs if r.get("pull_request_number")), None),
            "updated_at": runs[0]["updated_at"].isoformat() if runs[0].get("updated_at") else None,
        })

    projects.sort(key=lambda p: p["updated_at"] or "", reverse=True)
    projects = projects[:limit]

    gates = int(connection.execute(
        sql("SELECT count(*) FROM decisions WHERE project_id = :p AND status = 'pending'"),
        {"p": project_id}).scalar() or 0)

    spend_micros = int(connection.execute(
        sql("SELECT COALESCE(sum(cost_usd_micros), 0) FROM llm_calls "
            "WHERE project_id = :p AND created_at > date_trunc('month', now())"),
        {"p": project_id}).scalar() or 0)

    return {
        "company": {"id": project_id, "name": project_id},
        "burn": {"spent_usd": round(spend_micros / 1_000_000, 2), "cap_usd": None},
        "gates": gates,
        "projects": projects,
        # Autonomy is a real setting in the artifact, not decoration. Reported honestly as the
        # only mode BOSS actually implements today rather than faking a switch that does
        # nothing — see CONSOLE_V2_MIGRATION.md §5.
        "autonomy": {"mode": "Supervised",
                     "detail": "Spend & deploys pause for you.",
                     "modes_available": ["Supervised"]},
    }
