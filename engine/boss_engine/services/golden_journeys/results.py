"""Golden journey results, one row per journey per day per host, and the green-day counter.

THE COUNTER (BOSSAPP-1285, docs/preview/FREEZE.md): consecutive green days. A day is green
only when EVERY golden journey was green that day, on every host the preview counts (both the
laptop install and the shared server). A journey that did not run — or is still pending on an
open PR — makes the day red, not "skipped". Any red resets the counter; the freeze lifts at 5.
Only real-engine rows count: the release gate's scripted runs prove the runner, not the product.

A RED STAYS RED. Running a journey again the same day after it went red does not make the day
green: a run somebody had to retry by hand is not green (FREEZE.md). The row keeps the red and
counts the runs.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..core import make_id
from . import definitions as d
from .runner import GREEN, RED

#: Consecutive green days that lift the feature freeze.
FREEZE_DAYS = 5
#: How far back the history (and the counter) looks.
WINDOW_DAYS = 30
STATUSES = ("green", "red", "pending", "not_run")
#: `run`: judged by this install's own run (counts). `posted`: sent to POST .../results by a
#: caller that is not the journey account — shown on the card, never counted.
SOURCES = ("run", "posted")
#: The most of one recorded call's answer kept in a row (the verdict already read all of it).
CALL_CAP = 4000
MAX_CALLS = 200


def _clip(text: str, cap: int = CALL_CAP) -> str:
    text = str(text or "")
    if len(text) <= cap:
        return text
    half = cap // 2
    return f"{text[:half]}\n…[{len(text) - cap} characters trimmed]…\n{text[-half:]}"


def stored_calls(calls: Any) -> str:
    """The calls as JSON that is always valid: each answer clipped BEFORE serialising (cutting
    the serialised string made invalid JSON, and the row was lost)."""
    out = []
    for c in list(calls or [])[:MAX_CALLS]:
        if not isinstance(c, dict):
            continue
        c = dict(c)
        for k in ("stdout", "stderr", "body"):
            if isinstance(c.get(k), str):
                c[k] = _clip(c[k])
        out.append(c)
    return json.dumps(out, default=str)


def rejudge(row: Mapping[str, Any], journey: d.Journey) -> tuple[bool, str]:
    """A green row is believed only if BOSS's verdict, run again here over the row's own calls
    and reply, accepts EVERY item. (ok, why not)."""
    import tempfile

    from ..acceptance import verdict as _verdict
    from ..acceptance.session import CommandRecord

    reply = str(row.get("reply") or "")
    calls = row.get("calls") or []
    if not reply or not calls:
        return False, "a green result must carry its calls and its reply"
    try:
        rec = CommandRecord(calls)
    except Exception as exc:  # noqa: BLE001
        return False, f"its calls cannot be read ({type(exc).__name__})"
    with tempfile.TemporaryDirectory(prefix="boss-journey-") as tree:
        judged = _verdict.judge([i.as_contract() for i in journey.acceptance], reply, {},
                                root=tree, exec_log=rec)
    items = judged.get("items") or []
    if judged.get("verdict") != "met" or len(items) != len(journey.acceptance) or any(
            i.get("status") != _verdict.MET for i in items):
        return False, (judged.get("why") or "the verdict does not accept every item")
    return True, ""


def this_host() -> str:
    """The label this install's rows carry (BOSS_JOURNEY_HOST; e.g. `laptop`, `boss-1`)."""
    return (os.environ.get("BOSS_JOURNEY_HOST") or "local").strip()[:64] or "local"


def counted_hosts(seen: Iterable[str]) -> list[str]:
    """The hosts a green day needs: BOSS_JOURNEY_HOSTS (comma list) when set, else every host
    that reported in the window, else this one."""
    named = [h.strip() for h in (os.environ.get("BOSS_JOURNEY_HOSTS") or "").split(",") if h.strip()]
    return named or sorted(set(seen)) or [this_host()]


def today(now: Optional[datetime] = None) -> date:
    return (now or datetime.now(timezone.utc)).date()


def record(connection: Connection, result: Mapping[str, Any], *, host: Optional[str] = None,
           day: Optional[date] = None, actor: str = "journeys",
           source: str = "run") -> dict[str, Any]:
    """Store one journey's result for its day. A red already stored that day stays red."""
    if source not in SOURCES:
        raise ValueError(f"source must be one of {list(SOURCES)}")
    status = str(result.get("status") or "")
    if status not in STATUSES:
        raise ValueError(f"status must be one of {list(STATUSES)}, got {status!r}")
    jid = str(result.get("journey_id") or "")
    if not jid:
        raise ValueError("journey_id is required")
    host = (host or result.get("host") or this_host())[:64]
    day = day or today()
    prior = connection.execute(text(
        "SELECT id, status, why, runs, source FROM golden_journey_results "
        "WHERE host = :h AND day = :d AND journey_id = :j"),
        {"h": host, "d": day, "j": jid}).mappings().first()
    row = {"h": host, "d": day, "j": jid, "t": str(result.get("title") or "")[:200],
           "m": str(result.get("mode") or "real")[:16], "s": status,
           "v": result.get("verdict"), "w": str(result.get("why") or "")[:2000],
           "i": json.dumps(result.get("items") or [], default=str),
           "c": stored_calls(result.get("calls")), "src": source,
           "sec": float(result.get("seconds") or 0), "a": actor[:120]}
    if prior is not None and prior["source"] == "run" and source == "posted":
        raise ValueError(f"{jid} was already judged by this install's own run on {day}; "
                         "a posted result does not replace it")
    if prior is not None and prior["source"] == "posted" and source == "run":
        prior = dict(prior, status=row["s"])          # the install's own run replaces it
    if prior is not None and prior["status"] == RED and status != RED:
        # The day stays red; say that it was run again and what the later run found.
        row.update(s=RED, w=(f"{prior['why']} (run again: {status})")[:2000])
    if prior is None:
        row["id"] = make_id("gjr")
        connection.execute(text(
            "INSERT INTO golden_journey_results (id, host, day, journey_id, title, mode, status, "
            "verdict, why, items, calls, seconds, recorded_by, source) VALUES (:id, :h, :d, :j, :t, "
            ":m, :s, :v, :w, CAST(:i AS JSONB), CAST(:c AS JSONB), :sec, :a, :src)"), row)
    else:
        row["id"] = prior["id"]
        connection.execute(text(
            "UPDATE golden_journey_results SET title = :t, mode = :m, status = :s, verdict = :v, "
            "why = :w, items = CAST(:i AS JSONB), calls = CAST(:c AS JSONB), seconds = :sec, "
            "recorded_by = :a, source = :src, runs = runs + 1, updated_at = now() WHERE id = :id"), row)
    return {"id": row["id"], "host": host, "day": day.isoformat(), "journey_id": jid,
            "status": row["s"]}


def _rows(connection: Connection, since: date) -> list[dict[str, Any]]:
    return [dict(r) for r in connection.execute(text(
        "SELECT host, day, journey_id, title, mode, status, verdict, why, items, seconds, runs, "
        "updated_at, recorded_by, source FROM golden_journey_results WHERE day >= :since ORDER BY day, journey_id"),
        {"since": since}).mappings()]


def day_states(rows: Iterable[Mapping[str, Any]], expected: Iterable[str], hosts: Iterable[str]
               ) -> dict[date, dict[str, Any]]:
    """Per day: green or not, and what kept it from green ("J3 red on boss-1", "J7 not run")."""
    expected, hosts = list(expected), list(hosts)
    by_day: dict[date, dict[tuple[str, str], str]] = {}
    for r in rows:
        if str(r.get("mode") or "real") != "real" or str(r.get("source") or "run") != "run":
            continue
        day = r["day"] if isinstance(r["day"], date) else date.fromisoformat(str(r["day"]))
        by_day.setdefault(day, {})[(r["host"], r["journey_id"])] = r["status"]
    out = {}
    for day, seen in by_day.items():
        short = []
        for h in hosts:
            for j in expected:
                st = seen.get((h, j))
                if st != GREEN:
                    short.append(f"{j} {st or 'not run'}" + (f" on {h}" if len(hosts) > 1 else ""))
        out[day] = {"green": not short, "short": short}
    return out


def green_streak(states: Mapping[date, Mapping[str, Any]], now: date) -> int:
    """Consecutive green days ending today — or yesterday, while today has not reported yet (a
    day still running is not red until it is over)."""
    day = now if now in states else now - timedelta(days=1)
    n = 0
    while states.get(day, {}).get("green"):
        n += 1
        day -= timedelta(days=1)
    return n


def summary(connection: Connection, *, journeys: Optional[list[d.Journey]] = None,
            now: Optional[datetime] = None) -> dict[str, Any]:
    """What the Govern card and the report show: today's result per journey and host, the
    counter, and the last days at a glance."""
    journeys = journeys if journeys is not None else d.load_all()
    day0 = today(now)
    rows = _rows(connection, day0 - timedelta(days=WINDOW_DAYS))
    hosts = counted_hosts(r["host"] for r in rows
                          if r.get("mode", "real") == "real" and r.get("source", "run") == "run")
    expected = [j.id for j in journeys]
    states = day_states(rows, expected, hosts)
    streak = green_streak(states, day0)
    todays = {(r["host"], r["journey_id"]): r for r in rows
              if (r["day"] if isinstance(r["day"], date) else date.fromisoformat(str(r["day"]))) == day0}
    listed = []
    for j in journeys:
        per_host = {}
        for h in hosts:
            r = todays.get((h, j.id))
            per_host[h] = ({"status": r["status"], "why": r["why"], "verdict": r["verdict"],
                            "seconds": r["seconds"], "runs": r["runs"], "mode": r["mode"],
                            "recorded_by": r.get("recorded_by"), "source": r.get("source"),
                            "items": r["items"],
                            "at": r["updated_at"].isoformat() if r.get("updated_at") else None}
                           if r else {"status": "not_run", "why": "no run today yet"})
        listed.append({**j.summary_row(), "today": per_host})
    history = [{"day": (day0 - timedelta(days=k)).isoformat(),
                **states.get(day0 - timedelta(days=k), {"green": False, "short": ["no run"]})}
               for k in range(13, -1, -1)]
    pending = [{"id": j.id, **(j.pending or {})} for j in journeys if j.is_pending]
    return {"today": day0.isoformat(), "hosts": hosts, "green_days": streak,
            "needed": FREEZE_DAYS, "freeze_lifted": streak >= FREEZE_DAYS,
            "journeys": listed, "pending": pending, "history": history}
