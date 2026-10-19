"""The monitor scheduler (BOSSAPP-598, Phase 1a): evaluation on its own clock, out of the health
sweep (spec §2, §4.1, R1.9).

  tick()          one pass, called by the worker's background thread through the API:
    1. groups     every watched app and service is a rule group (interval 60 s, phase spread by
                  hash(id) mod interval); a worker claims due groups FOR UPDATE SKIP LOCKED
                  with a lease of 2x the interval, so loops and workers scale with no
                  coordinator. Remote queries run outside transactions; state is written in one
                  short transaction per group (app_alerts.evaluate_subject). No backfill of
                  missed evaluations: lag is recorded and a slow group raises a meta-alert.
    2. synthetics HTTP / TCP / DNS checks and /metrics scrapes of services BOSS did not
                  deploy, through the outbound guard: public addresses, or a private one only
                  when it is a registered service on a registered host (else it would be an
                  SSRF probe). Results are blackbox-exporter series on the floor.
    3. quiet      external alerts whose source stopped talking for 24 h resolve, and say so.
    4. triage     the agent triages what fired (storm-folded); never awaited by anything.
    5. notify     plan (routing tree, group timings, one thread per incident) and deliver the
                  outbox. Notification never waits on triage.
    6. self-check meta-alerts: evaluator lag, the bundled Prometheus not answering, an outbox
                  backlog, an ingest refusal spike, the model not answering.

Never raises: monitoring never fails its caller.
"""
from __future__ import annotations

import hashlib
import json
import logging
import socket
import ssl
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlsplit

from ..ports import sql
from . import monitoring as mon
from .core import make_id

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 60
GROUPS_PER_TICK = 50
CHECKS_PER_TICK = 20
META_SERVICE = "BOSS (self-check)"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _phase(subject_id: str, interval: int) -> int:
    return int(hashlib.sha256(subject_id.encode()).hexdigest()[:8], 16) % max(1, interval)


# --------------------------------------------------------------------------- #
# Rule groups
# --------------------------------------------------------------------------- #
def ensure_groups(connection, now: Optional[datetime] = None) -> int:
    """One group per deployed app and per service with rules, made once."""
    from .deployment_health import DEPLOYED_PREDICATE

    now = now or _now()
    made = 0
    rows = connection.execute(sql(
        "SELECT t.id, t.project_id, t.app_name AS name, 'target' AS kind FROM deployment_targets t "
        "WHERE t.status = 'active' AND " + DEPLOYED_PREDICATE.format(alias="t")
        + " AND NOT EXISTS (SELECT 1 FROM rule_groups g WHERE g.subject_id = t.id) "
        "UNION ALL SELECT s.id, s.project_id, s.name, 'service' FROM monitored_services s "
        "WHERE s.deployment_target_id IS NULL AND EXISTS (SELECT 1 FROM app_alert_rules r "
        " WHERE r.service_id = s.id AND r.target_id IS NULL) "
        "AND NOT EXISTS (SELECT 1 FROM rule_groups g WHERE g.subject_id = s.id)")).mappings().all()
    for r in rows:
        made += connection.execute(sql(
            "INSERT INTO rule_groups (id, project_id, subject_id, subject_kind, name, interval_s, "
            " next_eval_at) VALUES (:i, :p, :s, :k, :n, :iv, :next) ON CONFLICT DO NOTHING"),
            {"i": make_id("rgrp"), "p": r["project_id"], "s": r["id"], "k": r["kind"],
             "n": r["name"] or r["id"], "iv": DEFAULT_INTERVAL_S,
             "next": now + timedelta(seconds=_phase(r["id"], DEFAULT_INTERVAL_S))}).rowcount
    return made


def claim(engine, owner: str, now: datetime, limit: int = GROUPS_PER_TICK) -> list[dict]:
    from ..db import begin

    with begin(engine) as c:
        return [dict(r) for r in c.execute(sql(
            "UPDATE rule_groups g SET lease_owner = :o, lease_until = :now + "
            " make_interval(secs => 2 * g.interval_s) WHERE id IN (SELECT id FROM rule_groups "
            " WHERE next_eval_at <= :now AND (lease_until IS NULL OR lease_until < :now) "
            " ORDER BY next_eval_at LIMIT :n FOR UPDATE SKIP LOCKED) RETURNING *"),
            {"o": owner, "now": now, "n": limit}).mappings().all()]


def run_group(engine, settings: Any, group: dict, now: datetime) -> dict[str, Any]:
    from ..db import begin
    from . import app_alerts

    started = time.monotonic()
    lag_ms = max(0, int((now - group["next_eval_at"]).total_seconds() * 1000))
    error = None
    out: dict[str, Any] = {}
    try:
        with begin(engine) as c:
            subj = app_alerts.subject(c, group["subject_id"])
        if subj is None:
            with begin(engine) as c:
                c.execute(sql("DELETE FROM rule_groups WHERE id = :i"), {"i": group["id"]})
            return {"gone": True}
        out = app_alerts.evaluate_subject(engine, settings, subj, now)
    except Exception as exc:  # noqa: BLE001 — one group never costs another
        log.exception("monitor: group %s failed", group.get("id"))
        error = f"{type(exc).__name__}: {exc}"[:300]
    took = int((time.monotonic() - started) * 1000)
    interval = int(group["interval_s"])
    # No backfill: the next evaluation is the next phase point after now.
    nxt = group["next_eval_at"] + timedelta(seconds=interval)
    while nxt <= now:
        nxt += timedelta(seconds=interval)
    with begin(engine) as c:
        c.execute(sql(
            "UPDATE rule_groups SET last_eval_at = :now, last_duration_ms = :d, last_lag_ms = :l, "
            " last_error = :e, next_eval_at = :next, lease_owner = NULL, lease_until = NULL "
            "WHERE id = :i"),
            {"i": group["id"], "now": now, "d": took, "l": lag_ms, "e": error, "next": nxt})
    return {"group": group["id"], "ms": took, "lag_ms": lag_ms, "error": error,
            "fired": len(out.get("fired") or [])}


# --------------------------------------------------------------------------- #
# Synthetic checks and scrapes, through the outbound guard
# --------------------------------------------------------------------------- #
def _registered_private(connection, project_id: str, host: str) -> bool:
    """A private address is allowed only when it is a registered service on a registered host."""
    host = (host or "").lower()
    rows = connection.execute(sql(
        "SELECT t.public_url, h.address FROM deployment_targets t LEFT JOIN deployment_hosts h "
        " ON h.id = t.host_id WHERE t.project_id = :p AND t.status <> 'retired'"),
        {"p": project_id}).all()
    for url, addr in rows:
        if url and (urlsplit(str(url)).hostname or "").lower() == host:
            return True
        if addr and str(addr).split("@")[-1].split(":")[0].lower() == host:
            return True
    return False


def _resolve_allowed(connection, project_id: str, host: str, port: int) -> list[str]:
    from ..identity.http import _is_public, _resolve

    try:
        addrs = _resolve(host, port)
    except OSError as exc:
        raise mon.IngestRefused(400, f"{host} does not resolve ({exc.strerror or exc})") from None
    public = [a for a in addrs if _is_public(a)]
    if public:
        return public
    if _registered_private(connection, project_id, host):
        return addrs
    raise mon.IngestRefused(403, f"{host} is a private address that is not a registered service "
                                 "on a registered host; BOSS does not probe it")


def check_http(connection, project_id: str, target: str, *, method: str = "GET",
               expect: Optional[dict] = None, timeout: float = 10.0) -> dict[str, Any]:
    """One HTTP probe, pinned to a checked address, no redirects followed."""
    import http.client

    expect = expect or {}
    parts = urlsplit(target)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return {"ok": False, "word": "Not an http(s) URL"}
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        ip = _resolve_allowed(connection, project_id, parts.hostname, port)[0]
    except mon.IngestRefused as refused:
        return {"ok": False, "word": refused.word, "refused": True}
    started = time.monotonic()
    out: dict[str, Any] = {}
    try:
        if parts.scheme == "https":
            ctx = ssl.create_default_context()
            raw = socket.create_connection((ip, port), timeout=timeout)
            sock = ctx.wrap_socket(raw, server_hostname=parts.hostname)
            cert = sock.getpeercert()
            conn = http.client.HTTPSConnection(parts.hostname, port, timeout=timeout,
                                               context=ctx)
            conn.sock = sock
            if cert and cert.get("notAfter"):
                out["cert_expiry"] = ssl.cert_time_to_seconds(cert["notAfter"])
        else:
            conn = http.client.HTTPConnection(ip, port, timeout=timeout)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        conn.request(method or "GET", path, headers={"Host": parts.netloc,
                                                     "User-Agent": "boss-synthetic"})
        resp = conn.getresponse()
        body = resp.read(256 * 1024)
        status = int(resp.status)
        conn.close()
    except Exception as exc:  # noqa: BLE001 — unreachable is the answer
        return {"ok": False, "duration": time.monotonic() - started,
                "word": f"No answer ({type(exc).__name__})"}
    duration = time.monotonic() - started
    want = expect.get("status")
    ok = (status == int(want)) if want else 200 <= status < 400
    word = f"HTTP {status} in {int(duration * 1000)} ms"
    contains = expect.get("contains")
    if ok and contains and str(contains).encode() not in body:
        ok, word = False, f"HTTP {status}, but the body does not contain {contains!r}"
    if out.get("cert_expiry") and out["cert_expiry"] - time.time() < 14 * 86400:
        word += "; TLS certificate expires within 14 days"
    return {"ok": ok, "status": status, "duration": duration, "word": word, **out}


def check_tcp(connection, project_id: str, target: str, timeout: float = 5.0) -> dict[str, Any]:
    host, _, port = target.rpartition(":")
    try:
        ip = _resolve_allowed(connection, project_id, host, int(port))[0]
    except (ValueError, mon.IngestRefused) as exc:
        return {"ok": False, "word": getattr(exc, "word", "Give host:port")}
    started = time.monotonic()
    try:
        socket.create_connection((ip, int(port)), timeout=timeout).close()
    except OSError as exc:
        return {"ok": False, "duration": time.monotonic() - started,
                "word": f"Port {port} did not answer ({type(exc).__name__})"}
    d = time.monotonic() - started
    return {"ok": True, "duration": d, "word": f"Port {port} answered in {int(d * 1000)} ms"}


def check_dns(connection, project_id: str, target: str) -> dict[str, Any]:
    started = time.monotonic()
    try:
        addrs = sorted({i[4][0] for i in socket.getaddrinfo(target, None)})
    except OSError as exc:
        return {"ok": False, "word": f"{target} does not resolve ({exc.strerror or exc})"}
    d = time.monotonic() - started
    return {"ok": bool(addrs), "duration": d, "word": f"Resolves to {len(addrs)} address(es)"}


def run_check(engine, settings: Any, check: dict, now: datetime) -> dict[str, Any]:
    from ..db import begin
    from . import app_alerts

    with begin(engine) as c:
        svc = mon.service(c, check["project_id"], check["service_id"])
        if svc is None:
            return {"gone": True}
        fn = {"http": lambda: check_http(c, check["project_id"], check["target"],
                                         method=check.get("method") or "GET",
                                         expect=mon._json(check.get("expect"), {}) or {}),
              "tcp": lambda: check_tcp(c, check["project_id"], check["target"]),
              "dns": lambda: check_dns(c, check["project_id"], check["target"])}[check["kind"]]
    res = fn()
    with begin(engine) as c:
        base = {"check_id": check["id"], "instance": check["target"], "module": check["kind"]}
        pts = [({"__name__": "probe_success", **base}, now, 1.0 if res["ok"] else 0.0)]
        if res.get("duration") is not None:
            pts.append(({"__name__": "probe_duration_seconds", **base}, now,
                        float(res["duration"])))
        if res.get("status") is not None:
            pts.append(({"__name__": "probe_http_status_code", **base}, now,
                        float(res["status"])))
        if res.get("cert_expiry"):
            pts.append(({"__name__": "probe_ssl_earliest_cert_expiry", **base}, now,
                        float(res["cert_expiry"])))
        mon.write_points(c, check["project_id"], svc, [
            (mon.stamp(lab, project_id=check["project_id"], svc=svc), t, v) for lab, t, v in pts],
            now=now)
        c.execute(sql(
            "UPDATE synthetic_checks SET last_run_at = :now, last_ok = :ok, last_word = :w, "
            " next_run_at = :next WHERE id = :i"),
            {"i": check["id"], "now": now, "ok": res["ok"], "w": res["word"][:300],
             "next": now + timedelta(seconds=int(check["interval_s"]))})
        mon.note_data(c, svc["id"], now, path="synthetic",
                      recognised=f"Synthetic check: {res['word']}")
        if not svc.get("deployment_target_id"):
            app_alerts.ensure_service_rules(c, mon.subject_of(svc))
    return res


def scrape_service(engine, settings: Any, svc: dict, now: datetime) -> dict[str, Any]:
    """A service's /metrics URL (Connect › Has /metrics), through the same guard."""
    from ..db import begin
    from . import metric_ingest

    url = (svc.get("onboarding") or {}).get("scrape_url")
    if not url:
        return {"skipped": True}
    parts = urlsplit(url)
    with begin(engine) as c:
        try:
            _resolve_allowed(c, svc["project_id"], parts.hostname or "",
                             parts.port or (443 if parts.scheme == "https" else 80))
        except mon.IngestRefused as refused:
            _scrape_word(c, svc["id"], refused.word, now)
            return {"ok": False, "word": refused.word}
    from ..identity.http import OutboundRefused, fetch
    try:
        resp = fetch("GET", url, headers={"Accept": "text/plain;version=0.0.4"},
                     purpose="monitoring.scrape")
        body = resp.body.decode("utf-8", "replace") if resp.status == 200 else None
        word = "" if body is not None else f"The page answered {resp.status}"
    except (OutboundRefused, Exception) as exc:  # noqa: BLE001
        body, word = None, f"Could not read the page ({type(exc).__name__}: {str(exc)[:80]})"
    with begin(engine) as c:
        if body is None:
            _scrape_word(c, svc["id"], word, now)
            return {"ok": False, "word": word}
        try:
            got = metric_ingest.ingest_exposition(c, settings, svc, body, now)
        except Exception as exc:  # noqa: BLE001
            _scrape_word(c, svc["id"], f"Not Prometheus text: {str(exc)[:120]}", now)
            return {"ok": False, "word": str(exc)[:200]}
        _scrape_word(c, svc["id"], got["word"], now)
    return {"ok": True, **got}


def _scrape_word(connection, service_id: str, word: str, now: datetime) -> None:
    connection.execute(sql(
        "UPDATE monitored_services SET onboarding = onboarding || jsonb_build_object("
        " 'scrape_word', CAST(:w AS TEXT), 'scraped_at', CAST(:at AS TEXT)) WHERE id = :s"),
        {"s": service_id, "w": word[:300], "at": now.isoformat()})


# --------------------------------------------------------------------------- #
# Self-check: meta-alerts (by email AND console) and the watchdog
# --------------------------------------------------------------------------- #
WATCHDOG = ("# HELP ALERTS Always firing: BOSS's dead-man's switch for an external monitor.\n"
            "# TYPE ALERTS gauge\n"
            'ALERTS{alertname="BossWatchdog",alertstate="firing",severity="none"} 1\n'
            "# TYPE boss_watchdog gauge\nboss_watchdog 1\n")


def meta_alert(connection, project_id: str, name: str, summary: str, firing: bool,
               now: datetime) -> Optional[str]:
    """A self-check alert on the project's BOSS (self-check) service."""
    from . import app_alerts

    svc = mon.find_or_create_service(connection, project_id, META_SERVICE, "boss")
    fp = app_alerts.fingerprint(svc["id"], name, {"boss_meta": "true"})
    open_id = connection.execute(sql(
        "SELECT id FROM app_alerts WHERE fingerprint = :f AND state = 'firing'"),
        {"f": fp}).scalar()
    if firing and not open_id:
        aid = make_id("alrt")
        connection.execute(sql(
            "INSERT INTO app_alerts (id, project_id, service_id, rule_kind, source_kind, "
            " fingerprint, alertname, labels, annotations, state, active_at, fired_at, "
            " last_eval_at, eval_source, group_key, triage_state, urgency) VALUES (:i, :p, :s, "
            " 'meta', 'synthetic', :f, :n, CAST(:l AS JSONB), CAST(:a AS JSONB), 'firing', :now, "
            " :now, :now, 'boss', 'boss_meta', 'off', 'high') ON CONFLICT DO NOTHING"),
            {"i": aid, "p": project_id, "s": svc["id"], "f": fp, "n": name, "now": now,
             "l": json.dumps({"severity": "critical", "boss_meta": "true", "layer": "boss"}),
             "a": json.dumps({"summary": summary})})
        mon.transition(connection, alert_id=aid, project_id=project_id, at=now,
                       from_state=None, to_state="firing", reason=summary, actor="evaluator")
        return aid
    if not firing and open_id:
        connection.execute(sql(
            "UPDATE app_alerts SET state = 'resolved', cleared_at = :now, cleared_reason = "
            " 'recovered', updated_at = :now WHERE id = :i"), {"i": open_id, "now": now})
        mon.transition(connection, alert_id=open_id, project_id=project_id, at=now,
                       from_state="firing", to_state="resolved", reason="self-check passed")
    return open_id


def self_check(connection, settings: Any, now: datetime) -> dict[str, bool]:
    projects = connection.execute(sql(
        "SELECT DISTINCT project_id FROM rule_groups UNION SELECT DISTINCT project_id FROM "
        "signal_sources UNION SELECT DISTINCT project_id FROM notifications WHERE created_at > "
        ":day"), {"day": now - timedelta(days=1)}).scalars().all()
    out = {}
    for pid in projects:
        lag = connection.execute(sql(
            "SELECT count(*) FROM rule_groups WHERE project_id = :p AND (last_lag_ms > "
            " 2000 * interval_s OR next_eval_at < :now - make_interval(secs => 2 * interval_s))"),
            {"p": pid, "now": now}).scalar()
        backlog = connection.execute(sql(
            "SELECT count(*) FROM notifications WHERE project_id = :p AND status IN ('pending', "
            " 'failed') AND created_at < :since"),
            {"p": pid, "since": now - timedelta(minutes=5)}).scalar()
        dead = connection.execute(sql(
            "SELECT count(*) FROM notifications WHERE project_id = :p AND status = 'dead' "
            "AND created_at > :since"), {"p": pid, "since": now - timedelta(days=1)}).scalar()
        refused = connection.execute(sql(
            "SELECT COALESCE(sum(c.refused), 0) FROM signal_source_counts c JOIN signal_sources s "
            " ON s.id = c.source_id WHERE s.project_id = :p AND c.hour >= :h"),
            {"p": pid, "h": (now - timedelta(hours=1)).replace(minute=0, second=0,
                                                              microsecond=0)}).scalar()
        no_model = connection.execute(sql(
            "SELECT count(*) FROM app_alerts WHERE project_id = :p AND triage_state = 'no_model' "
            "AND triage_attempt_at > :since"),
            {"p": pid, "since": now - timedelta(minutes=10)}).scalar()
        url = str(getattr(settings, "monitoring_prometheus_url", "") or "")
        from .app_alerts import _bundled_answers

        prom_down = bool(url) and not _bundled_answers(url)
        checks = {
            "BossEvaluatorLag": (int(lag or 0) > 0, f"{int(lag or 0)} rule group(s) are "
                                 "evaluating late (more than twice their interval)"),
            "BossOutboxBacklog": (int(backlog or 0) > 0 or int(dead or 0) > 0,
                                  f"{int(backlog or 0)} notification(s) waiting over 5 minutes, "
                                  f"{int(dead or 0)} undeliverable in the last day"),
            "BossIngestRefusals": (int(refused or 0) > 100, f"{int(refused or 0)} ingest "
                                   "requests refused in the last hour"),
            "BossModelNotAnswering": (int(no_model or 0) > 0, "Alerts could not be triaged: no "
                                      "model is answering (notifications still go out)"),
            "BossDatasourceDown": (prom_down, "The bundled Prometheus is not answering; rules "
                                   "fall back to BOSS's own floor"),
        }
        for name, (firing, summary) in checks.items():
            meta_alert(connection, pid, name, summary, firing, now)
            out[f"{pid}:{name}"] = firing
    return out


# --------------------------------------------------------------------------- #
# The tick
# --------------------------------------------------------------------------- #
def tick(engine, settings: Any, *, now: Optional[datetime] = None,
         owner: Optional[str] = None, adapter: Any = None) -> dict[str, Any]:
    from ..db import begin
    from . import app_alerts, notify

    now = now or _now()
    owner = owner or f"{socket.gethostname()}:{id(engine)}"
    out: dict[str, Any] = {"groups": 0, "checks": 0, "errors": []}

    def guard(name, fn):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — monitoring never fails its caller
            log.exception("monitor: %s failed", name)
            out["errors"].append(f"{name}: {type(exc).__name__}: {exc}"[:200])
            return None

    def setup():
        with begin(engine) as c:
            mon.ensure_target_services(c)
            ensure_groups(c, now)
    guard("groups", setup)
    for g in guard("claim", lambda: claim(engine, owner, now)) or []:
        run_group(engine, settings, g, now)
        out["groups"] += 1

    def checks():
        with begin(engine) as c:
            rows = [dict(r) for r in c.execute(sql(
                "UPDATE synthetic_checks SET next_run_at = :lease WHERE id IN (SELECT id FROM "
                " synthetic_checks WHERE enabled AND next_run_at <= :now ORDER BY next_run_at "
                " LIMIT :n FOR UPDATE SKIP LOCKED) RETURNING *"),
                {"now": now, "lease": now + timedelta(minutes=2), "n": CHECKS_PER_TICK}
            ).mappings().all()]
            scrapes = [dict(r) for r in c.execute(sql(
                "SELECT * FROM monitored_services WHERE onboarding ? 'scrape_url' AND ("
                " NOT onboarding ? 'scraped_at' OR (onboarding->>'scraped_at')::timestamptz < "
                " :due) LIMIT 20"), {"due": now - timedelta(seconds=60)}).mappings().all()]
        for chk in rows:
            guard("check", lambda chk=chk: run_check(engine, settings, chk, now))
            out["checks"] += 1
        for svc in scrapes:
            svc["onboarding"] = mon._json(svc.get("onboarding"), {}) or {}
            guard("scrape", lambda svc=svc: scrape_service(engine, settings, svc, now))
    guard("checks", checks)

    def quiet():
        with begin(engine) as c:
            out["quiet_resolved"] = app_alerts.quiet_resolve(c, now)
            mon.prune(c, now)
    guard("quiet", quiet)
    out["triaged"] = guard("triage", lambda: app_alerts.triage_pending(
        engine, settings, adapter=adapter)) or 0
    guard("self_check", lambda: _in_tx(engine, lambda c: self_check(c, settings, now)))

    def plan():
        with begin(engine) as c:
            pids = c.execute(sql(
                "SELECT DISTINCT project_id FROM app_alerts WHERE state = 'firing' OR "
                "(notified_at IS NOT NULL AND cleared_at > :since)"),
                {"since": now - timedelta(days=1)}).scalars().all()
        planned = 0
        for pid in pids:
            planned += (guard("plan", lambda pid=pid: _in_tx(
                engine, lambda c: notify.plan(c, settings, pid, now))) or {}).get("enqueued", 0)
        out["enqueued"] = planned
    guard("plan", plan)
    out["delivery"] = guard("deliver", lambda: notify.deliver_due(engine, settings, now=now))
    from . import bundled_monitoring

    out["bundled"] = guard("bundled", lambda: bundled_monitoring.tick(engine, settings, now))
    return out


def _in_tx(engine, fn):
    from ..db import begin

    with begin(engine) as c:
        return fn(c)


__all__ = ["WATCHDOG", "check_dns", "check_http", "check_tcp", "claim", "ensure_groups",
           "meta_alert", "run_check", "run_group", "scrape_service", "self_check", "tick"]
