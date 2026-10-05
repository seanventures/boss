"""The Command Center's one read: GET /v1/command-center/view (command-center spec §6.2).

THE FROZEN CONTRACT is apps/web/lib/command-center-view.ts; this module answers exactly that
shape. Change it with the lead, never inside a stream.

PURE COMPOSITION PLUS BOUNDED READS. Nothing here restates a rule another module owns:
  * attention, counts, missions, outcomes, teams, agents ARE agents_live_board.compose() —
    Agents › Live's board, built with `with_builds=False` (this page draws no builds) — so a
    card here and its twin on Live are one card, and `counts.needs_you` is the one number the
    pill, Live, Approvals and the old Command Center all say.
  * `recent` and `expired_answerable` are command_center.assemble()'s, from the same call.
  * host is run_retrospective.host_advice; budget is BudgetGovernor.status_for_project; the
    Models and Repo dots read the setup basics view command_center.setup_gaps already built.
  * money is included_spend.MONEY_SQL — the budget's own measure, so a plan-covered call is $0
    here exactly as it is on Budget; INCLUDED_SQL is the plan's API value, shown apart.
  * old Shepherd questions (system_health, > 7 days) are one `cleanup` row whose button is
    system_doctor.clean_up_old — the group lists exactly what that clears.

The reads (each bounded, each served by an index in db/schema.sql):
  1. work     — agent work finished by hour, yesterday and today, in the viewer's zone;
  2. money    — MONEY_SQL / INCLUDED_SQL by hour over the week, in the viewer's zone;
  3. runs     — the week's finished runs counted in SQL over EVERY row (never the first sixty);
  4. pulse    — finished agent steps per local day over 14 weeks, the five busiest agents and
                the seven newest things the team did, one per app (memoised 60s);
  5. systems  — the queue, previews (read-only: never GET /v1/previews, which tears down and
                deletes rows), deploys and the last hour of model calls.

The v2 page draws no Floor and no day strip, so their reads (hand-offs, the 24h strip) are gone
from the view. `week` stays: the week's delivery line (runs, builds, PRs, spend) is still drawn;
its builds / PRs / jobs / deploys / tokens / model hours are self_observability.delivery_metrics,
memoised 60s.

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

#: THE PULSE (Command Center v2): the team's last 14 weeks of finished agent steps, by day.
PULSE_WEEKS = 14
PULSE_TOP = 5
PULSE_LATEST = 7
#: The newest acts the "Latest from your team" feed looks through for one act per app.
PULSE_SCAN = 400
#: How long the pulse is reused, per (project, zone), in this process: it moves by the step.
PULSE_MEMO_SECONDS = 60.0
#: System findings older than this are history, grouped into one row with a Clean up.
OLD_FINDING = timedelta(days=7)

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

#: How long the week's delivery scalars are reused, per project, in this process.
DELIVERY_MEMO_SECONDS = 60.0
_DELIVERY_MEMO: dict[str, tuple[float, dict[str, Any]]] = {}
_DELIVERY_LOCK = threading.Lock()


def delivery(connection: Any, project_id: str) -> dict[str, Any]:
    """self_observability.delivery_metrics(days=7), reused for 60s per project."""
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


def ever_ran(connection: Any, project_id: str) -> bool:
    return bool(connection.execute(
        text(f"SELECT EXISTS (SELECT 1 {_RUN_SCOPE})"), {"p": project_id}).scalar())


# ── the pulse: the team's last 14 weeks ──────────────────────────────────────────────────────

_PULSE_MEMO: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_PULSE_LOCK = threading.Lock()

#: What an agent did at a build's station, as a verb before the app's name.
STATION_VERB = {"plan": "planned", "tests": "wrote the tests for", "build": "built",
                "check": "checked", "review": "reviewed", "pr": "opened a PR for"}
#: An agent step: finished work an agent did (never a run, a chat, an approval or a hand-off).
STEP_KINDS = ("stage", "seat", "step")


def pulse_start(now: datetime, tz: str) -> datetime:
    """The Monday PULSE_WEEKS - 1 weeks before this week's Monday, 00:00 in `tz`."""
    zone = ZoneInfo(tz)
    today = now.astimezone(zone).date()
    monday = today - timedelta(days=today.weekday()) - timedelta(weeks=PULSE_WEEKS - 1)
    return datetime.combine(monday, dtime(0), tzinfo=zone)


def pulse(connection: Any, project_id: str, tz: str, now: datetime) -> dict[str, Any]:
    """THE TEAM'S PULSE, aggregated in SQL (never in the browser from the apps list): finished
    agent steps per local day over 14 weeks, the five busiest agents, and the seven newest
    things the team did — one per app — in words ("Atlas planned Interview Scorecard"). Three
    bounded statements on agent_acts_ended / agent_acts_role_ended, plus one for subjects."""
    from . import factory_view
    from .command_center import run_name
    from .code_task_service import is_internal_run

    start = pulse_start(now, tz)
    zone = ZoneInfo(tz)
    base = {"p": project_id, "since": start, "kinds": list(STEP_KINDS), "tz": tz}
    by_day = {r["d"]: int(r["n"]) for r in connection.execute(
        text(
            """
            SELECT (ended_at AT TIME ZONE :tz)::date AS d, count(*) AS n
            FROM agent_acts
            WHERE project_id = :p AND ended_at >= :since AND role_key IS NOT NULL
              AND kind = ANY(:kinds) AND status = 'complete'
            GROUP BY 1
            """
        ), base).mappings()}
    top = [{"role_key": str(r["role_key"]), "n": int(r["n"])} for r in connection.execute(
        text(
            """
            SELECT role_key, count(*) AS n
            FROM agent_acts
            WHERE project_id = :p AND ended_at >= :since AND role_key IS NOT NULL
              AND kind = ANY(:kinds) AND status = 'complete'
            GROUP BY role_key ORDER BY count(*) DESC, role_key LIMIT :lim
            """
        ), {**base, "lim": PULSE_TOP}).mappings()]
    first, last = start.date(), now.astimezone(zone).date()
    days = []
    d = first
    while d <= last:
        days.append({"date": d.isoformat(), "n": by_day.get(d, 0)})
        d += timedelta(days=1)
    busiest = max(days, key=lambda x: (x["n"], x["date"])) if days else None
    recent = [dict(r) for r in connection.execute(
        text(
            """
            SELECT a.role_key, a.kind, a.step_id, a.ended_at, a.code_task_run_id,
                   a.workflow_run_id
            FROM agent_acts a
            WHERE a.project_id = :p AND a.ended_at IS NOT NULL AND a.ended_at >= :since
              AND a.role_key IS NOT NULL AND a.kind = ANY(:kinds) AND a.status = 'complete'
              AND (a.code_task_run_id IS NOT NULL OR a.workflow_run_id IS NOT NULL)
            ORDER BY a.ended_at DESC
            LIMIT :lim
            """
        ), {**base, "lim": PULSE_SCAN}).mappings()]
    builds_ids = sorted({str(r["code_task_run_id"]) for r in recent if r["code_task_run_id"]})
    run_ids = sorted({str(r["workflow_run_id"]) for r in recent
                      if r["workflow_run_id"] and not r["code_task_run_id"]})
    builds: dict[str, dict[str, Any]] = {}
    if builds_ids:
        builds = {str(r["id"]): dict(r) for r in connection.execute(
            text("SELECT id, title, spec, state, finalize_ctx FROM code_task_runs "
                 "WHERE id = ANY(:ids)"),
            {"ids": builds_ids}).mappings()}
    runs: dict[str, dict[str, Any]] = {}
    if run_ids:
        runs = {str(r["id"]): dict(r) for r in connection.execute(
            text("SELECT r.id, r.input, d.workflow_key, d.name AS workflow_name, d.namespace "
                 "FROM workflow_runs r JOIN workflow_definitions d "
                 "ON d.id = r.workflow_definition_id WHERE r.id = ANY(:ids)"),
            {"ids": run_ids}).mappings()}
    # WHAT THE TEAM MADE, beside what it did (independent test, parity e): a pull request
    # opened, a research dossier, a panel's minutes or PRD — the artifacts acts carry — are
    # moves too, and the feed said only code-task steps.
    made = [dict(r) for r in connection.execute(
        text(
            """
            SELECT role_key, artifacts, label, ended_at, code_task_run_id, workflow_run_id,
                   meeting_run_id
            FROM agent_acts
            WHERE project_id = :p AND ended_at >= :since AND role_key IS NOT NULL
              AND jsonb_typeof(artifacts) = 'array' AND jsonb_array_length(artifacts) > 0
            ORDER BY ended_at DESC
            LIMIT :lim
            """
        ), {**base, "lim": PULSE_SCAN}).mappings()]
    candidates: list[tuple[str, dict[str, Any]]] = []
    for a in recent:
        verdict = None
        if a["code_task_run_id"]:
            run = builds.get(str(a["code_task_run_id"]))
            if not run:
                continue
            if is_internal_run(run):
                continue
            subject = factory_view.app_name(run)
            station = factory_view.STAGE_STATION.get(str(a["step_id"] or "").lower())
            verb = STATION_VERB.get(station or "", "worked on")
            if station == "review":
                state = factory_view._j(run.get("state"), {}) or {}
                verdict = factory_view._verdict(str(a["step_id"]), state, set())
            kind, rid = "build", str(run["id"])
        else:
            run = runs.get(str(a["workflow_run_id"]))
            if not run or str(run.get("namespace") or "system") != "product":
                continue
            subject = run_name(run)
            verb, kind, rid = "worked on", "run", str(run["id"])
        candidates.append((str(_iso(a["ended_at"])), {
            "role_key": str(a["role_key"]), "verb": verb, "subject": subject,
            "verdict": verdict, "at": _iso(a["ended_at"]), "run_id": rid, "kind": kind,
            "url": None}))
    for m in made:
        for art in (m["artifacts"] if isinstance(m["artifacts"], list) else []):
            if not isinstance(art, dict):
                continue
            words = made_words(str(art.get("kind") or ""), str(art.get("ref") or ""),
                               str(art.get("url") or ""), str(m["label"] or ""))
            if not words:
                continue
            verb, subject = words
            url = str(art.get("url") or "")
            candidates.append((str(_iso(m["ended_at"])), {
                "role_key": str(m["role_key"]), "verb": verb, "subject": subject,
                "verdict": None, "at": _iso(m["ended_at"]),
                "run_id": str(m["workflow_run_id"] or m["code_task_run_id"] or "") or None,
                "kind": "made", "url": url if url.startswith("http") else None}))
            break
    candidates.sort(key=lambda c: c[0], reverse=True)
    latest: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _at, item in candidates:
        if len(latest) >= PULSE_LATEST:
            break
        key = str(item["subject"]).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        latest.append(item)
    return {
        "weeks": PULSE_WEEKS,
        "start": first.isoformat(),
        "days": days,
        "total": sum(x["n"] for x in days),
        "busiest": dict(busiest) if busiest and busiest["n"] else None,
        "top": top,
        "latest": latest,
        "last_act_at": _iso(recent[0]["ended_at"]) if recent else None,
    }


_PR_NUM = __import__("re").compile(r"(?:#|/pull/)(\d+)")


def made_words(kind: str, ref: str, url: str, label: str) -> Optional[tuple[str, str]]:
    """An artifact as a move in the feed: (verb, subject). The subject is the act's own label
    after its kind ("Build · Ship it #182" → "Ship it #182"). None for kinds the feed skips."""
    subject = label.split("·", 1)[1].strip() if "·" in label else label.strip()
    if not subject:
        return None
    if kind == "pr":
        n = _PR_NUM.search(ref) or _PR_NUM.search(url)
        return (f"opened PR #{n.group(1)} for" if n else "opened a PR for"), subject
    if kind == "report":
        return "wrote a research dossier on", subject
    if kind == "minutes":
        return ("wrote the PRD for" if "prd" in (ref + " " + label).lower()
                else "wrote the minutes for"), subject
    return None


def pulse_memo(connection: Any, project_id: str, tz: str, now: datetime) -> dict[str, Any]:
    """`pulse`, reused for PULSE_MEMO_SECONDS per (project, zone)."""
    at = time.monotonic()
    key = (project_id, tz)
    with _PULSE_LOCK:
        hit = _PULSE_MEMO.get(key)
        if hit and at - hit[0] < PULSE_MEMO_SECONDS:
            return hit[1]
    value = pulse(connection, project_id, tz, now)
    with _PULSE_LOCK:
        _PULSE_MEMO[key] = (at, value)
    return value


def empty_pulse(now: datetime, tz: str) -> dict[str, Any]:
    start = pulse_start(now, tz).date()
    last = now.astimezone(ZoneInfo(tz)).date()
    return {"weeks": PULSE_WEEKS, "start": start.isoformat(),
            "days": [{"date": (start + timedelta(days=i)).isoformat(), "n": 0}
                     for i in range((last - start).days + 1)],
            "total": 0, "busiest": None, "top": [], "latest": [], "last_act_at": None}


# ── old system findings: one row, one Clean up ───────────────────────────────────────────────


def group_old_findings(attention: list[Mapping[str, Any]], now: datetime,
                       total: Optional[tuple[int, Optional[datetime]]] = None,
                       ) -> tuple[list[Mapping[str, Any]], Optional[dict[str, Any]]]:
    """ELEVEN CARDS OF HISTORY WERE ONE FACT. Shepherd's questions about builds stuck weeks ago
    ("stuck in `coding` … 53894 minutes. Let it continue, or cancel it?") filled Needs you on
    shared-server. Those older than OLD_FINDING — and only the ones system_doctor.clean_up_old can
    clear, so the row and its button agree — become one `cleanup` row. A critical finding and
    anything recent stay cards. Pure."""
    keep: list[Mapping[str, Any]] = []
    old: list[Mapping[str, Any]] = []
    for a in attention:
        at = _aware(datetime.fromisoformat(str(a["at"]))) if a.get("at") else None
        if (a.get("kind") == "system_health" and str(a.get("id") or "").startswith("shepherd:")
                and a.get("severity") != "critical" and at is not None
                and now - at > OLD_FINDING):
            old.append(a)
        else:
            keep.append(a)
    # THE TRUE COUNT (independent test): the cards are a bounded page of Shepherd's questions,
    # so the row counts every old open question in the table when the caller read it.
    n = len(old)
    since = min((_aware(datetime.fromisoformat(str(a["at"]))) for a in old), default=None)
    if total is not None and total[0] > n:
        n = total[0]
        since = _aware(total[1]) or since
    if not n or since is None:
        return keep, None
    return keep, {
        "count": n,
        "since": since.isoformat(),
        "ids": [str(a["id"]) for a in old],
        "title": f"{n} old {'build' if n == 1 else 'builds'} stuck since "
                 f"{since.strftime('%b')} {since.day}",
        "detail": "Shepherd can clear them safely",
    }


def old_questions(connection: Any, project_id: str, now: datetime) -> tuple[int, Optional[datetime]]:
    """How many of the project's open Shepherd questions are older than OLD_FINDING, and the
    oldest — every row, not the page the cards read."""
    row = connection.execute(
        text("SELECT count(*) AS n, min(created_at) AS since FROM shepherd_escalations "
             "WHERE project_id = :p AND status = 'open' AND created_at < :cut"),
        {"p": project_id, "cut": now - OLD_FINDING}).mappings().one()
    return int(row["n"] or 0), row["since"]


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
    previews = [dict(r) for r in connection.execute(
        text("SELECT status, created_at FROM app_previews WHERE project_id = :p "
             "ORDER BY created_at DESC LIMIT 5"),
        {"p": project_id},
    ).mappings()]
    deploy = connection.execute(
        text("SELECT status, created_at, finished_at FROM deployment_runs WHERE project_id = :p "
             "ORDER BY created_at DESC LIMIT 1"),
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

    # PREVIEWS: read-only over app_previews.
    previews = list(probe.get("previews") or [])
    if not previews:
        out.append({"key": "previews", "tone": "grey", "word": "Previews", "state": "Not set up",
                    "tip": "No preview yet", "href": "?area=code_tasks&view=apps"})
    else:
        latest = previews[0]
        running = sum(1 for p in previews if p.get("status") == "running")
        started = _aware(latest.get("created_at"))
        if latest.get("status") == "failed" and started and now - started <= timedelta(hours=24):
            out.append({"key": "previews", "tone": "red", "word": "Previews", "state": "Failed",
                        "tip": "The latest preview failed to start", "href": "?area=code_tasks&view=apps"})
        elif latest.get("status") == "starting" and started and now - started > timedelta(minutes=5):
            out.append({"key": "previews", "tone": "amber", "word": "Previews", "state": "Slow",
                        "tip": f"A preview has been starting for {_age((now - started).total_seconds())}",
                        "href": "?area=code_tasks&view=apps"})
        else:
            out.append({"key": "previews", "tone": "green", "word": "Previews", "state": "OK",
                        "tip": (f"{_plural(running, 'preview', 'previews')} running" if running
                                else "None running, none requested"),
                        "href": "?area=code_tasks&view=apps"})

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
        if status in DEPLOY_FAILED and at and now - at <= timedelta(days=7):
            out.append({"key": "deploys", "tone": "red", "word": "Deploys", "state": "Failed",
                        "tip": f"The latest deploy failed {ago} ago", "href": "?area=infra"})
        elif status in DEPLOY_OK or status in DEPLOY_FAILED:
            out.append({"key": "deploys", "tone": "green", "word": "Deploys", "state": "OK",
                        "tip": f"Last deploy {ago} ago", "href": "?area=infra"})
        else:
            out.append({"key": "deploys", "tone": "amber", "word": "Deploys", "state": "Deploying",
                        "tip": f"A deploy is in progress ({status.replace('_', ' ')})",
                        "href": "?area=infra"})
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
    # NOT RUNNING, STALLED: a live run with no progress for over a week (the live board's
    # `stalled`, the same rows the In flight card groups) is said for what it is.
    stalled = int(counts.get("stalled") or 0)
    if stalled:
        return {"key": "stalled", "text": f"{stalled} stalled", "tone": "amber",
                "tip": "Runs with no progress for over 7 days. Resume or close them."}
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
        "hours": [hours_[i] for i in (current - 1, current) if 0 <= i < 48],
        "today": [t["runs_finished"], t["runs_failed"], t["money_micros"], t["included_micros"],
                  t["ever_ran"], t["hours"][0]["start"]],
        "counts": [view["counts"].get(k) for k in ("running", "stalled", "needs_you", "stopped")],
        "pulse": [view["pulse"]["total"], view["pulse"]["start"], view["pulse"]["last_act_at"],
                  [(x["role_key"], x["n"]) for x in view["pulse"]["top"]],
                  [(x["run_id"], x["at"], x["verb"]) for x in view["pulse"]["latest"]]],
        "cleanup": (view.get("cleanup") or {}).get("ids"),
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

    host = _quiet(connection, "host advice", host_advice, connection, project_id,
                  default={"show": False}) or {"show": False}
    budget_full = _quiet(connection, "the budget", BudgetGovernor(settings).status_for_project,
                         connection, project_id, now=now, default={}) or {}

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
    live_runs = any(m["state"] not in ("done",) for m in board["missions"])
    ever = any_week or live_runs or bool(
        _quiet(connection, "whether anything ran", ever_ran, connection, project_id, default=False))
    team_pulse = _quiet(connection, "the pulse", pulse_memo, connection, project_id, zone, now,
                        default=None) or empty_pulse(now, zone)
    if not today["last_work_at"]:
        # The newest finished agent step (the pulse's), else the newest agent presence.
        idle = [a.get("since") for a in board["agents"] if a.get("since")]
        today["last_work_at"] = team_pulse.get("last_act_at") or (max(idle) if idle else None)
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
                 if m["state"] not in ("done", "stopped", "stalled", "waiting_you",
                                       "waiting_person"))
    runs_24h = int(today.get("runs_finished") or 0) + int(today.get("runs_failed") or 0) + moving
    old_total = _quiet(connection, "old questions", old_questions, connection, project_id, now,
                       default=None)
    attention, cleanup = group_old_findings(board["attention"], now, old_total)
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
        "attention": attention,
        "missions": board["missions"],
        "outcomes": board["outcomes"],
        "recent": cc["recent"],
        "teams": board["teams"],
        "agents": board["agents"],
        "today": today,
        "week": week,
        "pulse": team_pulse,
        "cleanup": cleanup,
        "budget": budget,
        "host": {k: v for k, v in host.items() if k != "gauges"},
        "systems": dots,
        "budget_refusal": refusal,
    }
    view["etag"] = etag_of(view, board["etag"])
    return view
