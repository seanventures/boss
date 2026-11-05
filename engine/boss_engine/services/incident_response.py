"""An app's error-rate alert, carried through to a verified fix: incident response.

WHAT WAS THERE, AND WHY IT DID NOT ADD UP TO A RESPONSE
--------------------------------------------------------
`app_metrics` scraped a deployed app's own /metrics, and when more than the declared share of
requests failed it opened an `incidents` row — and stopped. The row named the app and a
percentage. It did not say which release was serving, which route was failing, or what the app
had logged; no event was emitted, so nothing could react to it; `ai_sre_overwatch` diagnoses
only incidents found by a registered telemetry source (there were none, so it returned early);
change correlation read the old one-container `deployment_runs`, never `stack_deployments`, where
the deployed commit actually lives; and `ReliabilityService.remediate` started a code task
against the deployment's default repository with a title-derived NEW app identity, so a fix
would have been built as a different app, from the wrong branch.

THIS MODULE IS THE MISSING MIDDLE, in five parts:

  * `on_alert`        — when the alert opens: record the serving release (commit, ref, when, by
                        whom), the previous healthy release, the failing routes and the build
                        lineage that owns the app, as incident evidence; emit `incident.opened`
                        (a dispatchable event: a subscription can start a workflow on it —
                        any but Incident Response, which only a rule's own Respond
                        automatically starts).
  * `investigate`     — correlate the three things an SRE looks at: the app's own metrics (which
                        routes fail, from when), its logs (read-only, from its containers on its
                        host, redacted), and the release diff (GitHub compare previous..serving).
                        Deterministic: the hypothesis says what the evidence shows and how sure
                        it is, and never more. The answer key of a controlled fault is never an
                        input — only what any operator could see.
  * recovery checks   — `start_recovery_check` / `look_at_recovery`: after the fix ships, the
                        SAME scrapes that raised the alert (BOSS's own monitor, not anything the
                        fixing agent says) must show the error rate back at the pre-incident
                        baseline on enough traffic, for a stability window.
  * `report`          — the timeline, the root cause, and what THIS response cost (its own model
                        calls: billed and subscription-included kept apart), as JSON + Markdown.
  * `fix_brief`       — the bug-framed brief the fix build receives.
"""
from __future__ import annotations

import json
import re
import shlex
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..ports import sql
from .core import emit_event, make_id

EVENT_OPENED = "incident.opened"
EVENT_RESOLVED = "incident.resolved"
#: The catalogue workflow that answers an app incident (workflow_catalogue "incident_response").
WORKFLOW_KEY = "incident_response"

#: How far before the alert's onset to read logs from, and how many lines per container.
LOG_LEAD_MINUTES = 10
LOG_TAIL_LINES = 3000
#: A release that went out this long before onset is still a candidate trigger.
RELEASE_LOOKBACK_MINUTES = 120
#: The pre-incident window the recovery check compares against.
BASELINE_MINUTES = 30
#: Recovered means at most this much worse than the pre-incident baseline (percentage points).
BASELINE_TOLERANCE_PP = 1.0
DEFAULT_STABLE_MINUTES = 3
DEFAULT_RECOVERY_TIMEOUT_MINUTES = 30

_ACTIVE_RUN_STATES = ("queued", "validating", "running", "waiting", "paused", "blocked")
_ISO = "%Y-%m-%dT%H:%M:%SZ"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).isoformat()
    return str(v)


def _dt(v: Any) -> Optional[datetime]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _json(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return {}
    return v if v is not None else {}


def _evidence(connection, incident_id: str, kind: str, *, ref: str = "",
              payload: Optional[dict] = None, at: Optional[datetime] = None) -> None:
    connection.execute(sql(
        "INSERT INTO incident_evidence (id, incident_id, kind, ref, payload, created_at) "
        "VALUES (:id, :inc, :kind, :ref, CAST(:payload AS JSONB), :at)"),
        {"id": make_id("ev"), "inc": incident_id, "kind": kind, "ref": ref[:200],
         "payload": json.dumps(payload or {}, default=str), "at": at or _now()})


def _incident(connection, incident_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(sql("SELECT * FROM incidents WHERE id = :i"),
                             {"i": incident_id}).mappings().first()
    if not row:
        return None
    out = dict(row)
    out["finalize_ctx"] = _json(out.get("finalize_ctx")) or {}
    return out


def _merge_ctx(connection, incident_id: str, patch: dict[str, Any]) -> None:
    connection.execute(sql(
        "UPDATE incidents SET finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) "
        "|| CAST(:p AS JSONB) WHERE id = :i"),
        {"i": incident_id, "p": json.dumps(patch, default=str)})


# --------------------------------------------------------------------------- #
# 1. The serving release, at the moment the alert opens.
# --------------------------------------------------------------------------- #
def deploy_context(connection, target_id: str) -> dict[str, Any]:
    """What is serving on this target, what served before it, and which build owns the app.

    `serving` is the newest HEALTHY stack deploy (the one app_metrics' declaration comes from);
    `previous` the newest healthy one before it with a DIFFERENT commit — the release to compare
    against. `extends_run_id` is the build lineage a fix must continue: the serving deploy's own
    build, else the newest deploy of this target that a build shipped (a release deployed from a
    branch by hand carries no build, but the app it belongs to does)."""
    rows = [dict(r) for r in connection.execute(sql(
        "SELECT id, status, commit_sha, ref, repository, created_by, created_at, finished_at, "
        "       code_task_run_id, workflow_run_id, manifest "
        "FROM stack_deployments WHERE target_id = :t "
        "ORDER BY created_at DESC LIMIT 50"), {"t": target_id}).mappings().all()]
    healthy = [r for r in rows if r["status"] == "healthy"]
    if not healthy:
        return {}
    serving = healthy[0]
    previous = next((r for r in healthy[1:] if r["commit_sha"] and
                     r["commit_sha"] != serving["commit_sha"]), None)
    owner = next((r for r in rows if r.get("code_task_run_id")), None)
    manifest = _json(serving.get("manifest")) or {}
    out = {
        "stack_deployment_id": serving["id"],
        "commit_sha": serving["commit_sha"],
        "ref": serving["ref"],
        "repository": serving["repository"],
        "deployed_at": _iso(serving["finished_at"] or serving["created_at"]),
        "deployed_by": serving["created_by"],
        "build_run_id": serving.get("code_task_run_id"),
        "extends_run_id": (owner or {}).get("code_task_run_id"),
        "compose_project": manifest.get("compose_project"),
        "app_dir": manifest.get("workdir") or manifest.get("dir"),
        "manifest_path": manifest.get("manifest_path"),
    }
    if previous:
        out.update({"previous_stack_deployment_id": previous["id"],
                    "previous_commit_sha": previous["commit_sha"],
                    "previous_deployed_at": _iso(previous["finished_at"]
                                                 or previous["created_at"])})
    return out


#: How many past occurrences an incident row keeps in finalize_ctx.occurrences.
OCCURRENCE_HISTORY = 20
_RANGE = re.compile(r"\[\s*([0-9][0-9a-z]*)\s*(?::[^\]]*)?\]")


def rule_settle_window(rule: Optional[dict]) -> timedelta:
    """How long a rule can keep firing on samples from BEFORE a fix: its own lookback (window
    minutes, the longest range selector in its expr) plus its `for` and keep_firing_for. Pure.
    A 10m rule still sees the pre-fix errors for 10 min after the fix, whatever the recovery
    check said (BOSSAPP-613)."""
    from . import promql_lite as pq

    rule = rule or {}
    params = rule.get("params") or {}
    look = float(params.get("window_minutes") or rule.get("window_minutes") or 5) * 60.0
    for m in _RANGE.finditer(str(rule.get("expr") or "")):
        try:
            look = max(look, pq.parse_duration(m.group(1)) / 1000.0)
        except Exception:  # noqa: BLE001 — not a duration (a label value in brackets)
            continue
    hold = float(rule.get("for_seconds") or 0) + float(rule.get("keep_firing_for_s") or 0)
    return timedelta(seconds=look + hold)


def settling_until(inc: dict, rule: Optional[dict]) -> Optional[datetime]:
    """A resolved incident whose fix was verified is SETTLING until the firing rule's own window
    has fully elapsed past the fix: a firing before then is the old occurrence's lagging signal,
    not a new one. None when the incident is not settling on a verified fix."""
    if inc.get("status") != "resolved":
        return None
    ver = (inc.get("finalize_ctx") or {}).get("verification") or {}
    if ver.get("state") != "recovered":
        return None
    fixed = _dt(ver.get("since")) or _dt(ver.get("at"))
    return fixed + rule_settle_window(rule) if fixed else None


def archive_occurrence(connection, incident_id: str, *, now: datetime) -> None:
    """Before a closed incident row is reused for a NEW occurrence, keep the one that ended —
    its opening, response, verification, alert and release — in finalize_ctx.occurrences, so
    the new occurrence never erases who did what last time."""
    inc = _incident(connection, incident_id)
    if not inc:
        return
    ctx = inc["finalize_ctx"]
    if not any(ctx.get(k) for k in ("occurrence", "response", "verification", "alert")):
        return
    ver = ctx.get("verification") or {}
    past = {"occurrence": ctx.get("occurrence") or _iso(inc.get("first_seen")),
            "first_seen": _iso(inc.get("first_seen")),
            "closed_at": ver.get("at") or _iso(inc.get("last_seen")),
            "status": inc.get("status"), "archived_at": _iso(now),
            **{k: ctx.get(k) for k in ("response", "verification", "alert", "deploy", "app")}}
    history = [h for h in (ctx.get("occurrences") or []) if isinstance(h, dict)]
    _merge_ctx(connection, incident_id,
               {"occurrences": (history + [past])[-OCCURRENCE_HISTORY:]})


def _response_in_flight(connection, project_id: str, incident_id: str) -> Optional[str]:
    return connection.execute(sql(
        "SELECT id FROM workflow_runs WHERE project_id = :p AND input->>'incident_id' = :i "
        "AND status = ANY(:s) ORDER BY created_at DESC LIMIT 1"),
        {"p": project_id, "i": incident_id, "s": list(_ACTIVE_RUN_STATES)}).scalar()


def on_alert(connection, target: dict, incident_id: str, *, action: str, window: dict,
             rule: dict, now: datetime) -> Optional[str]:
    """The alert opened (or reopened) this incident, or resolved it. Record the evidence and
    say so as an event. Returns the event id, or None when nothing was emitted.

    Called inside the scrape's transaction by app_metrics.evaluate_alert, in a savepoint there:
    a failure here never costs the scrape or the incident row."""
    project_id = target["project_id"]
    app = {"target_id": target["id"], "app_name": target.get("app_name"),
           "environment": target.get("environment")}
    if action in ("opened", "reopened"):
        deploy = deploy_context(connection, target["id"])
        alert = {"fired_at": _iso(now), "rule": rule,
                 "error_rate": window.get("error_rate"), "requests": window.get("requests"),
                 "errors": window.get("errors"), "routes": (window.get("routes") or [])[:8]}
        if rule.get("alert_id"):
            # The app_alerts record this evidence belongs to (and the mark that keeps the
            # schema's one-time backfill of older alerts from copying it again).
            alert["alert_id"] = rule["alert_id"]
        ref = str(rule.get("error_rate_percent") if rule.get("error_rate_percent") is not None
                  else rule.get("alert") or "")
        running = _response_in_flight(connection, project_id, incident_id)
        if running:
            # One response per incident at a time: an alert firing again while the fix is being
            # built is the same problem, not a second one. It is evidence, and nothing more: the
            # occurrence, the response and its verification stay the ones in flight (D2: a
            # re-fire mid-fix reset the opening, and the report said 4.5 min for an 11.7 min fix).
            _evidence(connection, incident_id, "alert", ref=ref, payload=alert, at=now)
            _evidence(connection, incident_id, "note", ref=running,
                      payload={"text": "Alert fired again while response "
                                       f"{running} is in flight; no second response."}, at=now)
            return None
        # A REOPENED incident is a new occurrence: the response, the verification and the
        # report of the last one must not be read as this one's.
        # An incident an app reported first keeps its reports (they are about this same
        # failure); it stops being reported-only the moment the monitor confirms it.
        reported_first = action == "opened" and bool(connection.execute(sql(
            "SELECT 1 FROM incident_evidence WHERE incident_id = :i AND kind = "
            "'customer_reports' LIMIT 1"), {"i": incident_id}).first())
        patch = {"app": app, "deploy": deploy, "alert": alert, "verification": None,
                 "response": None, "reported_only": False}
        if not reported_first:
            patch["occurrence"] = _iso(now)
        _merge_ctx(connection, incident_id, patch)
        _evidence(connection, incident_id, "alert", ref=ref, payload=alert, at=now)
        if deploy:
            _evidence(connection, incident_id, "deployment",
                      ref=str(deploy.get("commit_sha") or ""), payload=deploy, at=now)
        pct = round(float(window.get("error_rate") or 0) * 100.0, 1)
        title = (f"{target.get('app_name')} ({target.get('environment')}): {pct}% of requests "
                 f"failing")
        if rule.get("kind") not in (None, "error_rate"):
            # Another rule opened it (latency, health check, ...): say which, not a percentage.
            title = (f"{target.get('app_name')} ({target.get('environment')}): "
                     f"{rule.get('alert') or 'alert'} fired")
        return emit_event(connection, project_id, EVENT_OPENED, "monitor", "incident",
                          incident_id, {
                              "incident_id": incident_id, **app,
                              "commit_sha": deploy.get("commit_sha"),
                              "previous_commit_sha": deploy.get("previous_commit_sha"),
                              "extends_run_id": deploy.get("extends_run_id"),
                              "repository": deploy.get("repository"),
                              "error_rate_percent": pct,
                              "title": title,
                              # The directive a workflow started on this event reads.
                              "directive": f"Respond to the incident: {title}.",
                          })
    if action == "resolved":
        _evidence(connection, incident_id, "monitor_resolved", payload={
            "at": _iso(now), "error_rate": window.get("error_rate"),
            "requests": window.get("requests"), "rule": rule}, at=now)
        return emit_event(connection, project_id, EVENT_RESOLVED, "monitor", "incident",
                          incident_id, {"incident_id": incident_id, **app,
                                        "error_rate": window.get("error_rate")})
    return None


# --------------------------------------------------------------------------- #
# 2. Investigation: metrics x logs x release diff.
# --------------------------------------------------------------------------- #
_TB_FRAME = re.compile(r'File "([^"]+)", line (\d+)(?:, in (\S+))?')
_TB_EXC = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning)?)\s*:\s*(.*)$")


def _location(event: dict) -> str:
    for key in ("location", "where", "frame", "exc_location", "error_location"):
        if event.get(key):
            return str(event[key])[:200]
    file_, line = event.get("file") or event.get("filename"), event.get("line") or \
        event.get("lineno")
    if file_:
        return f"{file_}:{line}" if line else str(file_)
    tb = str(event.get("traceback") or event.get("exc_info") or event.get("stack") or "")
    frames = _TB_FRAME.findall(tb)
    if frames:
        f, n, _fn = frames[-1]
        return f"{f}:{n}"
    return ""


def _is_error(event: dict) -> bool:
    level = str(event.get("level") or event.get("levelname") or event.get("severity") or "")
    try:
        status = int(event.get("status") or event.get("status_code") or 0)
    except (TypeError, ValueError):
        status = 0
    return level.lower() in ("error", "critical", "fatal") or status >= 500 or \
        bool(event.get("exc_type") or event.get("exception"))


def parse_log_events(text: str) -> list[dict[str, Any]]:
    """Error events out of a container's log text: JSON lines the app writes (the brief's
    structured logs), and plain Python tracebacks for an app that writes none. Pure."""
    out: list[dict[str, Any]] = []
    tb: list[str] = []

    def flush_tb(exc_line: str) -> None:
        frames = _TB_FRAME.findall("\n".join(tb))
        exc = _TB_EXC.match(exc_line)
        out.append({"level": "error", "exc_type": exc.group(1) if exc else "Traceback",
                    "exc_message": exc.group(2)[:300] if exc else exc_line[:300],
                    "location": f"{frames[-1][0]}:{frames[-1][1]}" if frames else "",
                    "function": frames[-1][2] if frames and frames[-1][2] else ""})
        tb.clear()

    for raw in (text or "").splitlines():
        line = raw.strip()
        # `docker compose logs` prefixes "<service>-1  | "
        if " | " in line[:80] and not line.startswith("{"):
            line = line.split(" | ", 1)[1].strip()
        if tb:
            # Inside a traceback every line is a frame or source, until the first line that
            # names the exception (the one that does not start with "File" and is not code
            # indented under a frame).
            indented = raw[:1] in (" ", "\t") or raw.lstrip().startswith("File ")
            if line.startswith("File ") or (indented and not _TB_EXC.match(line)) or \
                    line.startswith("During handling") or not line:
                tb.append(line)
                continue
            flush_tb(line)
            continue
        if line.startswith("Traceback (most recent call last)"):
            tb.append(line)
            continue
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and _is_error(event):
            out.append(event)
    if tb:
        flush_tb("")
    return out


def error_signatures(events: list[dict[str, Any]], *, limit: int = 8) -> list[dict[str, Any]]:
    """Errors grouped by (exception, where, route): what keeps failing, and how often."""
    from .redaction import redact_secrets

    groups: dict[tuple, dict[str, Any]] = {}
    for e in events:
        exc = str(e.get("exc_type") or e.get("exception") or e.get("error") or "error")[:80]
        where = _location(e)
        route = " ".join(x for x in (str(e.get("method") or "").upper(),
                                     str(e.get("route") or e.get("path") or "")) if x)
        key = (exc, where, route)
        g = groups.setdefault(key, {"exc_type": exc, "location": where, "route": route,
                                    "count": 0, "first": None, "last": None, "sample": ""})
        g["count"] += 1
        ts = str(e.get("ts") or e.get("timestamp") or e.get("time") or "")
        if ts:
            g["first"] = g["first"] or ts
            g["last"] = ts
        if not g["sample"]:
            msg = str(e.get("exc_message") or e.get("message") or e.get("msg") or "")
            g["sample"] = redact_secrets(msg)[:300]
    return sorted(groups.values(), key=lambda g: -g["count"])[:limit]


def read_app_logs(access: Any, compose_project: str, since: datetime) -> dict[str, Any]:
    """The app's containers' logs since `since`, over the host's READ-ONLY channel. Only the
    containers of this app's own compose project are read; nothing is written."""
    if not compose_project or not re.match(r"^[A-Za-z0-9._-]+$", compose_project):
        return {"ok": False, "word": "This app runs no compose project BOSS can name",
                "text": ""}
    q = shlex.quote(f"label=com.docker.compose.project={compose_project}")
    stamp = since.astimezone(timezone.utc).strftime(_ISO)
    cmd = (f"for c in $(docker ps --filter {q} --format '{{{{.Names}}}}'); do "
           f"echo \"===== $c\"; docker logs --since {shlex.quote(stamp)} "
           f"--tail {LOG_TAIL_LINES} \"$c\" 2>&1; done")
    try:
        res = access.run(cmd, timeout=60, write=False)
    except Exception as exc:  # noqa: BLE001 — the investigation says what it could not read
        return {"ok": False, "word": f"Could not read the app's logs: {str(exc)[:160]}",
                "text": ""}
    from .redaction import redact_secrets

    text = redact_secrets(str(res.stdout or ""))
    containers = [x[6:].strip() for x in text.splitlines() if x.startswith("===== ")]
    return {"ok": res.returncode == 0, "containers": containers, "text": text,
            "word": f"Read {len(text.splitlines())} log lines from {len(containers)} "
                    f"container(s) since {stamp}"}


def release_diff(github: Any, base: Optional[str], head: Optional[str]) -> dict[str, Any]:
    """Commits and files between the previous healthy release and the serving one."""
    if not github or not base or not head or base == head:
        return {"ok": False, "word": "No earlier release to compare with", "commits": [],
                "files": []}
    try:
        got = github.compare_summary(base, head)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "word": f"Could not compare the releases: {str(exc)[:160]}",
                "commits": [], "files": []}
    return {"ok": True, **got,
            "word": f"{len(got.get('commits') or [])} commit(s), "
                    f"{len(got.get('files') or [])} file(s) changed"}


def _suffix_match(path: str, files: list[str]) -> Optional[str]:
    """The changed file a traceback path names. Container paths (/app/app/pricing.py) and repo
    paths (order-api/app/pricing.py) agree on their tail."""
    p = path.split(":")[0].replace("\\", "/").strip()
    if not p:
        return None
    parts = [x for x in p.split("/") if x]
    for n in range(len(parts), 0, -1):
        tail = "/".join(parts[-n:])
        for f in files:
            if f == tail or f.endswith("/" + tail):
                return f
    return None


def hypothesis(deploy: dict, alert: dict, signatures: list[dict], diff: dict, *,
               onset: Optional[datetime], onset_was: str = "the alert fired") -> dict[str, Any]:
    """The root cause the evidence supports, with its basis and confidence. Pure.

    high    the failing frame is in a file the serving release changed, and the release went
            out shortly before onset;
    medium  the release went out shortly before onset, but the failing frame is not in its diff
            (or there is no frame);
    low     no release near onset: the cause is not a change BOSS shipped."""
    commit = str(deploy.get("commit_sha") or "")
    deployed_at = _dt(deploy.get("deployed_at"))
    near = bool(deployed_at and onset and timedelta(0) <= onset - deployed_at
                <= timedelta(minutes=RELEASE_LOOKBACK_MINUTES))
    lag = round((onset - deployed_at).total_seconds() / 60.0, 1) if near else None
    top = signatures[0] if signatures else None
    files = [f["filename"] for f in (diff.get("files") or [])]
    in_diff = _suffix_match(top["location"], files) if top and top.get("location") else None
    route = (alert.get("routes") or [{}])[0] if alert.get("routes") else {}
    failing_route = route.get("route") if route and route.get("errors") else None

    if near and in_diff:
        confidence, basis = 0.9, "release_changed_failing_frame"
    elif near:
        confidence, basis = 0.6, "release_before_onset"
    else:
        confidence, basis = 0.3, "no_release_near_onset"
    parts = []
    if near:
        parts.append(f"Release {commit[:7]} went out {lag} min before {onset_was}")
    if top:
        parts.append(f"the app logs {top['count']}x {top['exc_type']}"
                     + (f" at {top['location']}" if top.get("location") else "")
                     + (f" on {top['route']}" if top.get("route") else ""))
    if in_diff:
        parts.append(f"{in_diff} is one of the {len(files)} file(s) that release changed")
    if failing_route:
        parts.append(f"{failing_route} carries the failures "
                     f"({round(float(route.get('error_rate') or 0) * 100, 1)}% of its requests)")
    statement = "; ".join(parts) + "." if parts else "No change, log or route explains it yet."
    return {"root_cause": statement, "basis": basis, "confidence": confidence,
            "suspect_commit": commit if near else None, "suspect_file": in_diff,
            "failing_route": failing_route,
            "exception": top["exc_type"] if top else None,
            "location": top.get("location") if top else None,
            "minutes_after_release": lag}


def fix_brief(incident: dict, ctx: dict, hyp: dict, signatures: list[dict], diff: dict,
              logs_word: str) -> str:
    """The bug-framed brief the fix build gets: the evidence, then what to do. Markdown."""
    deploy, alert = ctx.get("deploy") or {}, ctx.get("alert") or {}
    commit = str(deploy.get("commit_sha") or "")
    lines = [
        "INCIDENT FIX — reproduce, find the root cause, fix it minimally, prove it.",
        "",
        f"Incident: {incident.get('title')}",
        f"Service: {incident.get('service')} ({(ctx.get('app') or {}).get('environment')})",
        f"Serving release: {commit[:12]} (ref {deploy.get('ref')}, deployed "
        f"{deploy.get('deployed_at')})",
    ]
    if deploy.get("previous_commit_sha"):
        lines.append(f"Previous healthy release: {str(deploy['previous_commit_sha'])[:12]}")
    lines += ["", "## What BOSS's monitor saw",
              f"- {round(float(alert.get('error_rate') or 0) * 100, 1)}% of "
              f"{alert.get('requests')} requests failed (alert above "
              f"{(alert.get('rule') or {}).get('error_rate_percent')}% over "
              f"{(alert.get('rule') or {}).get('window_minutes')} min)"]
    for r in (alert.get("routes") or [])[:5]:
        if r.get("errors"):
            lines.append(f"- {r['route']}: {int(r['errors'])} of {int(r['requests'])} failed")
    lines += ["", "## What the app logged", f"_{logs_word}_"]
    for s in signatures[:5]:
        lines.append(f"- {s['count']}x `{s['exc_type']}`"
                     + (f" at `{s['location']}`" if s.get("location") else "")
                     + (f" on {s['route']}" if s.get("route") else "")
                     + (f": {s['sample']}" if s.get("sample") else ""))
    lines += ["", "## What changed in the serving release", f"_{diff.get('word')}_"]
    for c in (diff.get("commits") or [])[:10]:
        lines.append(f"- {str(c.get('sha'))[:7]} {c.get('message')}")
    for f in (diff.get("files") or [])[:20]:
        lines.append(f"  - {f['status']} {f['filename']} (+{f['additions']}/-{f['deletions']})")
    suspect = hyp.get("suspect_file")
    patch = next((f.get("patch") for f in (diff.get("files") or [])
                  if f["filename"] == suspect and f.get("patch")), None)
    if patch:
        lines += ["", f"Patch of {suspect} in that release:", "```diff", patch[:6000], "```"]
    lines += ["", "## Hypothesis", f"{hyp['root_cause']} (basis: {hyp['basis']}, "
                                    f"confidence {hyp['confidence']})",
              "", "## What to do",
              f"1. Write a regression test that reproduces the failure and FAILS on {commit[:12]} "
              "— the exact input shape the failing requests carry.",
              "2. Make the smallest change that fixes the root cause. Do not refactor, rename or "
              "reformat anything else; every existing test must still pass.",
              "3. In the pull request description, state the root cause in one paragraph "
              "(the commit and line that introduced it, and why), the regression test, and "
              "the fix.",
              "If the evidence does not support this hypothesis, say so in the pull request "
              "and fix what the evidence does show — never guess."]
    return "\n".join(lines)


def _github_for(connection, settings: Any, project_id: str, repository: str):
    from .github_adapter import GitHubAdapter
    from .github_app_adapter import repo_token

    owner, _, repo = (repository or "").partition("/")
    if not owner or not repo:
        return None
    token = repo_token(settings, owner, repo, connection=connection, project_id=project_id)
    return GitHubAdapter(settings, owner=owner, repo=repo, token=token)


def _host_for_target(connection, target_id: str) -> Optional[dict]:
    row = connection.execute(sql(
        "SELECT h.* FROM deployment_targets t JOIN deployment_hosts h ON h.id = t.host_id "
        "WHERE t.id = :t"), {"t": target_id}).mappings().first()
    return dict(row) if row else None


def investigate(connection, settings: Any, project_id: str, incident_id: str, *,
                host_access: Any = None, github: Any = None,
                now: Optional[datetime] = None) -> dict[str, Any]:
    """Correlate metrics, logs and the release diff for an app incident; persist the evidence
    and the hypothesis; return the fix brief. `host_access` / `github` are injectable for tests;
    by default they are resolved from the incident's own target and repository."""
    inc = _incident(connection, incident_id)
    if not inc or inc["project_id"] != project_id:
        raise LookupError(f"incident {incident_id!r} not found")
    ctx = inc["finalize_ctx"]
    app, deploy, alert = ctx.get("app") or {}, ctx.get("deploy") or {}, ctx.get("alert") or {}
    if not app.get("target_id"):
        raise ValueError("This incident was not raised by an app's own monitor; there is no app "
                         "to investigate. Use the reliability scan for telemetry incidents.")
    onset = _dt(ctx.get("occurrence")) or inc["first_seen"]
    now = now or _now()

    logs = {"ok": False, "word": "Logs not read", "text": ""}
    if deploy.get("compose_project"):
        try:
            if host_access is None:
                host = _host_for_target(connection, app["target_id"])
                if host:
                    from .stack_deploy import host_access_for
                    host_access = host_access_for(connection, settings, host)
            if host_access is not None:
                logs = read_app_logs(host_access, deploy["compose_project"],
                                     onset - timedelta(minutes=LOG_LEAD_MINUTES))
            else:
                logs = {"ok": False, "word": "The app's host is not known", "text": ""}
        except Exception as exc:  # noqa: BLE001 — say what could not be read, carry on
            logs = {"ok": False, "word": f"Could not read the app's logs: {str(exc)[:160]}",
                    "text": ""}
    signatures = error_signatures(parse_log_events(logs.get("text") or ""))

    if github is None and deploy.get("repository"):
        try:
            github = _github_for(connection, settings, project_id, deploy["repository"])
        except Exception:  # noqa: BLE001 — the diff is optional evidence
            github = None
    diff = release_diff(github, deploy.get("previous_commit_sha"), deploy.get("commit_sha"))
    # NAME THE ONSET FOR WHAT IT IS. An incident a customer reported first starts at the first
    # report, not at the alert (D2: "0.8 min before the alert fired" — the alert came 6.9 min
    # after the release; the first report 0.8 min after).
    #
    # THIS OCCURRENCE ONLY. An incident row is reused across occurrences: resolved, then
    # fired again later by the alert alone, with no new report. Without the lower bound, a
    # customer_reports row from a PRIOR, already-closed occurrence — always "created_at <=
    # onset" simply by being in the past — made every later, alert-only re-fire say "the
    # first customer report" too, blaming the wrong release on a report about the old one
    # (Order API, 2026-10-18). `since` is when THIS occurrence began (ingest() and
    # open_or_reopen both set it from the very moment that opened it), so a report only
    # counts here when it is no older than that.
    since = _occurrence(ctx)
    reported_first = bool(connection.execute(sql(
        "SELECT 1 FROM incident_evidence WHERE incident_id = :i AND kind = 'customer_reports' "
        "AND created_at <= :t AND (CAST(:s AS timestamptz) IS NULL OR created_at >= :s) "
        "LIMIT 1"), {"i": incident_id, "t": onset, "s": since}).first())
    hyp = hypothesis(deploy, alert, signatures, diff, onset=onset,
                     onset_was=("the first customer report" if reported_first
                                else "the alert fired"))
    brief = fix_brief(inc, ctx, hyp, signatures, diff, logs.get("word") or "")

    _evidence(connection, incident_id, "logs", ref=deploy.get("compose_project") or "",
              payload={"word": logs.get("word"), "containers": logs.get("containers") or [],
                       "signatures": signatures}, at=now)
    _evidence(connection, incident_id, "release_diff", ref=str(deploy.get("commit_sha") or ""),
              payload={"word": diff.get("word"), "commits": diff.get("commits") or [],
                       "files": [{k: f.get(k) for k in ("filename", "status", "additions",
                                                        "deletions")}
                                 for f in diff.get("files") or []]}, at=now)
    _evidence(connection, incident_id, "rca_hypothesis", ref=hyp["basis"], payload=hyp, at=now)
    connection.execute(sql(
        "UPDATE incidents SET status = CASE WHEN status IN ('open', 'triaged') THEN 'diagnosing' "
        "ELSE status END, confidence = :c, last_seen = :now WHERE id = :i"),
        {"c": hyp["confidence"], "now": now, "i": incident_id})
    title = (f"Fix {inc.get('service')}: {hyp.get('exception') or 'failing requests'}"
             + (f" on {hyp['failing_route']}" if hyp.get("failing_route") else ""))[:150]
    return {"incident_id": incident_id, "title": title, "incident_brief": brief,
            "root_cause": hyp["root_cause"], "hypothesis": hyp,
            "suspect_commit": hyp.get("suspect_commit"),
            "extends_run_id": deploy.get("extends_run_id"), "signatures": signatures,
            # The fix is cut from THIS commit, on a branch of its own (release_scope).
            "serving_commit_sha": deploy.get("commit_sha"),
            "logs_word": logs.get("word"), "diff_word": diff.get("word"),
            "target_id": app.get("target_id")}


# --------------------------------------------------------------------------- #
# 3. Recovery, judged by BOSS's own monitor.
# --------------------------------------------------------------------------- #
def _window(connection, target_id: str, start: datetime, end: datetime) -> dict[str, Any]:
    from . import app_metrics

    return app_metrics.aggregate(app_metrics._window_rows(connection, target_id, start, end))


def baseline(connection, target_id: str, onset: datetime) -> dict[str, Any]:
    """The error rate before the incident: the documented baseline recovery returns to."""
    # STRICTLY before onset: the scrape that fired the alert is the incident, not its baseline.
    w = _window(connection, target_id, onset - timedelta(minutes=BASELINE_MINUTES),
                onset - timedelta(microseconds=1))
    return {"error_rate": w.get("error_rate") or 0.0, "requests": w.get("requests") or 0,
            "minutes": BASELINE_MINUTES}


def start_recovery_check(connection, project_id: str, incident_id: str, *,
                         since: Optional[datetime] = None, workflow_run_id: Optional[str] = None,
                         stable_minutes: float = DEFAULT_STABLE_MINUTES,
                         timeout_minutes: float = DEFAULT_RECOVERY_TIMEOUT_MINUTES,
                         now: Optional[datetime] = None) -> dict[str, Any]:
    """Record a recovery check on the incident's app; the workflow step parks on it."""
    inc = _incident(connection, incident_id)
    if not inc or inc["project_id"] != project_id:
        raise LookupError(f"incident {incident_id!r} not found")
    ctx = inc["finalize_ctx"]
    target_id = (ctx.get("app") or {}).get("target_id")
    if not target_id:
        raise ValueError("This incident has no app monitor to verify recovery against.")
    now = now or _now()
    onset = _dt(ctx.get("occurrence")) or inc["first_seen"]
    base = baseline(connection, target_id, onset)
    from . import app_metrics

    decl = app_metrics.declarations(connection, [target_id]).get(target_id) or {}
    rule = app_metrics.alert_rule(decl.get("spec"))
    check_id = make_id("rcv")
    connection.execute(sql(
        "INSERT INTO incident_recovery_checks (id, project_id, incident_id, target_id, "
        " workflow_run_id, since, deadline, stable_minutes, baseline, rule, status, summary) "
        "VALUES (:id, :p, :i, :t, :w, :since, :deadline, :stable, CAST(:b AS JSONB), "
        " CAST(:r AS JSONB), 'waiting', :s)"),
        {"id": check_id, "p": project_id, "i": incident_id, "t": target_id,
         "w": workflow_run_id, "since": since or now,
         "deadline": now + timedelta(minutes=float(timeout_minutes)),
         "stable": float(stable_minutes), "b": json.dumps(base), "r": json.dumps(rule),
         "s": "Waiting for traffic on the new release"})
    return look_at_recovery(connection, check_id, now=now)


def judge_recovery(window: dict, base: dict, rule: dict) -> str:
    """clear | failing | quiet. Pure. Clear means: enough traffic, under the alert threshold, and
    within BASELINE_TOLERANCE_PP of the pre-incident error rate."""
    if window.get("requests") is None or window["requests"] < float(rule["min_requests"]) \
            or window.get("error_rate") is None:
        return "quiet"
    pct = window["error_rate"] * 100.0
    limit = min(float(rule["error_rate_percent"]),
                float(base.get("error_rate") or 0) * 100.0 + BASELINE_TOLERANCE_PP)
    return "clear" if pct <= limit else "failing"


def look_at_recovery(connection, check_id: str, *, now: Optional[datetime] = None) -> dict:
    """One look: has the app been clear for the whole stability window since the fix shipped?"""
    row = connection.execute(sql("SELECT * FROM incident_recovery_checks WHERE id = :i"),
                             {"i": check_id}).mappings().first()
    if not row:
        raise LookupError(check_id)
    row = dict(row)
    if row["status"] in ("recovered", "failed", "timed_out"):
        return row
    now = now or _now()
    since = row["since"]
    stable = timedelta(minutes=float(row["stable_minutes"]))
    base, rule = _json(row["baseline"]), _json(row["rule"])
    # Judged over the stability window, never reaching back before the release that fixed it.
    start = max(since, now - stable)
    w = _window(connection, row["target_id"], start, now)
    verdict = judge_recovery(w, base, rule)
    first_clear = row.get("first_clear_at")
    if verdict == "clear":
        first_clear = first_clear or start
    elif verdict == "failing":
        first_clear = None
    status, summary = row["status"], row["summary"]
    pct = None if w.get("error_rate") is None else round(w["error_rate"] * 100.0, 2)
    seen = int(w.get("requests") or 0)
    if verdict == "clear" and first_clear and now - first_clear >= stable and \
            now - since >= stable:
        status = "recovered"
        summary = (f"Recovered: {pct}% of {seen} requests failed over the last "
                   f"{row['stable_minutes']:g} min (baseline "
                   f"{round(float(base.get('error_rate') or 0) * 100, 2)}%, alert above "
                   f"{rule['error_rate_percent']}%)")
    elif now >= row["deadline"]:
        status = "timed_out"
        summary = (f"Not recovered in time: last window {pct}% of {seen} requests "
                   "failed" if pct is not None else "Not recovered in time: too little traffic "
                   "to judge")
    elif verdict == "failing":
        summary = f"Still failing: {pct}% of {seen} requests"
    elif verdict == "quiet":
        summary = "Waiting for enough traffic on the new release"
    else:
        summary = f"Clear so far ({pct}%); holding for {row['stable_minutes']:g} min"
    checks = int(row.get("checks") or 0) + 1
    connection.execute(sql(
        "UPDATE incident_recovery_checks SET status = :s, summary = :sum, checks = :c, "
        " first_clear_at = :fc, last_window = CAST(:w AS JSONB), updated_at = :now "
        "WHERE id = :i"),
        {"s": status, "sum": summary[:300], "c": checks, "fc": first_clear,
         "w": json.dumps(w, default=str), "now": now, "i": check_id})
    if status in ("recovered", "timed_out"):
        payload = {"check_id": check_id, "state": status, "summary": summary, "window": w,
                   "baseline": base, "rule": rule, "since": _iso(since), "at": _iso(now),
                   "independent": "BOSS's own scrapes of the app's /metrics"}
        _evidence(connection, row["incident_id"], "verification", ref=status, payload=payload,
                  at=now)
        _merge_ctx(connection, row["incident_id"], {"verification": payload})
        if status == "recovered":
            connection.execute(sql(
                "UPDATE incidents SET status = 'resolved', last_seen = :now WHERE id = :i"),
                {"now": now, "i": row["incident_id"]})
    out = dict(row)
    out.update({"status": status, "summary": summary, "checks": checks,
                "first_clear_at": first_clear, "last_window": w})
    return out


# --------------------------------------------------------------------------- #
# 4. The report: timeline, root cause, cost.
# --------------------------------------------------------------------------- #
def response_cost(connection, workflow_run_id: Optional[str]) -> dict[str, Any]:
    """What THIS response spent on models: its own builds' calls only. Billed (metered) and
    subscription-included are kept apart; an included call is never shown as money."""
    if not workflow_run_id:
        return {"billed_usd": 0.0, "included_list_usd": 0.0, "calls": 0, "estimated": False}
    row = connection.execute(sql(
        "SELECT COALESCE(SUM(l.cost_usd_micros) FILTER (WHERE NOT l.included_in_subscription), 0) "
        "         AS billed, "
        "       COALESCE(SUM(l.cost_usd_micros) FILTER (WHERE l.included_in_subscription), 0) "
        "         AS included, "
        "       COUNT(*) AS calls, BOOL_OR(COALESCE(l.price_estimated, false)) AS estimated, "
        "       COALESCE(SUM(l.total_tokens), 0) AS tokens "
        "FROM llm_calls l JOIN code_task_runs c ON c.id = l.code_task_run_id "
        "WHERE c.finalize_ctx->>'workflow_run_id' = :w"), {"w": workflow_run_id}).mappings().one()
    return {"billed_usd": round(int(row["billed"]) / 1e6, 2),
            "included_list_usd": round(int(row["included"]) / 1e6, 2),
            "calls": int(row["calls"]), "tokens": int(row["tokens"]),
            "estimated": bool(row["estimated"])}


def _threshold_percent(p: dict) -> Any:
    """Where the alert evidence kept the threshold. on_alert nests it on `rule`; a joined or
    settling firing spreads the same payload at the top; a split stores `threshold`."""
    rule = p.get("rule") if isinstance(p.get("rule"), dict) else {}
    for candidate in (rule.get("error_rate_percent"), p.get("error_rate_percent"),
                      p.get("threshold")):
        if candidate is not None:
            return candidate
    params = rule.get("params") if isinstance(rule.get("params"), dict) else {}
    if params.get("threshold") is not None and (
            rule.get("kind") == "error_rate" or p.get("kind") == "error_rate"):
        return params["threshold"]
    return None


def _alert_what(p: dict) -> str:
    """One timeline line. on_alert stores a ratio and a request count. A joined or split row
    stores `value` as the percent and often neither ratio nor count — those must not print
    as 0.0% of None."""
    rule = p.get("rule") if isinstance(p.get("rule"), dict) else {}
    kind = p.get("kind") or rule.get("kind")
    if p.get("error_rate") is not None:
        pct = round(float(p["error_rate"]) * 100, 1)
    elif kind in (None, "error_rate") and p.get("value") is not None:
        pct = p["value"]
    else:
        pct = round(float(p.get("error_rate") or 0) * 100, 1)
    traffic = f" of {p['requests']} requests" if p.get("requests") is not None else ""
    return (f"Alert fired: {pct}%{traffic} failing (threshold {_threshold_percent(p)}%)")


def _timeline(connection, inc: dict, workflow_run_id: Optional[str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    since = _occurrence(inc.get("finalize_ctx") or {})
    for e in connection.execute(sql(
            "SELECT kind, ref, payload, created_at FROM incident_evidence "
            "WHERE incident_id = :i AND (CAST(:s AS timestamptz) IS NULL "
            "  OR created_at >= CAST(:s AS timestamptz)) ORDER BY created_at, seq"),
            {"i": inc["id"], "s": since}).mappings().all():
        p = _json(e["payload"]) or {}
        words = {
            "alert": lambda: _alert_what(p),
            "deployment": lambda: (f"Serving release {str(p.get('commit_sha'))[:7]} "
                                   f"(deployed {p.get('deployed_at')})"),
            "logs": lambda: f"Logs read: {p.get('word')}",
            "release_diff": lambda: f"Release diff read: {p.get('word')}",
            "rca_hypothesis": lambda: f"Root cause: {p.get('root_cause')}",
            "verification": lambda: str(p.get("summary")),
            "monitor_resolved": lambda: "Monitor: error rate back under the alert threshold",
        }.get(e["kind"])
        if words:
            items.append({"at": _iso(e["created_at"]), "kind": e["kind"], "what": words()})
    if workflow_run_id:
        for s in connection.execute(sql(
                "SELECT s.step_id, s.status, s.output, s.created_at, s.updated_at, "
                "       d.approved_by, d.approved_at, d.question "
                "FROM workflow_step_runs s LEFT JOIN decisions d ON d.id = s.decision_id "
                "WHERE s.workflow_run_id = :w AND s.status IN ('succeeded', 'failed') "
                "ORDER BY s.updated_at"), {"w": workflow_run_id}).mappings().all():
            out = _json(s["output"]) or {}
            if s["approved_at"] and s["approved_by"]:
                # WHO APPROVED IS THE FACT. A person's gate on the response (the release approval)
                # is the approval of what it deploys; "Step approve succeeded" did not say who
                # (D2: the independent check could not tell the fix deploy was approved).
                items.append({"at": _iso(s["approved_at"]), "kind": "approval",
                              "what": f"{(s['question'] or s['step_id']).split(' — ')[0]} approved by "
                                      f"{_person(connection, s['approved_by'])}"})
            what = f"Step {s['step_id']} {s['status']}"
            if out.get("pull_request_url"):
                what += f" — {out['pull_request_url']}"
            if out.get("ci_status"):
                what += f" — CI {out['ci_status']}"
            items.append({"at": _iso(s["updated_at"]), "kind": "step", "what": what})
    target = ((inc.get("finalize_ctx") or {}).get("app") or {}).get("target_id")
    occurrence = _dt((inc.get("finalize_ctx") or {}).get("occurrence"))
    if target and occurrence:
        for d in connection.execute(sql(
                "SELECT s.commit_sha, s.status, s.finished_at, s.created_at, "
                "       d.approved_by, d.approved_at, d.status AS decision_status "
                "FROM stack_deployments s LEFT JOIN decisions d ON d.id = s.decision_id "
                "WHERE s.target_id = :t AND s.created_at > :o ORDER BY s.created_at"),
                {"t": target, "o": occurrence}).mappings().all():
            if d["approved_at"]:
                items.append({"at": _iso(d["approved_at"]), "kind": "approval",
                              "what": f"Deploy of {str(d['commit_sha'])[:7]} approved by "
                                      f"{_person(connection, d['approved_by'])}"})
            items.append({"at": _iso(d["finished_at"] or d["created_at"]), "kind": "deploy",
                          "what": f"Deployed {str(d['commit_sha'])[:7]}: {d['status']}"})
    return sorted(items, key=lambda i: i["at"] or "")


def _person(connection, principal_id: Optional[str]) -> str:
    """A person's display name for the report (their id when no name is on record)."""
    if not principal_id:
        return "unknown"
    point = connection.begin_nested()
    try:
        name = connection.execute(sql("SELECT display_name FROM identity_principals "
                                      "WHERE id = :i"), {"i": principal_id}).scalar()
        point.commit()
        return str(name or principal_id)
    except Exception:  # noqa: BLE001 — a name is a nicety; the id is the fact
        point.rollback()
        return principal_id


def report(connection, project_id: str, incident_id: str, *,
           workflow_run_id: Optional[str] = None, persist: bool = True) -> dict[str, Any]:
    """The incident's timeline, root cause and cost, as data and as Markdown."""
    inc = _incident(connection, incident_id)
    if not inc or inc["project_id"] != project_id:
        raise LookupError(f"incident {incident_id!r} not found")
    ctx = inc["finalize_ctx"]
    workflow_run_id = workflow_run_id or (ctx.get("response") or {}).get("workflow_run_id")
    hyp = _json(connection.execute(sql(
        "SELECT payload FROM incident_evidence WHERE incident_id = :i AND kind = 'rca_hypothesis' "
        "ORDER BY created_at DESC, seq DESC LIMIT 1"), {"i": incident_id}).scalar()) or {}
    timeline = _timeline(connection, inc, workflow_run_id)
    cost = response_cost(connection, workflow_run_id)
    ver = ctx.get("verification") or {}
    opened = _dt(ctx.get("occurrence")) or inc["first_seen"]
    closed = _dt(ver.get("at")) if ver.get("state") == "recovered" else None
    pr = connection.execute(sql(
        "SELECT pull_request_url FROM code_task_runs WHERE finalize_ctx->>'workflow_run_id' = :w "
        "AND pull_request_url IS NOT NULL ORDER BY created_at DESC LIMIT 1"),
        {"w": workflow_run_id}).scalar() if workflow_run_id else None
    deploy = ctx.get("deploy") or {}
    data = {
        "incident_id": incident_id, "title": inc["title"], "service": inc["service"],
        "status": inc["status"], "opened_at": _iso(opened), "resolved_at": _iso(closed),
        "minutes_to_recover": round((closed - opened).total_seconds() / 60.0, 1)
        if closed else None,
        "faulty_release": deploy.get("commit_sha"), "previous_release": deploy.get(
            "previous_commit_sha"),
        "root_cause": hyp, "pull_request_url": pr, "verification": ver, "timeline": timeline,
        "cost": cost, "workflow_run_id": workflow_run_id,
    }
    from .incident_responders import responders

    data["responders"] = responders(connection, project_id, incident_id, workflow_run_id,
                                    since=_occurrence(ctx))
    data["markdown"] = report_markdown(data)
    if persist:
        connection.execute(sql(
            "INSERT INTO sre_postmortems (id, project_id, incident_id, payload, created_at) "
            "VALUES (:id, :p, :i, CAST(:d AS JSONB), now()) "
            "ON CONFLICT (incident_id) DO UPDATE SET payload = EXCLUDED.payload, "
            "created_at = now()"),
            {"id": make_id("pm"), "p": project_id, "i": incident_id,
             "d": json.dumps({"kind": "incident_report", **data}, default=str)})
    return data


def report_markdown(d: dict[str, Any]) -> str:
    hyp, cost, ver = d.get("root_cause") or {}, d.get("cost") or {}, d.get("verification") or {}
    out = [f"# Incident report: {d['title']}", "",
           f"- **Service:** {d['service']}",
           f"- **Opened:** {d['opened_at']}",
           f"- **Resolved:** {d['resolved_at'] or 'not yet'}"
           + (f" ({d['minutes_to_recover']} min)" if d.get("minutes_to_recover") else ""),
           f"- **Faulty release:** {str(d.get('faulty_release') or '')[:12]}"
           + (f" (previous healthy {str(d['previous_release'])[:12]})"
              if d.get("previous_release") else ""),
           f"- **Fix:** {d.get('pull_request_url') or 'none yet'}", "",
           "## Root cause", "", str(hyp.get("root_cause") or "Not determined."),
           f"_Basis: {hyp.get('basis')}, confidence {hyp.get('confidence')}._", "",
           "## Verification", "",
           str(ver.get("summary") or "Not verified yet.")
           + (" Judged by BOSS's own scrapes of the app, not by the agent that fixed it."
              if ver else ""), "",
           ]
    if d.get("responders"):
        from .incident_responders import who_did_what_markdown

        out += who_did_what_markdown(d["responders"])
    out += ["## Timeline (UTC)", ""]
    for t in d.get("timeline") or []:
        out.append(f"- {t['at']} — {t['what']}")
    out += ["", "## Cost of this response", "",
            f"- Billed model spend: ${cost.get('billed_usd', 0):.2f}"
            + (" (estimated price)" if cost.get("estimated") else ""),
            f"- Included in a subscription (list-price equivalent, not billed): "
            f"${cost.get('included_list_usd', 0):.2f}",
            f"- Model calls: {cost.get('calls', 0)}; tokens: {cost.get('tokens', 0)}"]
    return "\n".join(out)


def _told(connection, incident_id: str) -> list[dict[str, Any]]:
    from .incident_signals import told_reporters

    return told_reporters(connection, incident_id)


def _occurrence(ctx: dict[str, Any]) -> Optional[datetime]:
    """When this occurrence of the incident began (finalize_ctx.occurrence), or None."""
    return _dt((ctx or {}).get("occurrence"))


def response_view(connection, project_id: str, incident_id: str) -> Optional[dict[str, Any]]:
    """Everything the console's incident card shows, in one read."""
    from .incident_responders import responders

    inc = _incident(connection, incident_id)
    if not inc or inc["project_id"] != project_id:
        return None
    ctx = inc["finalize_ctx"]
    # THIS OCCURRENCE ONLY. An incident row is reused when its alert fires again after it
    # resolved; the card read every occurrence's evidence and reports as this one's (Order API,
    # 2026-10-18: a re-fire at 18:43 showed the morning's ten HelpDesk reports and its RCA).
    since = _occurrence(ctx)
    evidence = [{"kind": e["kind"], "ref": e["ref"], "at": _iso(e["created_at"]),
                 "payload": _json(e["payload"])} for e in connection.execute(sql(
                     "SELECT kind, ref, payload, created_at FROM incident_evidence "
                     "WHERE incident_id = :i AND (CAST(:s AS timestamptz) IS NULL "
                     "  OR created_at >= CAST(:s AS timestamptz)) ORDER BY created_at, seq"),
                     {"i": incident_id, "s": since}).mappings().all()]
    reports = [{"reporter_target_id": r["reporter_target_id"], "via": r["via"],
                "at": _iso(r["received_at"]), **(_json(r["payload"]) or {})}
               for r in connection.execute(sql(
                   "SELECT reporter_target_id, via, payload, received_at FROM "
                   "app_signal_receipts WHERE incident_id = :i AND (CAST(:s AS timestamptz) "
                   "  IS NULL OR received_at >= CAST(:s AS timestamptz)) ORDER BY received_at"),
                   {"i": incident_id, "s": since}).mappings().all()]
    run_id = (ctx.get("response") or {}).get("workflow_run_id") or \
        _response_in_flight(connection, project_id, incident_id)
    run = None
    if run_id:
        row = connection.execute(sql(
            "SELECT id, status, created_at, updated_at FROM workflow_runs WHERE id = :r"),
            {"r": run_id}).mappings().first()
        if row:
            from .workflow_step_order import in_declared_order
            steps = [{"step_id": s["step_id"], "status": s["status"]}
                     for s in in_declared_order(connection, run_id, connection.execute(sql(
                         "SELECT step_id, status FROM workflow_step_runs WHERE "
                         "workflow_run_id = :r ORDER BY created_at, iteration"),
                         {"r": run_id}).mappings().all())]
            pr = connection.execute(sql(
                "SELECT pull_request_url FROM code_task_runs WHERE finalize_ctx->>'workflow_run_id'"
                " = :r AND pull_request_url IS NOT NULL ORDER BY created_at DESC LIMIT 1"),
                {"r": run_id}).scalar()
            run = {"id": row["id"], "status": row["status"], "started_at": _iso(row["created_at"]),
                   "steps": steps, "pull_request_url": pr}
    # WHICH RULES RESPOND BY THEMSELVES: per rule, never per app (BOSSAPP-598). The card says
    # which, and links to the app's Alerts tab where each is switched.
    from .app_alerts import auto_responding

    auto = [{**r, "on_behalf_of": _person(connection, r["on_behalf_of"])
             if r["on_behalf_of"] else None}
            for r in auto_responding(connection, (ctx.get("app") or {}).get("target_id"))]
    report_ready = bool(connection.execute(sql(
        "SELECT 1 FROM sre_postmortems WHERE incident_id = :i AND "
        "payload->>'kind' = 'incident_report'"), {"i": incident_id}).first())
    # WHAT DROVE IT (app_alerts_view.drivers): the card's label, title and SEV are the alert
    # firing now (or last), not the one that first opened a reused incident row.
    from .app_alerts_view import drivers, incident_title

    driver = drivers(connection, project_id, [incident_id]).get(incident_id)
    head = {k: (_iso(inc[k]) if k in ("first_seen", "last_seen") else inc[k])
            for k in ("id", "title", "service", "severity", "status", "confidence",
                      "first_seen", "last_seen")}
    head["title"] = incident_title(inc["title"], driver)
    return {
        "incident": head,
        "driver": ({k: driver[k] for k in ("alert_id", "alertname", "kind", "label", "sev",
                                           "state", "value_word")} if driver else None),
        "app": ctx.get("app"), "deploy": ctx.get("deploy"), "alert": ctx.get("alert"),
        "verification": ctx.get("verification"),
        "reported_only": bool(ctx.get("reported_only")),
        "evidence": evidence, "reports": reports, "told": _told(connection, incident_id),
        "response": run,
        # WHO DID WHAT (incident_responders): every part of the response with its agent, the
        # agent's role and job, and what it did — triage, investigation, the build's crew, the
        # person who approved, the deploy, the verification and the report.
        "responders": responders(connection, project_id, incident_id, run_id, since=since),
        "auto_respond_rules": auto,
        "report_ready": report_ready,
        "respond_input": {"incident_id": incident_id,
                          "extends_run_id": (ctx.get("deploy") or {}).get("extends_run_id"),
                          "title": inc["title"],
                          "directive": f"Respond to the incident: {inc['title']}."},
    }


def tell_reporters(connection, settings: Any, project_id: str, incident_id: str,
                   rep: dict[str, Any]) -> list[dict[str, Any]]:
    """Tell every app that reported this incident how it ended (incident_signals). Returns one
    receipt per reporter; [] when no app reported it."""
    from . import incident_signals

    return incident_signals.notify_reporters(connection, settings, project_id, incident_id, rep)
