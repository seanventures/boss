"""Is what BOSS deployed still working?

`deployment_runs` records the ACT of deploying. A run that finished "succeeded" stays
succeeded forever, so the console could say "deployed to staging" about an app that fell over
an hour ago. Deployment status and health are different questions, and BOSS only ever answered
the first one.

Three layers, kept apart on purpose:

  * PURE (uptime, latency, verdict, incidents) — functions of their arguments with the clock
    passed IN, so every metric is testable without sleeping, and a graph can never disagree
    with the number printed beside it.
  * PROBE — asks the destination, through the SAME `connector.observe()` the deployment gate
    uses. A second way to ask "is it serving" would drift from the first, which is the defect
    this codebase has paid for repeatedly (the preview resolver, the failure taxonomy, the
    test oracle).
  * SWEEP — records samples. Never raises: a monitoring system that can fail its caller has
    turned observation into an outage.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..config import Settings
from ..ports import sql
from .core import make_id

# A target is probed at most this often. Health is a background concern; hammering a customer
# host to draw a smoother graph is a cost paid by the thing being measured.
DEFAULT_INTERVAL_SECONDS = 120

# Raw samples are cheap but not free. Thirty days covers the 7-day window with room to ask
# "was this happening last month" without the table growing without bound.
RETENTION_DAYS = 30

# Consecutive failures before a target is DOWN rather than merely degraded. One failed probe is
# usually a network hiccup; calling that an outage trains people to ignore the status.
DOWN_AFTER_CONSECUTIVE_FAILURES = 3

# A reachable-but-slow app is neither up nor down, and collapsing it into "up" hides exactly
# the degradation worth catching early.
DEGRADED_LATENCY_MS = 2000

# "This target has something running on it." A target is a DESTINATION; it becomes a service
# only once a deployment has succeeded. Probing, scraping or reporting health for one that has
# never been deployed describes an empty slot as a broken app.
#
# Defined once and imported by everything that needs it — the sweep, the health view and the
# observability scrape config all have to agree, and three copies of this predicate would be
# three chances to disagree.
#
# TWO WAYS TO SHIP, ONE ANSWER. A registry/connector deploy is a `deployment_runs` row that
# succeeded; a stack deploy (a VM, a manifest, an approval) is a `stack_deployments` row that
# came up `healthy`. Counting only the first read every stack-deployed app as "Not deployed" in
# Observability while Infra showed it live (operator decision 2026-10-04): Infra, Observability,
# the sweep and the scrape config all read this one rule and `newest_deploys()`.
DEPLOYED_PREDICATE = (
    "(EXISTS (SELECT 1 FROM deployment_runs d "
    "         WHERE d.deployment_target_id = {alias}.id AND d.status IN ('succeeded', 'deployed'))"
    " OR EXISTS (SELECT 1 FROM stack_deployments sd "
    "            WHERE sd.target_id = {alias}.id AND sd.status = 'healthy'))")

#: The newest thing that went live on each target, whichever way it shipped.
_NEWEST_DEPLOYS_SQL = (
    "SELECT DISTINCT ON (t) t, id, commit_ref, finished_at, kind FROM ("
    "  SELECT deployment_target_id AS t, id, commit_ref, finished_at, 'registry' AS kind "
    "  FROM deployment_runs WHERE deployment_target_id = ANY(:ids) "
    "  AND status IN ('succeeded', 'deployed') "
    "  UNION ALL "
    "  SELECT target_id AS t, id, commit_sha AS commit_ref, "
    "         COALESCE(finished_at, updated_at, created_at) AS finished_at, 'stack' AS kind "
    "  FROM stack_deployments WHERE target_id = ANY(:ids) AND status = 'healthy'"
    ") live ORDER BY t, finished_at DESC NULLS LAST")


def newest_deploys(connection, target_ids: list[str]) -> dict[str, dict[str, Any]]:
    """target → {id, commit_ref, finished_at, kind} of its newest live deploy (registry run or
    healthy stack deploy). A target missing from the answer has never been deployed."""
    if not target_ids:
        return {}
    rows = connection.execute(sql(_NEWEST_DEPLOYS_SQL), {"ids": list(target_ids)}).mappings().all()
    return {r["t"]: dict(r) for r in rows}


# --------------------------------------------------------------------------- #
# Pure: metrics over samples. Clock passed in; no I/O.
# --------------------------------------------------------------------------- #
def uptime_pct(samples: list[dict], *, since: datetime) -> Optional[float]:
    """Percentage of probes in the window that succeeded, or None when there were none.

    None is NOT zero. A target nobody has probed yet is unknown, and rendering that as 0%
    reports an outage that never happened.
    """
    window = [s for s in samples if s["checked_at"] >= since]
    if not window:
        return None
    return round(100.0 * sum(1 for s in window if s["ok"]) / len(window), 2)


def latency_stats(samples: list[dict]) -> dict[str, Optional[int]]:
    """Average and p95 over SUCCESSFUL probes only — a timeout's duration is not a latency."""
    lat = sorted(s["latency_ms"] for s in samples if s["ok"] and s.get("latency_ms") is not None)
    if not lat:
        return {"avg_ms": None, "p95_ms": None}
    idx = min(len(lat) - 1, int(round(0.95 * (len(lat) - 1))))
    return {"avg_ms": int(sum(lat) / len(lat)), "p95_ms": int(lat[idx])}


def consecutive_failures(samples_newest_first: list[dict]) -> int:
    n = 0
    for s in samples_newest_first:
        if s["ok"]:
            break
        n += 1
    return n


def verdict(samples_newest_first: list[dict], *,
            degraded_latency_ms: int = DEGRADED_LATENCY_MS,
            window: int = 5) -> str:
    """up | degraded | down | unknown.

    `unknown` is a real state and stays distinct from `down`: never having looked is not the
    same as having looked and found it broken.
    """
    if not samples_newest_first:
        return "unknown"
    if consecutive_failures(samples_newest_first) >= DOWN_AFTER_CONSECUTIVE_FAILURES:
        return "down"
    recent = samples_newest_first[:window]
    if any(not s["ok"] for s in recent):
        return "degraded"
    latest = recent[0]
    if latest.get("latency_ms") is not None and latest["latency_ms"] > degraded_latency_ms:
        return "degraded"
    return "up"


def status_since(samples_newest_first: list[dict]) -> Optional[datetime]:
    """When the CURRENT verdict began — the answer to 'how long has it been like this'.

    Walks back while the verdict computed from that point still matches today's, so a target
    that has been up for a week reports a week rather than the age of its last probe.
    """
    if not samples_newest_first:
        return None
    current = verdict(samples_newest_first)
    boundary = samples_newest_first[0]["checked_at"]
    for i in range(1, len(samples_newest_first) + 1):
        if verdict(samples_newest_first[i - 1:]) != current:
            break
        boundary = samples_newest_first[i - 1]["checked_at"]
    return boundary


def incidents(samples_newest_first: list[dict]) -> list[dict[str, Any]]:
    """Closed and open outages, newest first.

    An incident opens on the probe that pushes the target to DOWN and closes on the next
    success. Degradation alone is visible in the graph but is not an incident — if a slow
    response paged someone, nobody would read the page.
    """
    ordered = list(reversed(samples_newest_first))  # oldest -> newest
    out: list[dict[str, Any]] = []
    run = 0
    open_at: Optional[datetime] = None
    first_error: Optional[str] = None
    for s in ordered:
        if not s["ok"]:
            run += 1
            if run == 1:
                first_error = s.get("error") or (f"HTTP {s['status_code']}"
                                                 if s.get("status_code") else "unreachable")
            if run == DOWN_AFTER_CONSECUTIVE_FAILURES and open_at is None:
                open_at = s["checked_at"]
        else:
            if open_at is not None:
                out.append({"started_at": open_at, "ended_at": s["checked_at"],
                            "duration_seconds": int((s["checked_at"] - open_at).total_seconds()),
                            "error": first_error})
                open_at = None
            run = 0
            first_error = None
    if open_at is not None:
        out.append({"started_at": open_at, "ended_at": None, "duration_seconds": None,
                    "error": first_error})
    return list(reversed(out))


# --------------------------------------------------------------------------- #
# Probe: ask the destination, through the connector the deploy gate already uses.
# --------------------------------------------------------------------------- #
class StackHealthProbe:
    """Health for a stack target: an HTTP GET of the address it serves on.

    A stack target has no deployment connector — it is deployed by stack_deploy over SSH with
    an approval — but once it is up it is just a web app at its public URL (or the URL its
    newest healthy deploy opened). The probe never SSHes: it asks the app, from outside, the
    way a visitor would. Kept out of the connector table on purpose, so deploy routing for
    stack targets is unchanged.
    """

    kind = "stack"

    def observe(self, target: dict, settings: Settings) -> dict:
        import urllib.error
        import urllib.request

        url = str(target.get("public_url") or target.get("stack_open_url")
                  or (target.get("domains") or [""])[0] or "").strip()
        if not url:
            return {"healthy": None, "reason": "no public URL declared"}
        if not url.startswith(("http://", "https://")):
            url = f"http://{url}"
        check = {"kind": "http", "url": url, "expected": "2xx-3xx"}
        try:
            req = urllib.request.Request(url, method="GET", headers={"User-Agent": "boss-health"})
            with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 — the operator's URL
                status = int(r.status)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
        except Exception as exc:  # noqa: BLE001 — unreachable IS the measurement
            return {"healthy": False, "checks": [{**check, "status": None, "error": str(exc)[:160]}]}
        return {"healthy": 200 <= status < 400, "checks": [{**check, "status": status}]}


#: A stack target's newest healthy deploy URL, for StackHealthProbe when no public_url is set.
STACK_OPEN_URL_COLUMN = (
    "(SELECT sd.open_url FROM stack_deployments sd WHERE sd.target_id = {alias}.id "
    " AND sd.status = 'healthy' ORDER BY sd.created_at DESC LIMIT 1) AS stack_open_url")


def probe(target: dict, settings: Settings) -> dict[str, Any]:
    """One health sample for one target. Never raises — unreachable IS the measurement."""
    from .deployment_connectors import connector_for

    conn = StackHealthProbe() if str(target.get("target_kind") or "").lower() == "stack" \
        else connector_for(target)
    if conn is None:
        return {"ok": False, "status_code": None, "latency_ms": None,
                "error": f"no connector for target_kind {target.get('target_kind')!r}"}

    started = time.monotonic()
    try:
        health = conn.observe(target, settings)
    except Exception as exc:  # noqa: BLE001 — a probe that throws is a failed probe
        return {"ok": False, "status_code": None,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "error": f"{type(exc).__name__}: {exc}"[:200]}

    latency_ms = int((time.monotonic() - started) * 1000)
    checks = health.get("checks") or []
    first = checks[0] if checks else {}
    # `healthy` is None when the target declares no check — unknown, not healthy.
    ok = bool(health.get("healthy"))
    return {
        "ok": ok,
        "status_code": first.get("status"),
        "latency_ms": latency_ms,
        "error": None if ok else (first.get("error") or health.get("reason")
                                  or f"expected {first.get('expected')}, got {first.get('status')}")[:200],
    }


def _due(connection, target_id: str, interval_seconds: int, now: datetime) -> bool:
    row = connection.execute(
        sql("SELECT checked_at FROM deployment_health_samples "
            "WHERE deployment_target_id = :t ORDER BY checked_at DESC LIMIT 1"),
        {"t": target_id}).mappings().first()
    if not row:
        return True
    return (now - row["checked_at"]).total_seconds() >= interval_seconds


def sweep(engine, settings: Settings, *, project_id: Optional[str] = None,
          interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
          now: Optional[datetime] = None) -> dict[str, Any]:
    """Probe every active target that is due and record a sample.

    Best-effort by design: this runs on the worker's idle path, and a monitoring sweep that can
    raise would turn "the dashboard is stale" into "the worker is down".
    """
    from ..db import begin

    now = now or datetime.now(timezone.utc)
    probed, skipped, failed = 0, 0, 0
    try:
        with begin(engine) as c:
            q = ("SELECT t.*, " + STACK_OPEN_URL_COLUMN.format(alias="t")
                 + " FROM deployment_targets t WHERE t.status = 'active'"
                 " AND " + DEPLOYED_PREDICATE.format(alias="t")
                 + (" AND t.project_id = :p" if project_id else ""))
            targets = [dict(r) for r in c.execute(
                sql(q), ({"p": project_id} if project_id else {})).mappings().all()]

        for t in targets:
            with begin(engine) as c:
                if not _due(c, t["id"], interval_seconds, now):
                    skipped += 1
                    continue
            sample = probe(t, settings)          # outside the transaction: it does network I/O
            with begin(engine) as c:
                c.execute(
                    sql("INSERT INTO deployment_health_samples "
                        "(id, project_id, deployment_target_id, checked_at, ok, status_code, "
                        " latency_ms, error) "
                        "VALUES (:id,:p,:t,:at,:ok,:sc,:ms,:err)"),
                    {"id": make_id("dhs"), "p": t["project_id"], "t": t["id"], "at": now,
                     "ok": sample["ok"], "sc": sample["status_code"],
                     "ms": sample["latency_ms"], "err": sample["error"]})
            probed += 1
            if not sample["ok"]:
                failed += 1

        if probed:
            with begin(engine) as c:
                c.execute(sql("DELETE FROM deployment_health_samples WHERE checked_at < :cut"),
                          {"cut": now - timedelta(days=RETENTION_DAYS)})
    except Exception as exc:  # noqa: BLE001
        return {"probed": probed, "skipped": skipped, "failed": failed,
                "error": f"{type(exc).__name__}: {exc}"[:200]}
    # The app's OWN metrics ride the same cadence and the same deployed-target list: an app
    # that declares `metrics:` in boss.deploy.yaml is scraped when due (app_metrics.sweep never
    # raises, so a broken metrics page cannot cost the health samples above).
    from . import app_metrics

    scraped = app_metrics.sweep(engine, settings, targets, now=now)
    # ...and so do the suspected incidents an app reports (incident_signals: never raises).
    from . import incident_signals

    signals = incident_signals.sweep(engine, settings, targets, now=now)
    # ALERT RULES ARE NOT EVALUATED HERE ANY MORE (BOSSAPP-598, R1.9): the monitor scheduler
    # (monitor_scheduler.tick, on its own clock and leases) evaluates every rule group. This
    # sweep only probes and scrapes, so a slow probe never delays an alert and vice versa.
    return {"probed": probed, "skipped": skipped, "failed": failed, **scraped, **signals}


# --------------------------------------------------------------------------- #
# Read: one assembled view, so the console cannot disagree with itself.
# --------------------------------------------------------------------------- #
def health_view(connection, project_id: str, *, now: Optional[datetime] = None,
                points: int = 60) -> dict[str, Any]:
    """Per-target health plus a rollup, assembled in one place.

    The sparkline is built from the SAME samples as the numbers beside it — computing them
    separately is how a graph ends up contradicting its own summary.
    """
    now = now or datetime.now(timezone.utc)
    targets = [dict(r) for r in connection.execute(
        sql("SELECT id, app_name, environment, repo, target_kind, target_ref, domains, status "
            "FROM deployment_targets WHERE project_id = :p AND status = 'active' "
            "ORDER BY app_name"), {"p": project_id}).mappings().all()]

    out = []
    for t in targets:
        samples = [dict(r) for r in connection.execute(
            sql("SELECT checked_at, ok, status_code, latency_ms, error "
                "FROM deployment_health_samples WHERE deployment_target_id = :t "
                "AND checked_at >= :cut ORDER BY checked_at DESC LIMIT 2000"),
            {"t": t["id"], "cut": now - timedelta(days=7)}).mappings().all()]

        began = status_since(samples)
        latest = samples[0] if samples else None
        # Last deployment of this target — the "which build, deployed when" half of the answer.
        dep = newest_deploys(connection, [t["id"]]).get(t["id"])

        # A target with no successful deployment is a DESTINATION, not a service. Reporting it
        # as "unknown" alongside running apps invites the reasonable question "why is this being
        # monitored when nothing was ever deployed to it?" — the honest answer is a distinct
        # state, not a shrug.
        status = "not_deployed" if not dep else verdict(samples)

        out.append({
            "target_id": t["id"], "app_name": t["app_name"], "environment": t["environment"],
            "url": (t["domains"] or [None])[0],
            "status": status,
            "status_since": began.isoformat() if began else None,
            "uptime_24h": uptime_pct(samples, since=now - timedelta(hours=24)),
            "uptime_7d": uptime_pct(samples, since=now - timedelta(days=7)),
            **latency_stats(samples),
            "last_checked": latest["checked_at"].isoformat() if latest else None,
            "last_latency_ms": latest["latency_ms"] if latest else None,
            "last_error": latest["error"] if latest else None,
            "sample_count": len(samples),
            # Oldest-first so the graph reads left to right like time does.
            "series": [{"at": s["checked_at"].isoformat(), "ok": s["ok"],
                        "ms": s["latency_ms"]} for s in reversed(samples[:points])],
            "incidents": [{**i,
                           "started_at": i["started_at"].isoformat(),
                           "ended_at": i["ended_at"].isoformat() if i["ended_at"] else None}
                          for i in incidents(samples)[:10]],
            "deployed_commit": dep["commit_ref"] if dep else None,
            "deployed_at": dep["finished_at"].isoformat() if dep and dep["finished_at"] else None,
            "deployment_run_id": dep["id"] if dep else None,
        })

    counts = {k: sum(1 for o in out if o["status"] == k)
              for k in ("up", "degraded", "down", "unknown", "not_deployed")}
    # not_deployed is deliberately NOT a health problem: an empty slot is not an outage, and
    # letting it colour the rollup would make onboarding a target look like breaking something.
    worst = ("down" if counts["down"] else "degraded" if counts["degraded"]
             else "up" if counts["up"] else "unknown" if counts["unknown"] else "not_deployed")
    return {"project_id": project_id, "targets": out, "summary": {**counts, "overall": worst,
                                                                  "total": len(out)}}
