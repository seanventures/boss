"""YOUR APP: the project's app, always one press away (operator, 2026-11-19: "for the Recipe why
there are no previews showing up now? … did we remove the Preview feature").

Every preview of recipe-box had been stopped for idle ("nobody opened it for 20 min"), and the only
way back to the app was build page → Outputs → Preview → Run preview. The project's Overview had
no app card at all unless the app was deployed.

THE RULE. A project whose builds produced something previewable has an app, and every project
surface shows it: the Overview's "Your app" card, the projects list card and Command Center's
project rows ("Open app"). The card says which version it shows — **main** once a change merged,
else **the latest ready change** (a completed build with an open pull request) — and whether its
preview runs now. A preview stopped by BOSS (idle, time limit, process gone) is never "gone": it
is `asleep`, and opening it wakes it (preview_wake.py; the console's Open app starts it through
the one write path, POST /v1/code-tasks/<run>/preview).

One read, batched across projects (`cards`), so the list and Command Center pay one query each
however many projects they show. Facts on record only; never raises from `cards` (a project with
no readable app simply has none).
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

log = logging.getLogger(__name__)

#: How many recent builds per project are considered (newest first).
RECENT_BUILDS = 40

#: A preview stopped by BOSS itself (reaper, reconcile, the gateway's self-heal): it sleeps and
#: wakes on open. A person's own Stop leaves no such error, and is "stopped".
BOSS_STOPPED = "Stopped by BOSS:"

STATE_WORDS = {
    "running": "Running",
    "starting": "Starting… about a minute",
    "asleep": "Asleep · wakes when you open it",
    "stopped": "Stopped · opens again when you press Open app",
    "failed": "Its last start failed · Open app tries again",
    "none": "Not started yet · Open app starts it",
}


def preview_href(preview_id: str) -> str:
    """The console's door to a preview (lib/browser-links previewWebHref): same-origin; the API
    sends a signed-in browser on to the preview's own origin."""
    from urllib.parse import quote

    return f"/v1/previews/{quote(str(preview_id), safe='')}/web/"


def sleep_state(status: Any, error: Any) -> str:
    """running | starting | asleep | stopped | failed for one preview row."""
    st = str(status or "")
    if st in ("running", "verified"):
        return "running"
    if st == "starting":
        return "starting"
    if st == "expired" or (st == "stopped" and str(error or "").startswith(BOSS_STOPPED)):
        return "asleep"
    if st == "stopped":
        return "stopped"
    return "failed"


def _pick(rows: list[Mapping[str, Any]], shas: Mapping[str, str]) -> Optional[dict[str, Any]]:
    """Main (the newest merged build) first, else the newest ready change (PR not closed)."""
    from .. import pr_merge

    ready = None
    for r in rows:   # newest first
        decision = {"status": r.get("decision_status"),
                    "merged_sha": shas.get(str(r.get("decision_id") or ""), "")}
        state = pr_merge.state(r, decision)
        if state == pr_merge.MERGED:
            return {**dict(r), "version": "main"}
        if state != pr_merge.CLOSED and ready is None:
            ready = {**dict(r), "version": "change"}
    return ready


def cards(connection: Connection, project_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """{project id: its app card} for the projects that have an app. Never raises."""
    ids = list(dict.fromkeys(str(p) for p in project_ids if p))
    if not ids:
        return {}
    try:
        with connection.begin_nested():
            return _cards(connection, ids)
    except Exception:  # noqa: BLE001 — a card never fails its page
        log.warning("your_app: could not read the apps of %d project(s)", len(ids), exc_info=True)
        return {}


def card(connection: Connection, project_id: str) -> Optional[dict[str, Any]]:
    """The one project's app card, or None when it has nothing previewable yet. Raises (the
    Overview's own part() turns a failure into "Couldn't load")."""
    return _cards(connection, [str(project_id)]).get(str(project_id))


def _cards(connection: Connection, ids: list[str]) -> dict[str, dict[str, Any]]:
    from .. import pr_merge
    from .titles import short_title

    rows = [dict(r) for r in connection.execute(text(
        "SELECT * FROM (SELECT r.id, r.project_id, r.title, r.pull_request_url, "
        "r.pull_request_number, r.finalize_ctx, r.decision_id, r.created_at, "
        "d.status AS decision_status, row_number() OVER (PARTITION BY r.project_id "
        "ORDER BY r.created_at DESC) AS n "
        "FROM code_task_runs r LEFT JOIN decisions d ON d.id = r.decision_id "
        "WHERE r.project_id = ANY(:ids) AND r.status = 'completed' "
        "AND (r.pull_request_url IS NOT NULL OR r.pull_request_number IS NOT NULL) "
        "AND COALESCE(r.state->'app_kind'->>'kind', '') <> 'library') x "
        "WHERE n <= :lim ORDER BY project_id, created_at DESC"),
        {"ids": ids, "lim": RECENT_BUILDS}).mappings()]
    if not rows:
        return {}
    by_project: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_project.setdefault(str(r["project_id"]), []).append(r)
    shas: dict[str, str] = {}
    for pid, runs in by_project.items():
        shas.update(pr_merge.merged_shas(connection, pid, [r["decision_id"] for r in runs
                                                           if r.get("decision_id")]))
    picked = {pid: chosen for pid, runs in by_project.items()
              if (chosen := _pick(runs, shas)) is not None}
    if not picked:
        return {}
    names = {str(r["id"]): str(r["name"] or "") for r in connection.execute(text(
        "SELECT id, name FROM projects WHERE id = ANY(:ids)"), {"ids": list(picked)}).mappings()}
    run_ids = [str(r["id"]) for r in picked.values()]
    previews = {str(r["run_id"]): dict(r) for r in connection.execute(text(
        "SELECT DISTINCT ON (run_id) id, run_id, status, error, created_at FROM app_previews "
        "WHERE run_id = ANY(:runs) AND COALESCE(mode, '') <> 'fixture' "
        "ORDER BY run_id, created_at DESC"), {"runs": run_ids}).mappings()}
    out: dict[str, dict[str, Any]] = {}
    for pid, run in picked.items():
        prev = previews.get(str(run["id"]))
        state = sleep_state(prev["status"], prev["error"]) if prev else "none"
        number = run.get("pull_request_number")
        version_label = ("Main" if run["version"] == "main"
                         else f"Change #{number}" if number else "Latest change")
        words = STATE_WORDS[state]
        why = str((prev or {}).get("error") or "")
        if state == "asleep" and "to free memory" in why:
            words = "Stopped to free memory · Open app starts it again"
        elif state == "asleep" and "to make room" in why:
            words = "Stopped to make room for other previews · Open app starts it again"
        out[pid] = {
            "run_id": str(run["id"]),
            "name": names.get(pid) or pid,
            "title": short_title(run.get("title")),
            "version": run["version"],
            "version_label": version_label,
            "version_words": ((f"Main, as merged in change #{number}" if number else "Main")
                              if run["version"] == "main"
                              else f"{version_label}, not merged yet"),
            "pr_number": number,
            "pr_url": run.get("pull_request_url"),
            "preview_id": prev["id"] if prev else None,
            "state": state,
            "words": words,
            # RECREATE PREVIEW (operator, 2026-11-19): a preview that is not running can always be
            # started again from the same build, its kept data restored.
            "recreate": state in ("asleep", "stopped", "failed"),
            # Opening a running or starting preview's link shows it (or "Waking up your app…"
            # until it serves); an asleep one wakes on that same link (preview_wake).
            "href": preview_href(prev["id"]) if prev and state in ("running", "starting", "asleep")
            else None,
            "error": (str(prev.get("error") or "")[:300] or None) if prev and state == "failed"
            else None,
            "project_href": f"?area=project&project_id={pid}",
        }
    return out


__all__ = ["BOSS_STOPPED", "STATE_WORDS", "card", "cards", "preview_href", "sleep_state"]
