"""The Command Center's one read: GET /v1/command-center/view (command-center spec §6.2).

THE FROZEN CONTRACT is apps/web/lib/command-center-view.ts; this module answers exactly that
shape. Change it with the lead, never inside a stream.

PURE COMPOSITION PLUS FIVE BOUNDED READS. Nothing here restates a rule another module owns:
  * attention, counts, missions, outcomes, teams, agents ARE agents_live_board.compose() —
    Agents › Live's board, built with `with_builds=False` (this page draws no builds) — so a
    card here and its twin on Live are one card, and `counts.needs_you` is the one number the
    pill, Live, Approvals and the old Command Center all say.
  * `recent` and `expired_answerable` are command_center.assemble()'s, from the same call.
  * host is run_retrospective.host_advice; budget is BudgetGovernor.status_for_project; the
    Models and Repo dots read the setup basics view command_center.setup_gaps already built.
  * money is included_spend.MONEY_SQL — the budget's own measure, so a plan-covered call is $0
    here exactly as it is on Budget; INCLUDED_SQL is the plan's API value, shown apart.
  * week builds / PRs / jobs / deploys / tokens / model hours are
    self_observability.delivery_metrics, memoised 60s (eleven scalars that change slowly).

The five reads (each bounded, each served by an index in db/schema.sql):
  1. flows    — hand-offs in the last hour, from real hands (agent_acts.observed_edges) (the Floor's edges);
  2. work     — agent work finished by hour, yesterday and today, in the viewer's zone;
  3. money    — MONEY_SQL / INCLUDED_SQL by hour over the week, in the viewer's zone;
  4. runs     — the week's finished runs counted in SQL over EVERY row (never the first sixty:
                command_center.read's LIMIT 60 undercounted a busy install), plus the LAST 24H
                strip's ≤ 10 rows and their steps;
  5. systems  — the queue, previews (read-only: never GET /v1/previews, which tears down and
                deletes rows), deploys and the last hour of model calls.

NOTHING IS ESTIMATED. No percent next to a run, no ETA, no forecast, no time-to-cap: the budget's
forecast fields are read past, never sent. `Budget at n%` is the one ratio, and it is money.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, Mapping, Optional, TypedDict
from zoneinfo import ZoneInfo

from sqlalchemy import text

from .included_spend import INCLUDED_SQL, MONEY_SQL

#: Hand-offs the Floor draws: acts started this recently.
#: The Floor's edges: hand-offs started in the last hour (spec §4.7; the operator's decision
#: after the independent test), thin and fading with age; comets fly only on new ones (web).
FLOW_WINDOW = timedelta(minutes=60)
FLOWS_MAX = 60
#: Rows on the LAST 24H strip; the rest are counted (`day.more`).
STRIP_ROWS = 10
STRIP_WINDOW = timedelta(hours=24)
TICKS_MAX = 40
#: How long the week's delivery scalars are reused, per project, in this process.
DELIVERY_MEMO_SECONDS = 60.0

LIVE_RUN = ("queued", "validating", "running", "waiting", "paused", "blocked")
COMPLETED = ("completed",)
FAILED = ("failed", "deadlettered")
CANCELLED = ("cancelled", "compensated")
WORK_KINDS = ("step", "stage", "seat")

DEPLOY_OK = frozenset({"deployed", "healthy", "succeeded", "completed", "verified", "live"})
DEPLOY_FAILED = frozenset({"failed", "rolled_back", "rollback_pending", "error"})


class ViewStatus(TypedDict):
    key: str
    text: str
    tone: str
    tip: str


class SystemDot(TypedDict):
    key: str
    tone: str
    word: str
    state: str
    tip: str
    href: str


#: Infra's dot tones, as the Command Center draws them.
_TONE = {"green": "green", "red": "red", "amber": "amber", "accent": "amber", "grey": "grey",
         "hollow": "grey", "none": "grey"}


def deploy_dot(status: str, *, ago: str, recent: bool) -> SystemDot:
    """THE DEPLOYS DOT, IN INFRA'S WORDS. One vocabulary (infra_view.deploy_word): a deploy
    waiting on a person says Needs you here as it does in Infra › Deploys, a rejected one
    Rejected, one refused by policy Blocked. Only a failure older than a week reads as settled."""
    from .infra_view import deploy_word

    w = deploy_word(status, newest_for_target=True)
    word, tone = w["word"], _TONE.get(w["tone"], "grey")
    if word in ("Failed", "Rolled back", "Blocked") and not recent:
        word, tone = "OK", "green"
    tips = {"Live": f"Last deploy {ago} ago", "OK": f"Last deploy {ago} ago",
            "Needs you": "A deploy is waiting for your approval",
            "Failed": f"The latest deploy failed {ago} ago",
            "Rolled back": f"The latest deploy was rolled back {ago} ago",
            "Rejected": f"The latest deploy was rejected {ago} ago",
            "Blocked": f"The latest deploy was blocked by a rule {ago} ago"}
    return {"key": "deploys", "tone": tone, "word": "Deploys",
            "state": "OK" if word == "Live" else word,
            "tip": tips.get(word, f"A deploy is in progress ({word.lower()})"),
            "href": "?area=infra&view=deploys"}


# ── small helpers ────────────────────────────────────────────────────────────────────────────


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _aware(value).isoformat()
    return str(value)


def _aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _money(micros: int) -> str:
    """`$84` for whole dollars, `$4.12` otherwise — how the Budget page writes a cap."""
    dollars = (micros or 0) / 1_000_000
    return f"${dollars:,.0f}" if abs(dollars - round(dollars)) < 0.005 else f"${dollars:,.2f}"


def _age(seconds: float) -> str:
    """`7m`, `3h`, `2d`: how long ago, in one token."""
    s = max(0, int(seconds))
    if s < 3600:
        return f"{max(1, s // 60)}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _clip(words: str, limit: int = 14) -> str:
    """A tooltip is ≤ 14 words (workflow-studio §4): the server's reason, cut at a word."""
    parts = str(words or "").split()
    return " ".join(parts) if len(parts) <= limit else " ".join(parts[:limit]) + "…"


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


# ── the zone ─────────────────────────────────────────────────────────────────────────────────

_ZONES: Optional[frozenset[str]] = None
_ZONES_LOCK = threading.Lock()


def resolve_zone(connection: Any, tz: Optional[str]) -> str:
    """The viewer's zone if Postgres and Python both know it, else `UTC` (the response says which
    was used). Validated against pg_timezone_names, read once per process."""
    global _ZONES
    name = str(tz or "").strip()
    if not name or name.upper() == "UTC":
        return "UTC"
    if _ZONES is None:
        with _ZONES_LOCK:
            if _ZONES is None:
                try:
                    with connection.begin_nested():
                        _ZONES = frozenset(str(r[0]) for r in connection.execute(
                            text("SELECT name FROM pg_timezone_names")))
                except Exception:  # noqa: BLE001 — an unknown zone is UTC, never an error
                    return "UTC"
    if name not in _ZONES:
        return "UTC"
    try:
        ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return "UTC"
    return name


def day_bounds(now: datetime, tz: str) -> tuple[datetime, datetime, datetime]:
    """(yesterday 00:00, today 00:00, 6 days before today 00:00) in `tz`, as aware datetimes."""
    zone = ZoneInfo(tz)
    today = now.astimezone(zone).date()
    t0 = datetime.combine(today, dtime(0), tzinfo=zone)
    y0 = datetime.combine(today - timedelta(days=1), dtime(0), tzinfo=zone)
    w0 = datetime.combine(today - timedelta(days=6), dtime(0), tzinfo=zone)
    return y0, t0, w0


def _hour_index(local_naive: datetime, y0: datetime) -> int:
    """Which of the 48 buckets a local wall-clock hour falls in (0-23 yesterday, 24-47 today)."""
    base = y0.replace(tzinfo=None)
    return int((local_naive - base).total_seconds() // 3600)


# ── the delivery memo ────────────────────────────────────────────────────────────────────────

_DELIVERY_MEMO: dict[str, tuple[float, dict[str, Any]]] = {}
_DELIVERY_LOCK = threading.Lock()


def delivery(connection: Any, project_id: str) -> dict[str, Any]:
    """self_observability.delivery_metrics(days=7), reused for 60s per project (§6.2 read 5)."""
    from .self_observability import delivery_metrics

    at = time.monotonic()
    with _DELIVERY_LOCK:
        hit = _DELIVERY_MEMO.get(project_id)
        if hit and at - hit[0] < DELIVERY_MEMO_SECONDS:
            return hit[1]
    value = delivery_metrics(connection, project_id, days=7)
    with _DELIVERY_LOCK:
        _DELIVERY_MEMO[project_id] = (at, value)
    return value


# ── read 1: flows ────────────────────────────────────────────────────────────────────────────


def flows(connection: Any, project_id: str, roster: set[str], now: datetime) -> list[dict[str, Any]]:
    """Who handed work to whom in the last hour, one edge per (hander, taker) — read by the ONE
    helper the Organization view and the Team chart read (agent_acts.observed_edges), with
    `sequence`: a workflow records BOSS as every step's hander, so the hand-off is the previous
    step's agent → the next step's agent; a run's first agent is handed by the person who
    started it. A hander that is not on the roster is DROPPED — never drawn to a guessed node.
    `now` is the clock the helper uses (the database's); it is passed for the signature only."""
    from . import agent_acts

    del now
    edges = agent_acts.observed_edges(connection, project_id,
                                      hours=FLOW_WINDOW.total_seconds() / 3600,
                                      include_people=True, sequence=True, with_newest=True)
    out: list[dict[str, Any]] = []
    for e in edges:
        dst, src = str(e["target"]), str(e["source"])
        if dst not in roster:
            continue
        if src == "person":
            kind, src = "person", "you"
        elif src in roster and src != dst:
            kind = "agent"
        else:
            continue
        out.append({"from_kind": kind, "from": src, "to": dst, "n": int(e["times"]),
                    "last_at": _iso(e["last_at"]), "newest_act_id": str(e.get("newest_act_id") or ""),
                    "run_id": e.get("run_id")})
    out.sort(key=lambda f: str(f["last_at"]), reverse=True)
    return out[:FLOWS_MAX]


# ── reads 2 and 3: today, by hour ────────────────────────────────────────────────────────────


def hours(connection: Any, project_id: str, tz: str, now: datetime,
          finished_by_hour: Mapping[int, tuple[int, int]]) -> dict[str, Any]:
    """48 hourly buckets in the viewer's zone (yesterday 0-23, today 24-47), plus today's totals
    and the week's money. `finished_by_hour` comes from the runs read (read 4)."""
    y0, t0, w0 = day_bounds(now, tz)
    zone = ZoneInfo(tz)
    work = connection.execute(
        text(
            """
            SELECT date_trunc('hour', ended_at AT TIME ZONE :tz) AS h, count(*) AS n,
                   max(ended_at) AS last
            FROM agent_acts
            WHERE project_id = :p AND ended_at >= :y0 AND kind = ANY(:kinds)
              AND status = 'complete'
            GROUP BY 1
            """
        ),
        {"p": project_id, "tz": tz, "y0": y0, "kinds": list(WORK_KINDS)},
    ).mappings().all()
    money = connection.execute(
        text(
            f"""
            SELECT date_trunc('hour', created_at AT TIME ZONE :tz) AS h,
                   COALESCE(sum({MONEY_SQL}), 0) AS money, COALESCE(sum({INCLUDED_SQL}), 0) AS inc
            FROM cost_receipts
            WHERE project_id = :p AND created_at >= :w0
            GROUP BY 1
            """
        ),
        {"p": project_id, "tz": tz, "w0": w0},
    ).mappings().all()
    buckets = []
    for i in range(48):
        local = (y0.replace(tzinfo=None) + timedelta(hours=i))
        buckets.append({"start": local.replace(tzinfo=zone).isoformat(), "work": 0,
                        "runs_finished": 0, "runs_failed": 0, "money_micros": 0,
                        "included_micros": 0})
    last_work: Optional[datetime] = None
    for r in work:
        i = _hour_index(r["h"], y0)
        if 0 <= i < 48:
            buckets[i]["work"] += int(r["n"])
        last = _aware(r["last"])
        if last and (last_work is None or last > last_work):
            last_work = last
    week_money = 0
    for r in money:
        week_money += int(r["money"] or 0)
        i = _hour_index(r["h"], y0)
        if 0 <= i < 48:
            buckets[i]["money_micros"] += int(r["money"] or 0)
            buckets[i]["included_micros"] += int(r["inc"] or 0)
    for i, (done, failed) in finished_by_hour.items():
        if 0 <= i < 48:
            buckets[i]["runs_finished"] += done
            buckets[i]["runs_failed"] += failed
    today = buckets[24:]
    return {
        "hours": buckets,
        "now": now.isoformat(),
        "runs_finished": sum(b["runs_finished"] for b in today),
        "runs_failed": sum(b["runs_failed"] for b in today),
        "money_micros": sum(b["money_micros"] for b in today),
        "included_micros": sum(b["included_micros"] for b in today),
        "last_work_at": _iso(last_work),
        "_week_money": week_money,
        "_t0": t0,
    }


# ── read 4: runs ─────────────────────────────────────────────────────────────────────────────

_RUN_SCOPE = """
    FROM workflow_runs r
    JOIN workflow_definitions d ON d.id = r.workflow_definition_id
    WHERE r.project_id = :p
      AND COALESCE(d.namespace, 'system') = 'product'
      AND r.parent_run_id IS NULL
"""


def finished_counts(connection: Any, project_id: str, tz: str, now: datetime
                    ) -> tuple[list[dict[str, Any]], dict[int, tuple[int, int]], bool]:
    """The week's finished runs, counted in SQL over EVERY row in the window, by local hour:
    the week bars (by day) and Today's per-hour finished/failed come from one aggregate."""
    y0, _t0, w0 = day_bounds(now, tz)
    rows = connection.execute(
        text(
            f"""
            SELECT date_trunc('hour', COALESCE(r.completed_at, r.updated_at) AT TIME ZONE :tz) AS h,
                   count(*) FILTER (WHERE r.status = ANY(:ok)) AS completed,
                   count(*) FILTER (WHERE r.status = ANY(:bad)) AS failed
            {_RUN_SCOPE}
              AND r.status = ANY(:done)
              AND COALESCE(r.completed_at, r.updated_at) >= :w0
            GROUP BY 1
            """
        ),
        {"p": project_id, "tz": tz, "w0": w0, "ok": list(COMPLETED), "bad": list(FAILED),
         "done": list(COMPLETED + FAILED)},
    ).mappings().all()
    zone = ZoneInfo(tz)
    first_day = w0.astimezone(zone).date()
    days = [{"date": (first_day + timedelta(days=i)).isoformat(), "completed": 0, "failed": 0}
            for i in range(7)]
    by_hour: dict[int, tuple[int, int]] = {}
    for r in rows:
        local: datetime = r["h"]
        d = (local.date() - first_day).days
        if 0 <= d < 7:
            days[d]["completed"] += int(r["completed"])
            days[d]["failed"] += int(r["failed"])
        i = _hour_index(local, y0)
        if 0 <= i < 48:
            done, bad = by_hour.get(i, (0, 0))
            by_hour[i] = (done + int(r["completed"]), bad + int(r["failed"]))
    return days, by_hour, bool(rows)


def _row_state(status: str, mission: Optional[Mapping[str, Any]]) -> str:
    if status in COMPLETED:
        return "completed"
    if status in FAILED:
        return "failed"
    if status in CANCELLED:
        return "cancelled"
    state = str((mission or {}).get("state") or "")
    if state == "waiting_you":
        return "waiting_you"
    if status == "blocked" or state in ("blocked", "stopped"):
        return "blocked"
    return "running"


def strip(connection: Any, project_id: str, now: datetime,
          missions: list[Mapping[str, Any]]) -> dict[str, Any]:
    """LAST 24H: runs that were live at any point in the window — still live, started in it, or
    finished in it — live first (by start), then finished (newest first). ≤ 10 rows; the rest are
    counted, in the same statement (`count(*) OVER ()`), so `more` is exact."""
    from .command_center import run_name, step_index

    since = now - STRIP_WINDOW
    runs = [dict(r) for r in connection.execute(
        text(
            """
            SELECT r.id, r.status, r.input, r.error, r.created_at, r.completed_at, r.updated_at,
                   d.workflow_key, d.name AS workflow_name,
                   COALESCE(jsonb_array_length(v.spec->'steps'), 0) AS spec_steps,
                   count(*) OVER () AS total
            FROM workflow_runs r
            JOIN workflow_definitions d ON d.id = r.workflow_definition_id
            JOIN workflow_versions v ON v.id = r.workflow_version_id
            WHERE r.project_id = :p
              AND COALESCE(d.namespace, 'system') = 'product'
              AND r.parent_run_id IS NULL
              AND (r.status = ANY(:live) OR r.created_at >= :since
                   OR (r.updated_at >= :since AND COALESCE(r.completed_at, r.updated_at) >= :since))
            ORDER BY (r.status = ANY(:live)) DESC,
                     CASE WHEN r.status = ANY(:live) THEN r.created_at END ASC,
                     COALESCE(r.completed_at, r.updated_at) DESC
            LIMIT :lim
            """
        ),
        {"p": project_id, "live": list(LIVE_RUN), "since": since, "lim": STRIP_ROWS},
    ).mappings()]
    total = int(runs[0]["total"]) if runs else 0
    steps_by_run: dict[str, list[dict[str, Any]]] = {}
    if runs:
        for s in connection.execute(
            text(
                """
                SELECT s.workflow_run_id, s.step_id, s.status, s.started_at, s.completed_at,
                       s.decision_id, s.step_kind, s.created_at, s.updated_at,
                       dd.created_at AS decision_at
                FROM workflow_step_runs s
                LEFT JOIN decisions dd ON dd.id = s.decision_id
                WHERE s.workflow_run_id = ANY(:ids)
                ORDER BY s.created_at, s.iteration
                """
            ),
            {"ids": [r["id"] for r in runs]},
        ).mappings():
            steps_by_run.setdefault(str(s["workflow_run_id"]), []).append(dict(s))
    by_mission = {str(m.get("run_id")): m for m in missions}
    rows: list[dict[str, Any]] = []
    for r in runs:
        run_id = str(r["id"])
        status = str(r["status"] or "")
        latest: dict[str, dict[str, Any]] = {}
        for s in steps_by_run.get(run_id, []):
            latest[str(s["step_id"])] = s
        steps = list(latest.values())
        done = sum(1 for s in steps if s["status"] in ("succeeded", "skipped"))
        count = max(int(r["spec_steps"] or 0), len(steps))
        ticks = sorted(_iso(s["completed_at"]) for s in steps
                       if s["status"] in ("succeeded", "skipped", "failed") and s["completed_at"])
        gates = []
        for s in steps:
            if not (s.get("decision_id") or str(s.get("step_kind") or "") == "decision"):
                continue
            # A GATE THE WORKER PARKED NEVER "STARTS": a waiting decision step has no
            # started_at in production, so it was never drawn. Its wait began when its decision
            # was asked, else when the step row was written.
            began = s.get("started_at") or s.get("decision_at") or s.get("created_at")
            if not began:
                continue
            open_gate = s["status"] in ("waiting", "running", "leased", "ready", "queued")
            gates.append({"from": _iso(began),
                          "to": None if open_gate else _iso(s.get("completed_at")
                                                            or s.get("updated_at") or began)})
        mission = by_mission.get(run_id)
        state = _row_state(status, mission)
        finished = None
        if state in ("completed", "failed", "cancelled"):
            finished = _iso(r["completed_at"] or r["updated_at"])
        rows.append({
            "run_id": run_id,
            "name": run_name(r),
            "workflow_name": str(r["workflow_name"] or r["workflow_key"] or ""),
            "state": state,
            "started_at": _iso(r["created_at"]),
            "finished_at": finished,
            "step_index": int((mission or {}).get("step_index") or step_index(done, count, status)),
            "step_count": int((mission or {}).get("step_count") or count),
            "ticks": [t for t in ticks if t][-TICKS_MAX:],
            "gates": sorted(gates, key=lambda g: str(g["from"])),
            "verb": (mission or {}).get("verb") if state in ("running", "waiting_you", "blocked") else None,
            "error": (str(r["error"]).strip().splitlines()[0][:220]
                      if state == "failed" and r.get("error") and str(r["error"]).strip() else None),
        })
    return {"since": since.isoformat(), "rows": rows, "more": max(0, total - len(rows))}


def ever_ran(connection: Any, project_id: str) -> bool:
    return bool(connection.execute(
        text(f"SELECT EXISTS (SELECT 1 {_RUN_SCOPE})"), {"p": project_id}).scalar())


# ── read 5: systems ──────────────────────────────────────────────────────────────────────────


def _probe(connection: Any, project_id: str, now: datetime, t0: datetime) -> dict[str, Any]:
    """The queue, deadletters, previews, deploys and the last hour of model calls, in one
    round trip of scalar subqueries — each served by an index (§6.2 read 5)."""
    row = connection.execute(
        text(
            """
            SELECT
              (SELECT min(created_at) FROM jobs
                WHERE project_id = :p AND status = 'queued') AS oldest_queued,
              (SELECT count(*) FROM jobs
                WHERE project_id = :p AND status IN ('queued', 'leased')) AS open_jobs,
              (SELECT count(*) FROM deadletters
                WHERE project_id = :p AND created_at > :day) AS deadletters,
              (SELECT count(*) FROM deployment_targets WHERE project_id = :p) AS targets,
              (SELECT count(*) FROM llm_calls
                WHERE project_id = :p AND created_at > :hour) AS calls_hour,
              (SELECT count(*) FROM llm_calls
                WHERE project_id = :p AND created_at > :hour AND NOT ok) AS failed_hour,
              (SELECT count(*) FROM llm_calls
                WHERE project_id = :p AND created_at >= :t0) AS calls_today
            """
        ),
        {"p": project_id, "day": now - timedelta(hours=24), "hour": now - timedelta(hours=1),
         "t0": t0},
    ).mappings().one()
    # The newest preview OF EACH APP in the last day, plus the newest overall: a failed preview
    # of one app is not hidden because another app's preview started after it.
    previews = [dict(r) for r in connection.execute(
        text("SELECT DISTINCT ON (COALESCE(repo, run_id, id)) status, created_at, "
             "       COALESCE(repo, run_id, id) AS app FROM app_previews "
             "WHERE project_id = :p "
             "ORDER BY COALESCE(repo, run_id, id), created_at DESC"),
        {"p": project_id},
    ).mappings()]
    previews.sort(key=lambda r: _aware(r.get("created_at")) or datetime.min.replace(
        tzinfo=timezone.utc), reverse=True)
    # The newest deploy of ANY kind — the same union Infra › Deploys reads (a stack deploy is
    # a deploy too), worded by the same vocabulary (infra_view.deploy_word).
    deploy = connection.execute(
        text("SELECT status, created_at, finished_at FROM ("
             "  SELECT status, created_at, finished_at FROM deployment_runs WHERE project_id = :p "
             "  UNION ALL "
             "  SELECT status, created_at, finished_at FROM stack_deployments "
             "  WHERE project_id = :p) u ORDER BY created_at DESC LIMIT 1"),
        {"p": project_id},
    ).mappings().first()
    return {**dict(row), "previews": previews, "deploy": dict(deploy) if deploy else None}


def systems(*, probe: Mapping[str, Any], basics: Optional[Mapping[str, Any]],
            budget: Mapping[str, Any], host: Mapping[str, Any], now: datetime,
            reachable_setup: bool = True) -> list[SystemDot]:
    """The seven dots (§4.11). Each tone comes from an existing derivation; grey hollow means
    "not set up", never failure. Pure: tested without a database."""
    from . import setup_view

    out: list[SystemDot] = []

    # MODELS: the rows Repo and models draws, plus the last hour of calls.
    models = list((basics or {}).get("models") or [])
    working = [m for m in models if setup_view.model_working(m)]
    limited = [m for m in models if str((m.get("pill") or {}).get("state")) == "rate_limited"]
    failed_hour = int(probe.get("failed_hour") or 0)
    today_calls = int(probe.get("calls_today") or 0)
    if basics is None:
        out.append({"key": "models", "tone": "grey", "word": "Models", "state": "Unknown",
                    "tip": "Could not read Repo and models", "href": "?area=setup&focus=models"})
    elif not models:
        # "NOT SET UP" LOOKS THE SAME ON EVERY DOT (independent test D13): grey and hollow. The
        # setup line in Needs you and the sentence ("Finish setup") carry that it blocks work.
        out.append({"key": "models", "tone": "grey", "word": "Models", "state": "Not set up",
                    "tip": "No model connected yet", "href": "?area=setup&focus=models"})
    elif not working:
        broken = models[0]
        out.append({"key": "models", "tone": "red", "word": "Models", "state": "Failing",
                    "tip": _clip(f"{broken['label']}: {broken['pill']['tip']}"),
                    "href": "?area=setup&focus=models"})
    elif limited or failed_hour:
        tip = (f"{limited[0]['label']}: {limited[0]['pill']['tip']}" if limited
               else f"{_plural(failed_hour, 'model call', 'model calls')} failed in the last hour")
        out.append({"key": "models", "tone": "amber", "word": "Models", "state": "Slow",
                    "tip": _clip(tip), "href": "?area=setup&focus=models"})
    else:
        out.append({"key": "models", "tone": "green", "word": "Models", "state": "OK",
                    "tip": _clip(f"{working[0]['label']} working · "
                                 f"{_plural(today_calls, 'call', 'calls')} today"),
                    "href": "?area=setup&focus=models"})

    # REPO: connected, expiring within 3 days, failing, or none connected.
    repos = list(((basics or {}).get("repository") or {}).get("repos") or [])
    if basics is None:
        out.append({"key": "repo", "tone": "grey", "word": "Repo", "state": "Unknown",
                    "tip": "Could not read Repo and models", "href": "?area=setup&focus=repository"})
    elif not repos:
        out.append({"key": "repo", "tone": "grey", "word": "Repo", "state": "Not set up",
                    "tip": "No repository connected yet", "href": "?area=setup&focus=repository"})
    else:
        default = next((r for r in repos if r.get("default")), repos[0])
        soon = setup_view.expiring_within({"repository": basics["repository"], "models": []}, 3)
        if not any(setup_view.repository_working(r) for r in repos):
            out.append({"key": "repo", "tone": "red", "word": "Repo", "state": "Failing",
                        "tip": _clip(f"{default['full_name']}: {default['pill']['tip']}"),
                        "href": "?area=setup&focus=repository"})
        elif soon:
            out.append({"key": "repo", "tone": "amber", "word": "Repo", "state": "Expiring",
                        "tip": f"Token expires in {_plural(int(soon['days']), 'day', 'days')}",
                        "href": "?area=setup&focus=repository"})
        else:
            out.append({"key": "repo", "tone": "green", "word": "Repo", "state": "OK",
                        "tip": _clip(f"{default['full_name']} connected"),
                        "href": "?area=setup&focus=repository"})

    # BUDGET: the budget's own state word; no cap is grey, not red.
    state = str(budget.get("budget_state") or "unknown")
    period = str(budget.get("period") or "month")
    words = {"day": "today", "week": "this week", "month": "this month"}
    spent = f"{_money(int(budget.get('actual_usd_micros') or 0))} of " \
            f"{_money(int(budget.get('cap_usd_micros') or 0))} {words.get(period, 'this ' + period)}"
    if state == "unknown" or not int(budget.get("cap_usd_micros") or 0):
        out.append({"key": "budget", "tone": "grey", "word": "Budget", "state": "No cap",
                    "tip": "No spending cap set", "href": "?area=budget"})
    else:
        tone = {"green": "green", "yellow": "amber", "orange": "amber", "red": "red"}.get(state, "grey")
        out.append({"key": "budget", "tone": tone, "word": "Budget",
                    "state": {"green": "OK", "amber": "Near cap", "red": "Over cap"}.get(tone, "Unknown"),
                    "tip": spent, "href": "?area=budget"})

    # HOST: run_retrospective.host_advice, the server's own words.
    # SEAM (stability stream): a system-health attention kind and a /v1 system-health read are
    # coming; when they land, this dot reads that instead of host_advice alone.
    if not host.get("show"):
        out.append({"key": "host", "tone": "green", "word": "Host", "state": "OK",
                    "tip": "No memory, disk or stall trouble in the last hour", "href": "?area=infra"})
    else:
        critical = host.get("severity") == "critical"
        disk = host.get("cause") == "disk"
        reason = " · ".join(str(x) for x in (host.get("headline"), host.get("detail")) if x)
        out.append({"key": "host", "tone": "red" if critical else "amber", "word": "Host",
                    # A full disk is its own state, not "slow": nothing is slow yet.
                    "state": "Struggling" if critical else ("Disk" if disk else "Slow"),
                    "tip": _clip(reason or "The host needs attention"),
                    "href": "?area=infra"})

    # QUEUE: how long the oldest job has waited; dead letters in 24h.
    oldest = _aware(probe.get("oldest_queued"))
    waited = (now - oldest).total_seconds() if oldest else 0.0
    dead = int(probe.get("deadletters") or 0)
    open_jobs = int(probe.get("open_jobs") or 0)
    if dead or waited > 30 * 60:
        tip = (f"{_plural(dead, 'dead letter', 'dead letters')} in the last 24h" if dead
               else f"Oldest job waiting {_age(waited)}")
        out.append({"key": "queue", "tone": "red", "word": "Queue", "state": "Stuck", "tip": tip,
                    "href": "?area=monitoring"})
    elif waited > 5 * 60:
        out.append({"key": "queue", "tone": "amber", "word": "Queue", "state": "Slow",
                    "tip": f"Oldest job waiting {_age(waited)}", "href": "?area=monitoring"})
    else:
        out.append({"key": "queue", "tone": "green", "word": "Queue", "state": "OK",
                    "tip": (f"{_plural(open_jobs, 'job', 'jobs')} open, none waiting long"
                            if open_jobs else "Nothing waiting"),
                    "href": "?area=monitoring"})

    # PREVIEWS: read-only over app_previews, worded as Infra › Previews words them.
    previews = list(probe.get("previews") or [])
    if not previews:
        out.append({"key": "previews", "tone": "grey", "word": "Previews", "state": "Not set up",
                    "tip": "No preview yet", "href": "?area=infra&view=previews"})
    else:
        latest = previews[0]
        running = sum(1 for p in previews if p.get("status") == "running")
        started = _aware(latest.get("created_at"))
        failed = [p for p in previews if p.get("status") == "failed"
                  and (_aware(p.get("created_at")) or now) >= now - timedelta(hours=24)]
        if failed:
            out.append({"key": "previews", "tone": "red", "word": "Previews", "state": "Failed",
                        "tip": (f"{_plural(len(failed), 'app', 'apps')}' newest preview failed "
                                "in the last 24h"),
                        "href": "?area=infra&view=previews"})
        elif latest.get("status") == "starting" and started and now - started > timedelta(minutes=5):
            out.append({"key": "previews", "tone": "amber", "word": "Previews", "state": "Slow",
                        "tip": f"A preview has been starting for {_age((now - started).total_seconds())}",
                        "href": "?area=infra&view=previews"})
        else:
            out.append({"key": "previews", "tone": "green", "word": "Previews", "state": "OK",
                        "tip": (f"{_plural(running, 'preview', 'previews')} running" if running
                                else "None running, none requested"),
                        "href": "?area=infra&view=previews"})

    # DEPLOYS: the latest deployment run; no target bound is grey.
    deploy = probe.get("deploy")
    targets = int(probe.get("targets") or 0)
    if not targets and not deploy:
        out.append({"key": "deploys", "tone": "grey", "word": "Deploys", "state": "Not set up",
                    "tip": "No deploy target yet", "href": "?area=infra"})
    elif not deploy:
        out.append({"key": "deploys", "tone": "green", "word": "Deploys", "state": "OK",
                    "tip": f"{_plural(targets, 'target', 'targets')} ready, nothing deployed yet",
                    "href": "?area=infra"})
    else:
        status = str(deploy.get("status") or "")
        at = _aware(deploy.get("finished_at")) or _aware(deploy.get("created_at"))
        ago = _age((now - at).total_seconds()) if at else ""
        out.append(deploy_dot(status, ago=ago, recent=bool(at and now - at <= timedelta(days=7))))
    return out


# ── the sentence ─────────────────────────────────────────────────────────────────────────────


def status(*, counts: Mapping[str, Any], attention: list[Mapping[str, Any]],
           budget: Mapping[str, Any], host: Mapping[str, Any], runs_24h: int, ever: bool,
           last_work_at: Optional[str], now: datetime,
           refusal: Optional[Mapping[str, Any]] = None) -> ViewStatus:
    """THE STATE SENTENCE (§4.5), first matching rule wins. Row 1 (`Can't reach BOSS`) is the
    web's: only it can know. Pure.

    Two refinements from the independent test: a budget that REFUSES AT THE DOOR (the budget's
    own cap_refusal: the money already spent leaves no room) stopped work even when no build
    row records the refusal; and a host WARNING is no headline when its cause is the disk (the
    Host dot says Disk), nor ever outranks "Ready for first build" — only critical does."""
    state = str(budget.get("budget_state") or "unknown")
    refused = refusal is not None or any(a.get("kind") == "budget_refused" for a in attention)
    period = str(budget.get("period") or "month")
    if refused and (state == "red" or refusal is not None):
        over = str((refusal or {}).get("period") or period)
        return {"key": "budget_stopped", "text": "Budget stopped work", "tone": "red",
                "tip": f"Over the {over} cap. Raise it in Budget."}
    if host.get("show") and host.get("severity") == "critical":
        return {"key": "host_critical", "text": "Host struggling", "tone": "red",
                "tip": _clip(str(host.get("headline") or "The host needs attention"))}
    stopped = int(counts.get("stopped") or 0)
    if stopped:
        return {"key": "stopped", "text": f"{stopped} stopped", "tone": "red",
                "tip": "Failed steps or builds waiting for you"}
    needs = int(counts.get("needs_you") or 0)
    if needs:
        return {"key": "needs_you", "text": f"{needs} {'needs' if needs == 1 else 'need'} you",
                "tone": "amber", "tip": "Decisions waiting on you. Same count as Approvals."}
    fresh = runs_24h == 0 and not ever and not int(counts.get("setup") or 0)
    if host.get("show") and host.get("cause") != "disk" and not fresh:
        return {"key": "host_slow", "text": "Host is slow", "tone": "amber",
                "tip": _clip(str(host.get("headline") or "The host is under pressure"))}
    if state in ("orange", "red"):
        ratio = float(budget.get("utilization_ratio") or 0.0)
        words = {"day": "today", "week": "this week", "month": "this month"}
        return {"key": "budget_high", "text": f"Budget at {math.floor(ratio * 100)}%",
                "tone": "amber",
                "tip": _clip(f"{_money(int(budget.get('actual_usd_micros') or 0))} of "
                             f"{_money(int(budget.get('cap_usd_micros') or 0))} "
                             f"{words.get(period, 'this ' + period)}. "
                             f"Resets {budget.get('resets_at_label') or 'soon'}.")}
    # NEVER "RUNNING" FOR A RUN THAT WAITS ON YOU (independent test D3): the sentence counts the
    # runs actually moving; one parked at your gate is in the needs-you rule above.
    running = int(counts.get("moving", counts.get("running")) or 0)
    if running:
        return {"key": "running", "text": f"{running} running", "tone": "accent",
                "tip": "Workflow runs in progress"}
    if int(counts.get("setup") or 0):
        return {"key": "setup", "text": "Finish setup", "tone": "amber",
                "tip": "Builds can't start until setup is done"}
    if runs_24h == 0 and not ever:
        return {"key": "first_build", "text": "Ready for first build", "tone": "accent",
                "tip": "Describe something to build below"}
    last = _aware(datetime.fromisoformat(last_work_at)) if last_work_at else None
    tip = "Nothing running or waiting."
    if last:
        tip += f" Last work {_age((now - last).total_seconds())} ago."
    return {"key": "quiet", "text": "All quiet", "tone": "green", "tip": tip}


# ── the ETag ─────────────────────────────────────────────────────────────────────────────────


def etag_of(view: Mapping[str, Any], board_etag: str) -> str:
    """Over what the page shows changing, never the clock (`generated_at`, elapsed times and
    `today.now` are left out: the web ticks them). §6.2 ETag material."""
    t = view["today"]
    hours_ = t["hours"]
    now = datetime.fromisoformat(t["now"])
    current = next((i for i in range(47, -1, -1)
                    if datetime.fromisoformat(hours_[i]["start"]) <= now), 47)
    material = {
        "board": board_etag,
        "tz": view["tz"],
        "status": [view["status"]["key"], view["status"]["text"], view["status"]["tone"]],
        "expired": view["counts"].get("expired_answerable"),
        "recent": [(r.get("run_id"), r.get("outcome")) for r in view["recent"]],
        "flows": [(f["from"], f["to"], f["n"], f["newest_act_id"]) for f in view["flows"]],
        "hours": [hours_[i] for i in (current - 1, current) if 0 <= i < 48],
        "today": [t["runs_finished"], t["runs_failed"], t["money_micros"], t["included_micros"],
                  t["ever_ran"], t["hours"][0]["start"]],
        "day": [(r["run_id"], r["state"], r["step_index"], len(r["ticks"]), len(r["gates"]),
                 r["finished_at"]) for r in view["day"]["rows"]] + [view["day"]["more"]],
        "week": {k: v for k, v in view["week"].items() if k != "days"}
        | {"days": [(d["date"], d["completed"], d["failed"]) for d in view["week"]["days"]]},
        "budget": [view["budget"]["state"], round(view["budget"]["actual_usd_micros"] / 10_000),
                   view["budget"]["cap_usd_micros"], view["budget"]["day"]["cap_usd_micros"]],
        "host": [view["host"].get("show"), view["host"].get("severity"),
                 view["host"].get("headline")],
        "systems": [(s["key"], s["tone"], s["tip"]) for s in view["systems"]],
        "refusal": [(view.get("budget_refusal") or {}).get(k) for k in ("period", "actual_usd_micros")],
    }
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode())
    return f'"cc-{digest.hexdigest()[:24]}"'


# ── the view ─────────────────────────────────────────────────────────────────────────────────


def _quiet(connection: Any, what: str, fn, *args, default=None, **kwargs):
    """A secondary read never blanks the landing page: it degrades to its empty form."""
    try:
        with connection.begin_nested():
            return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger("boss.command_center_view").warning("could not read %s", what,
                                                              exc_info=True)
        return default


def build(connection: Any, settings: Any, project_id: str, *, tz: Optional[str] = None,
          viewer: Optional[str] = None, now: Optional[datetime] = None) -> dict[str, Any]:
    """The whole Command Center in one read. Composition only; see the module doc."""
    from . import agents_live_board
    from .budget import BudgetGovernor
    from .run_retrospective import host_advice

    now = now or datetime.now(timezone.utc)
    zone = resolve_zone(connection, tz)
    setup_out: dict[str, Any] = {}
    board, cc, _read = agents_live_board.compose(connection, settings, project_id, viewer=viewer,
                                                 now=now, with_builds=False, setup_out=setup_out)
    roster = {str(a["role_key"]) for a in board["agents"]}

    host = _quiet(connection, "host advice", host_advice, connection, project_id,
                  default={"show": False}) or {"show": False}
    budget_full = _quiet(connection, "the budget", BudgetGovernor(settings).status_for_project,
                         connection, project_id, now=now, default={}) or {}

    edge_list = _quiet(connection, "hand-offs", flows, connection, project_id, roster, now,
                       default=[]) or []
    days, by_hour, any_week = _quiet(connection, "finished runs", finished_counts, connection,
                                     project_id, zone, now,
                                     default=([], {}, False)) or ([], {}, False)
    today = _quiet(connection, "today", hours, connection, project_id, zone, now, by_hour,
                   default=None)
    if today is None:
        y0, t0, _w0 = day_bounds(now, zone)
        today = {"hours": [{"start": (y0.replace(tzinfo=None) + timedelta(hours=i))
                            .replace(tzinfo=ZoneInfo(zone)).isoformat(), "work": 0,
                            "runs_finished": 0, "runs_failed": 0, "money_micros": 0,
                            "included_micros": 0} for i in range(48)],
                 "now": now.isoformat(), "runs_finished": 0, "runs_failed": 0,
                 "money_micros": 0, "included_micros": 0, "last_work_at": None,
                 "_week_money": 0, "_t0": t0}
    t0 = today.pop("_t0")
    week_money = int(today.pop("_week_money") or 0)
    day = _quiet(connection, "the day strip", strip, connection, project_id, now,
                 board["missions"], default=None) or {
        "since": (now - STRIP_WINDOW).isoformat(), "rows": [], "more": 0}
    ever = any_week or bool(day["rows"]) or bool(
        _quiet(connection, "whether anything ran", ever_ran, connection, project_id, default=False))
    if not today["last_work_at"]:
        idle = [a.get("since") for a in board["agents"] if a.get("since")]
        today["last_work_at"] = max(idle) if idle else None
    today["ever_ran"] = ever

    metrics = _quiet(connection, "delivery metrics", delivery, connection, project_id,
                     default={}) or {}
    completed = sum(d["completed"] for d in days)
    failed = sum(d["failed"] for d in days)
    week = {
        "days": days,
        "runs": completed + failed,
        "passed": completed,
        "failed": failed,
        "builds": int(metrics.get("runs_total") or 0),
        "builds_passed": int(metrics.get("runs_succeeded") or 0),
        "prs_opened": int(metrics.get("prs_opened") or 0),
        "jobs": int(metrics.get("jobs_ran") or 0),
        "deployments": int(metrics.get("deployments") or 0),
        "money_micros": week_money,
        "tokens": int(metrics.get("tokens") or 0),
        # Time models spent answering (llm_calls.latency_ms), not agent time (truth bug 5).
        "model_hours": float(metrics.get("agent_hours") or 0.0),
    }

    periods = budget_full.get("periods") or {}
    day_period = periods.get("day") or {}
    budget = {
        "state": str(budget_full.get("budget_state") or "unknown"),
        "period": str(budget_full.get("period") or "day"),
        "cap_usd_micros": int(budget_full.get("cap_usd_micros") or 0),
        "actual_usd_micros": int(budget_full.get("actual_usd_micros") or 0),
        "utilization_ratio": (float(budget_full["utilization_ratio"])
                              if budget_full.get("utilization_ratio") is not None else None),
        "resets_at_label": str(budget_full.get("resets_at_label") or ""),
        "budget_timezone": str(budget_full.get("budget_timezone") or "UTC"),
        "day": {"cap_usd_micros": int(day_period.get("cap_usd_micros") or 0),
                "actual_usd_micros": int(day_period.get("actual_usd_micros") or 0)},
    }

    # REFUSED AT THE DOOR: work started while the money already spent leaves no room is refused
    # before any build row exists, so no attention item records it. The budget's own rule
    # (cap_refusal of one more micro-dollar, kind `cap`) says it; the page draws it as the
    # first card and the sentence says "Budget stopped work".
    refusal: Optional[dict[str, Any]] = None
    if budget_full.get("periods"):
        hit = BudgetGovernor(settings).cap_refusal(budget_full, 1)
        if hit and hit.get("kind") == "cap":
            words = {"day": "today", "week": "this week", "month": "this month"}
            p_ = str(hit.get("period") or "")
            refusal = {
                "period": p_,
                "cap_usd_micros": int(hit.get("cap_usd_micros") or 0),
                "actual_usd_micros": int(hit.get("actual_usd_micros") or 0),
                "resets_at_label": str(hit.get("resets_at_label") or ""),
                "title": "Budget refused new work",
                "detail": _clip(f"{_money(int(hit.get('actual_usd_micros') or 0))} of "
                                f"{_money(int(hit.get('cap_usd_micros') or 0))} "
                                f"{words.get(p_, 'this ' + p_)}. Raise the cap to start work."),
            }

    probe = _quiet(connection, "the systems", _probe, connection, project_id, now, t0,
                   default={}) or {}
    dots = systems(probe=probe, basics=setup_out.get("basics"), budget=budget_full, host=host,
                   now=now)

    counts = {**board["counts"], "expired_answerable": int(cc["counts"].get("expired_answerable") or 0)}
    moving = sum(1 for m in board["missions"]
                 if m["state"] not in ("done", "stopped", "waiting_you", "waiting_person"))
    runs_24h = len(day["rows"]) + int(day["more"])
    view: dict[str, Any] = {
        "project_id": project_id,
        "etag": "",
        "generated_at": now.isoformat(),
        "tz": zone,
        "status": status(counts={**counts, "moving": moving}, attention=board["attention"],
                         budget=budget_full,
                         host=host, runs_24h=runs_24h, ever=ever,
                         last_work_at=today["last_work_at"], now=now, refusal=refusal),
        "counts": counts,
        "attention": board["attention"],
        "missions": board["missions"],
        "outcomes": board["outcomes"],
        "recent": cc["recent"],
        "teams": board["teams"],
        "agents": board["agents"],
        "flows": edge_list,
        "today": today,
        "day": day,
        "week": week,
        "budget": budget,
        "host": {k: v for k, v in host.items() if k != "gauges"},
        "systems": dots,
        "budget_refusal": refusal,
    }
    view["etag"] = etag_of(view, board["etag"])
    return view
