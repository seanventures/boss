"""The client side of "Improve BOSS" and the daily heartbeat (docs/product/RUN_SCORECARDS.md).

ONE POST A DAY, AT MOST. The worker's existing loop asks `tick()` every few minutes; the tick is a
couple of indexed reads unless the day's send is due. When it is: cut the payload (or take the one
a preview already froze), POST the exact stored bytes with a 5 s timeout, record the outcome, and
schedule tomorrow. Any failure — refused, timed out, 500 — is recorded and waits for tomorrow:
no retry loop, no backoff, nothing that could ever slow down work.

NOTHING LEAVES WHEN IT SHOULD NOT. `BOSS_TELEMETRY=off` or `DO_NOT_TRACK=1` returns before any
payload is cut; heartbeat opted out with the switch off returns before any payload is cut. The
network-level test patches the socket layer and counts zero connections in both cases.

THE PREVIEW IS WHAT IS SENT. A payload is cut once and stored as its exact gzip bytes
(`telemetry_sends`, status `pending`). Govern shows those bytes decoded; the send posts those
bytes. Data that arrives after the cut goes into the next day's batch.

ALLOW-LIST, THEN REDACTION. Everything is built from SQL aggregates and passed through
`improve_allowlist.sanitize` (typed allow-list + the existing secret/PII gate) before it is stored.
"""
from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import logging
import os
import platform
import random
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional

from sqlalchemy import text

from . import improve_allowlist as allow

log = logging.getLogger(__name__)

SIZE_CAP_GZ = 64 * 1024
ROW_THRESHOLD = 200
MAX_UNSENT_BATCHES = 7
LOG_KEEP = 30
TIMEOUT_S = 5.0
SHARE_CAP_GZ = 256 * 1024

_OFF_WORDS = {"off", "0", "false", "no", "disabled", "disable", "none"}
_ON_WORDS = {"1", "true", "yes", "on"}

#: A send function: (url, gzip body) -> (http status or None, short error or None).
Poster = Callable[[str, bytes], tuple[Optional[int], Optional[str]]]


# ------------------------------------------------------------------------------------------
# Switches
# ------------------------------------------------------------------------------------------


def env_disabled(settings: Any) -> Optional[str]:
    """Why the environment turns everything off, or None. Read on every call."""
    tel = str(getattr(settings, "telemetry", "") or os.environ.get("BOSS_TELEMETRY", "")).strip().lower()
    if tel in _OFF_WORDS:
        return "BOSS_TELEMETRY=off"
    dnt = str(getattr(settings, "do_not_track", "") or os.environ.get("DO_NOT_TRACK", "")).strip().lower()
    if dnt in _ON_WORDS:
        return "DO_NOT_TRACK=1"
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get(connection, key: str) -> dict[str, Any]:
    raw = connection.execute(text("SELECT value FROM system_state WHERE key = :k"),
                             {"k": key}).scalar()
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}


def _put(connection, key: str, value: Mapping[str, Any]) -> None:
    connection.execute(text(
        "INSERT INTO system_state (key, value, updated_at) VALUES (:k, CAST(:v AS JSONB), now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"),
        {"k": key, "v": json.dumps(dict(value), default=str)})


SWITCHES = {
    "improve": ("improve_boss", False, "Improve BOSS"),
    "heartbeat": ("improve_heartbeat", True, "Daily heartbeat"),
    "collector": ("improve_collector", False, "Collector mode"),
}


def switches(connection) -> dict[str, dict[str, Any]]:
    out = {}
    for name, (key, default, label) in SWITCHES.items():
        got = _get(connection, key)
        out[name] = {"on": bool(got["on"]) if "on" in got else default,
                     "by": got.get("by"), "at": got.get("at"), "label": label,
                     "default": default, "asked": "on" in got}
    return out


def set_switch(connection, name: str, on: bool, *, actor: str, project_id: str) -> dict:
    """Flip one switch; record who and when in Govern's log; apply what it means at once."""
    if name not in SWITCHES:
        raise ValueError(f"switch must be one of {', '.join(SWITCHES)}")
    key, default, label = SWITCHES[name]
    before = switches(connection)[name]["on"]
    _put(connection, key, {"on": bool(on), "by": actor, "at": _now().isoformat()})
    if name in ("improve", "heartbeat"):
        _discard_pending(connection)
    if name == "improve" and not on:
        # Revoking stops sending immediately: batches not yet delivered never will be.
        connection.execute(text(
            "UPDATE telemetry_batches SET status = 'dropped' WHERE status = 'unsent'"))
    if before != bool(on) or not switches(connection)[name]["asked"]:
        from .scorecards import _log_change

        _log_change(connection, project_id, actor, key, label,
                    "on" if before else "off", "on" if on else "off")
    return switches(connection)


def _discard_pending(connection) -> None:
    connection.execute(text("DELETE FROM telemetry_sends WHERE status = 'pending'"))


# ------------------------------------------------------------------------------------------
# Identity
# ------------------------------------------------------------------------------------------

_B32 = "abcdefghijklmnopqrstuvwxyz234567"


def _new_identity() -> dict[str, str]:
    return {"id": "bi_" + "".join(secrets.choice(_B32) for _ in range(26)),
            "secret": secrets.token_hex(32), "created_at": _now().isoformat()}


def identity(connection) -> dict[str, str]:
    """The random install id and its secret (only ever leaves as a hash, or in a delete)."""
    got = _get(connection, "improve_identity")
    if got.get("id") and got.get("secret"):
        return got
    new = _new_identity()
    connection.execute(text(
        "INSERT INTO system_state (key, value) VALUES ('improve_identity', CAST(:v AS JSONB)) "
        "ON CONFLICT (key) DO NOTHING"), {"v": json.dumps(new)})
    return _get(connection, "improve_identity") or new


def rotate(connection, *, actor: str, project_id: str) -> dict[str, str]:
    before = identity(connection)["id"]
    new = _new_identity()
    _put(connection, "improve_identity", new)
    _discard_pending(connection)
    from .scorecards import _log_change

    _log_change(connection, project_id, actor, "improve_identity", "Install id",
                before[:7] + "…", new["id"][:7] + "…")
    return {"install_id": new["id"]}


def secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


# ------------------------------------------------------------------------------------------
# The heartbeat
# ------------------------------------------------------------------------------------------


def _install_method(settings: Any) -> str:
    chosen = str(getattr(settings, "install_method", "") or "").strip().lower()
    if chosen:
        return chosen if chosen in allow.INSTALL_METHODS else "other"
    if os.environ.get("BOSS_E2E_RUN_ID"):
        return "e2e"
    return "docker" if os.path.exists("/.dockerenv") else "source"


def _os_arch() -> tuple[str, str]:
    system = platform.system().lower()
    os_name = system if system in ("linux", "darwin", "windows") else "other"
    machine = platform.machine().lower()
    arch = ("amd64" if machine in ("x86_64", "amd64") else
            "arm64" if machine in ("arm64", "aarch64") else "other")
    return os_name, arch


def _version() -> str:
    try:
        from .. import __version__

        return str(__version__)
    except Exception:  # noqa: BLE001
        return "0.0.0"


def _edition(settings: Any) -> str:
    raw = str(getattr(settings, "boss_edition", "community") or "community").lower()
    if raw == "community":
        return "community"
    if raw.startswith("enterprise"):
        return "enterprise"
    return "cloud" if "cloud" in raw else "other"


def _install_started(connection) -> Optional[datetime]:
    return connection.execute(text("SELECT MIN(created_at) FROM projects")).scalar()


def heartbeat(connection, settings: Any) -> dict[str, Any]:
    """Exactly the documented eight fields: alive, which BOSS, how old (bucketed), worked today."""
    started = _install_started(connection)
    days = 0 if started is None else max(0, (_now() - started).days)
    worked = bool(connection.execute(text("""
        SELECT EXISTS (SELECT 1 FROM workflow_runs WHERE updated_at > now() - interval '24 hours')
            OR EXISTS (SELECT 1 FROM code_task_runs WHERE updated_at > now() - interval '24 hours')
            OR EXISTS (SELECT 1 FROM meeting_runs WHERE updated_at > now() - interval '24 hours')
            OR EXISTS (SELECT 1 FROM discovery_runs WHERE updated_at > now() - interval '24 hours')
    """)).scalar())
    os_name, arch = _os_arch()
    return {"install_id": identity(connection)["id"], "version": _version(),
            "edition": _edition(settings), "install_method": _install_method(settings),
            "os": os_name, "arch": arch, "age": allow.age_bucket(days), "worked_24h": worked}


# ------------------------------------------------------------------------------------------
# The batch: SQL aggregates since the last batch's end
# ------------------------------------------------------------------------------------------


def _ref(secret: str, unit_id: str) -> str:
    return hmac.new(secret.encode(), unit_id.encode(), hashlib.sha256).hexdigest()[:12]


def _count(connection, sql: str, params: Mapping[str, Any]) -> int:
    try:
        with connection.begin_nested():
            return int(connection.execute(text(sql), dict(params)).scalar() or 0)
    except Exception:  # noqa: BLE001 — a missing table in a partial schema reads as 0
        return 0


def _pairs(connection, sql: str, params: Mapping[str, Any]) -> list[tuple]:
    try:
        with connection.begin_nested():
            return [tuple(r) for r in connection.execute(text(sql), dict(params)).all()]
    except Exception:  # noqa: BLE001
        return []


def _host_ranges() -> tuple[Optional[str], Optional[str]]:
    try:
        mem = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3
    except (ValueError, OSError, AttributeError):
        mem = None
    cpus = os.cpu_count() or 0
    mem_r = None if mem is None else next(
        label for top, label in ((4, "<4"), (8, "4-8"), (16, "8-16"), (32, "16-32"),
                                 (64, "32-64"), (float("inf"), "64+")) if mem < top)
    cpu_r = None if not cpus else next(
        label for top, label in ((2, "1-2"), (4, "3-4"), (8, "5-8"), (16, "9-16"),
                                 (float("inf"), "17+")) if cpus <= top)
    return mem_r, cpu_r


def _days_bucket(start: Optional[datetime], at: Optional[datetime]) -> Optional[str]:
    if not start or not at:
        return None
    return allow.age_bucket(max(0, (at - start).days))


_SCORE_HIST = ("width_bucket(LEAST(GREATEST(score, 0), 0.9999), 0, 1, 10)")


def _scorecards(connection, secret: str, since: Optional[datetime], until: datetime,
                *, force_agg: bool = False, coarse: bool = False) -> tuple[dict, dict]:
    """Per-run rows (≤ ROW_THRESHOLD) or aggregates. Returns (block, aggregated-note)."""
    window = "computed_at <= :u" + (" AND computed_at > :s" if since else "")
    params = {"u": until, "s": since}
    n = _count(connection, f"SELECT COUNT(*) FROM run_scorecards WHERE {window}", params)
    if n == 0:
        return {"rows": [], "agg": []}, {}
    if n <= ROW_THRESHOLD and not force_agg:
        cols = [c for c in allow.SCORECARD_ROW if c not in ("ref", "skills_custom", "mcp_tools",
                                                            "skills")]
        rows = connection.execute(text(
            f"SELECT unit_id, skills, mcp_tools, {', '.join(cols)} FROM run_scorecards "
            f"WHERE {window} ORDER BY computed_at"), params).mappings().all()
        out = []
        builtin = allow.builtin_keys()["skills"]
        for r in rows:
            row = {c: r[c] for c in cols if r[c] is not None}
            for c in ("cost_usd", "confidence", "score"):
                if c in row:
                    row[c] = float(row[c])
            for c in ("tokens_in", "tokens_out"):
                if c in row:
                    row[c] = int(round(int(row[c]), -3))  # to the thousand
            if "wall_time_s" in row:
                row["wall_time_s"] = int(round(int(row["wall_time_s"]), -1))
            skills = list(r["skills"] or [])
            row["skills"] = sorted(s for s in skills if s in builtin)
            row["skills_custom"] = len([s for s in skills if s not in builtin])
            row["mcp_tools"] = len(list(r["mcp_tools"] or []))
            row["ref"] = _ref(secret, str(r["unit_id"]))
            out.append(row)
        return {"rows": out, "agg": []}, {}
    dims = ["unit_kind", "terminal_state"] if coarse else [
        "unit_kind", "harness", "model", "role_key", "workflow_key", "terminal_state",
        "experiment_arm"]
    groups = connection.execute(text(f"""
        SELECT {', '.join(dims)}, score_version, COUNT(*) AS n,
               COUNT(*) FILTER (WHERE pr_merged) AS merged,
               COUNT(*) FILTER (WHERE COALESCE(gates_failed, 0) > 0) AS gates_failed_units,
               COALESCE(SUM(steers), 0) AS steers, COALESCE(SUM(turns), 0) AS turns,
               COALESCE(SUM(cost_usd), 0) AS cost_usd, AVG(score) AS score_mean,
               array_agg({_SCORE_HIST}) FILTER (WHERE score IS NOT NULL) AS hist
        FROM run_scorecards WHERE {window}
        GROUP BY {', '.join(dims)}, score_version"""), params).mappings().all()
    # Custom names fold into `custom` here, so two custom roles become one group.
    merged: dict[tuple, dict[str, Any]] = {}
    for g in groups:
        key_vals = {}
        for d in dims:
            v = g[d]
            if d == "role_key":
                v = allow.catalogue_name("roles", v)
            elif d == "workflow_key":
                v = allow.catalogue_name("workflows", v)
            elif d == "model":
                v = allow.model_family(v)
            key_vals[d] = v
        key = tuple(key_vals.get(d) for d in dims) + (g["score_version"],)
        hist = [0] * 10
        for b in g["hist"] or []:
            if b:
                hist[min(10, int(b)) - 1] += 1
        cur = merged.get(key)
        if cur is None:
            merged[key] = {**{k: v for k, v in key_vals.items() if v is not None},
                           "score_version": g["score_version"], "n": int(g["n"]),
                           "merged": int(g["merged"]),
                           "gates_failed_units": int(g["gates_failed_units"]),
                           "steers": int(g["steers"]), "turns": int(g["turns"]),
                           "cost_usd": float(g["cost_usd"]),
                           "_score_sum": float(g["score_mean"] or 0) * int(g["n"]),
                           "score_hist": hist}
        else:
            for k in ("n", "merged", "gates_failed_units", "steers", "turns"):
                cur[k] += int(g[k])
            cur["cost_usd"] += float(g["cost_usd"])
            cur["_score_sum"] += float(g["score_mean"] or 0) * int(g["n"])
            cur["score_hist"] = [a + b for a, b in zip(cur["score_hist"], hist)]
    agg = []
    for v in merged.values():
        v["score_mean"] = round(v.pop("_score_sum") / v["n"], 3) if v["n"] else None
        v["cost_usd"] = round(v["cost_usd"], 2)
        if v.get("score_version") is None:
            v.pop("score_version")
        agg.append(v)
    note = {"scorecards": {"from_rows": n, "groups": len(agg),
                           "reason": "size_cap" if (force_agg and n <= ROW_THRESHOLD) or coarse
                           else "rows_over_threshold", "level": 2 if coarse else 1}}
    return {"rows": [], "agg": agg}, note


def build_batch(connection, settings: Any, *, day: date, since: Optional[datetime],
                until: datetime, force_agg: bool = False, coarse: bool = False) -> dict[str, Any]:
    """One day's batch, content-free, from aggregates. Sanitized before it is returned."""
    ident = identity(connection)
    p = {"s": since or datetime(1970, 1, 1, tzinfo=timezone.utc), "u": until}
    win = "> :s AND {c} <= :u"

    def w(col: str) -> str:
        return f"{col} {win.format(c=col)}"

    started = _install_started(connection)
    scale = {
        "projects": _count(connection, "SELECT COUNT(*) FROM projects", p),
        "active_projects": _count(connection, """
            SELECT COUNT(DISTINCT project_id) FROM (
              SELECT project_id FROM workflow_runs WHERE updated_at > now() - interval '7 days'
              UNION ALL SELECT project_id FROM code_task_runs
                        WHERE updated_at > now() - interval '7 days') x""", p),
        "people": _count(connection, "SELECT COUNT(DISTINCT member_id) FROM project_members "
                                     "WHERE member_type = 'human' AND status = 'active'", p),
        "agents_builtin": _count(connection, "SELECT COUNT(*) FROM roles WHERE born_by = "
                                             "'spec-pack' OR source_module_id IS NOT NULL", p),
        "agents_custom": _count(connection, "SELECT COUNT(*) FROM roles WHERE born_by IS "
                                            "DISTINCT FROM 'spec-pack' AND source_module_id IS NULL", p),
        "workflows_run": _count(connection, "SELECT COUNT(*) FROM workflow_runs r JOIN "
                                            "workflow_definitions d ON d.id = r.workflow_definition_id "
                                            f"WHERE d.namespace = 'product' AND {w('r.created_at')}", p),
        "builds": _count(connection, f"SELECT COUNT(*) FROM code_task_runs WHERE {w('created_at')} "
                                     "AND NOT (spec ? 'simulated')", p),
        "prs_merged": _count(connection, "SELECT COUNT(*) FROM code_task_runs r JOIN decisions d "
                                         "ON d.id = r.decision_id WHERE r.pull_request_number IS NOT "
                                         "NULL AND d.status IN ('approved','merged','executed') AND "
                                         f"{w('d.updated_at')}", p),
        "deploys": _count(connection, "SELECT COUNT(*) FROM stack_deployments WHERE status IN "
                                      f"('healthy','failed') AND {w('updated_at')}", p),
    }
    ratings: dict[str, dict[str, int]] = {}
    for kind, rating, n in _pairs(connection, f"""
            SELECT subject_kind, rating, COUNT(*) FROM person_feedback
            WHERE rating IS NOT NULL AND subject_kind IN ('build','research','panel','answer','deploy')
              AND {w('updated_at')} GROUP BY 1, 2""", p):
        ratings.setdefault(kind, {})[rating] = int(n)
    chips: dict[str, dict[str, int]] = {}
    for kind, chip, n in _pairs(connection, f"""
            SELECT subject_kind, c, COUNT(*) FROM person_feedback, unnest(chips) AS c
            WHERE subject_kind IN ('build','research','panel','answer','deploy')
              AND {w('updated_at')} GROUP BY 1, 2""", p):
        chips.setdefault(kind, {})[chip] = int(n)
    rejects: dict[str, dict[str, int]] = {}
    for dk, chip, n in _pairs(connection, f"""
            SELECT COALESCE(decision_kind, 'other'), c, COUNT(*) FROM person_feedback, unnest(chips) AS c
            WHERE subject_kind = 'reject' AND {w('created_at')} GROUP BY 1, 2""", p):
        rejects.setdefault(dk, {})[chip] = int(n)
    steers = {chip: int(n) for chip, n in _pairs(connection, f"""
            SELECT c, COUNT(*) FROM person_feedback, unnest(chips) AS c
            WHERE subject_kind = 'steer' AND {w('created_at')} GROUP BY 1""", p)}
    edits: dict[str, dict[str, int]] = {}
    for doc, ratio in _pairs(connection, f"""
            SELECT decision_kind, edit_ratio FROM person_feedback
            WHERE subject_kind = 'edit' AND {w('created_at')} LIMIT 500""", p):
        bucket = allow.edit_bucket(ratio)
        edits.setdefault(doc or "brief", {})
        edits[doc or "brief"][bucket] = edits[doc or "brief"].get(bucket, 0) + 1
    checkin = {a: int(n) for a, n in _pairs(connection, f"""
            SELECT rating, COUNT(*) FROM person_feedback
            WHERE subject_kind = 'checkin' AND {w('created_at')} GROUP BY 1""", p)}
    lines = [str(l) for (l,) in _pairs(connection, f"""
            SELECT one_liner FROM person_feedback WHERE subject_kind = 'checkin' AND share_line
              AND one_liner IS NOT NULL AND {w('created_at')} ORDER BY created_at LIMIT 10""", p)]

    def first(sql: str) -> Optional[datetime]:
        got = _pairs(connection, sql, p)
        return got[0][0] if got and got[0] else None

    funnel = {
        "setup": _days_bucket(started, first("SELECT MIN(created_at) FROM entitlement_sources")),
        "first_build": _days_bucket(started, first(
            "SELECT MIN(created_at) FROM code_task_runs WHERE NOT (spec ? 'simulated')")),
        "first_pr": _days_bucket(started, first(
            "SELECT MIN(updated_at) FROM code_task_runs WHERE pull_request_number IS NOT NULL")),
        "first_merge": _days_bucket(started, first(
            "SELECT MIN(d.updated_at) FROM code_task_runs r JOIN decisions d ON d.id = r.decision_id "
            "WHERE d.status IN ('approved','merged','executed')")),
        "first_deploy": _days_bucket(started, first(
            "SELECT MIN(updated_at) FROM stack_deployments WHERE status = 'healthy'")),
    }
    workflows: dict[str, int] = {}
    for key, n in _pairs(connection, f"""
            SELECT d.workflow_key, COUNT(*) FROM workflow_runs r
            JOIN workflow_definitions d ON d.id = r.workflow_definition_id
            WHERE d.namespace = 'product' AND {w('r.created_at')} GROUP BY 1""", p):
        name = allow.catalogue_name("workflows", key) or "custom"
        workflows[name] = workflows.get(name, 0) + int(n)
    doors = {
        "new_app": _count(connection, f"SELECT COUNT(*) FROM run_scorecards WHERE unit_kind = 'build' "
                                      f"AND task_kind = 'new_app' AND {w('ended_at')}", p),
        "add_feature": _count(connection, f"SELECT COUNT(*) FROM run_scorecards WHERE unit_kind IN "
                                          f"('build','quick_change') AND task_kind IN ('add_feature','fix') "
                                          f"AND {w('ended_at')}", p),
        "template": sum(v for k, v in workflows.items() if k != "custom"),
        "talk": _count(connection, f"SELECT COUNT(*) FROM agent_conversations WHERE {w('created_at')}", p),
        "research": _count(connection, f"SELECT COUNT(*) FROM discovery_runs WHERE {w('created_at')}", p),
        "panel": _count(connection, f"SELECT COUNT(*) FROM meeting_runs WHERE {w('created_at')}", p),
    }
    overrides = {
        "start_anyway": _count(connection, "SELECT COUNT(*) FROM events WHERE event_type = "
                                           f"'workflow.started_unready' AND {w('created_at')}", p),
        "waived_gates": _count(connection, "SELECT COUNT(*) FROM quality_gates WHERE status = "
                                           f"'waived' AND {w('created_at')}", p),
    }
    abandoned = _count(connection, f"""
        SELECT (SELECT COUNT(*) FROM code_task_runs WHERE status = 'cancelled' AND {w('updated_at')})
             + (SELECT COUNT(*) FROM workflow_runs WHERE status = 'cancelled' AND {w('updated_at')})""", p)
    custom = {
        "roles": allow.count_range(scale["agents_custom"]),
        "skills": allow.count_range(_count(connection, "SELECT COUNT(*) FROM skill_definitions", p)),
        "mcp_servers": allow.count_range(_count(connection, "SELECT COUNT(*) FROM mcp_servers WHERE "
                                                "provenance->>'source' IS DISTINCT FROM 'builtin'", p)),
        "workflows": allow.count_range(_count(connection, "SELECT COUNT(*) FROM workflow_definitions "
                                              "WHERE namespace = 'product' AND NOT is_catalogue", p)),
    }
    providers: dict[str, int] = {}
    for kind, n in _pairs(connection, "SELECT entitlement_kind, COUNT(*) FROM entitlement_sources "
                                      "GROUP BY 1", p):
        label = ("subscription" if kind in ("subscription", "licensed_seat") else
                 "self_hosted" if kind == "local_capacity" else "api_key")
        providers[label] = providers.get(label, 0) + int(n)
    targets: dict[str, int] = {}
    for kind, n in _pairs(connection, "SELECT target_kind, COUNT(*) FROM deployment_targets GROUP BY 1", p):
        k = str(kind or "")
        label = ("ssh_host" if "ssh" in k or "host" in k else "docker" if "docker" in k else
                 "kubernetes" if "k8s" in k or "kube" in k else
                 "static" if "static" in k or "pages" in k else
                 "cloud" if k in ("aws", "gcp", "azure", "fly", "render", "vercel", "digitalocean")
                 else "other")
        targets[label] = targets.get(label, 0) + int(n)
    mem_r, cpu_r = _host_ranges()
    errors: dict[str, int] = {}
    for cls, n in _pairs(connection, f"""
            SELECT error_class, COUNT(*) FROM run_scorecards WHERE error_class IS NOT NULL
              AND {w('ended_at')} GROUP BY 1""", p):
        name = cls if cls in allow.ERROR_CLASSES else "other"
        errors[name] = errors.get(name, 0) + int(n)
    health = {
        "errors": errors,
        "retries": _count(connection, f"SELECT COALESCE(SUM(retries), 0) FROM run_scorecards "
                                      f"WHERE {w('ended_at')}", p),
        "stuck": _count(connection, f"SELECT COUNT(*) FROM events WHERE event_type LIKE 'shepherd.%' "
                                    f"AND {w('created_at')}", p),
        "budget_cap_hits": _count(connection, "SELECT COUNT(*) FROM events WHERE event_type IN "
                                              "('budget.refused','budget.reservation_denied',"
                                              "'budget.consume_denied','budget.allocation_denied') "
                                              f"AND {w('created_at')}", p),
        "doctor": {k: int(n) for k, n in _pairs(connection, f"""
            SELECT finding_kind, COUNT(*) FROM system_doctor_actions
            WHERE finding_kind IS NOT NULL AND {w('created_at')} GROUP BY 1""", p)},
    }
    cards, note = _scorecards(connection, ident["secret"], since, until,
                              force_agg=force_agg, coarse=coarse)
    batch = {
        "day": day.isoformat(),
        "since": since.date().isoformat() if since else None,
        "until": until.date().isoformat(),
        "context": {"version": _version(), "edition": _edition(settings)},
        "scale": {k: allow.count_range(v) for k, v in scale.items()},
        "feedback": {"ratings": ratings, "chips": chips, "rejects": rejects, "steers": steers,
                     "edits": edits, "checkin": checkin, "lines": lines},
        "usage": {"funnel": funnel, "workflows": workflows, "doors": doors,
                  "overrides": overrides, "abandoned": abandoned, "custom": custom},
        "setup": {"memory_gb": mem_r, "cpus": cpu_r, "providers": providers,
                  "deploy_targets": targets},
        "health": health,
        "scorecards": cards,
        "aggregated": note,
    }
    clean, _ = allow.sanitize(batch, allow.BATCH)
    return clean


# ------------------------------------------------------------------------------------------
# Cut, freeze, preview, send
# ------------------------------------------------------------------------------------------


def encode(payload: Mapping[str, Any]) -> tuple[bytes, bytes]:
    """(json bytes, gzip bytes). Deterministic: sorted keys, no spaces, gzip mtime 0."""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return raw, gzip.compress(raw, compresslevel=9, mtime=0)


def _schedule(connection) -> dict[str, Any]:
    return _get(connection, "improve_schedule")


def _next_due(after: datetime) -> datetime:
    """Tomorrow (UTC), at a random minute of it: at most one send per UTC day, never in sync
    with every other install."""
    tomorrow = datetime(after.year, after.month, after.day, tzinfo=timezone.utc) + timedelta(days=1)
    return tomorrow + timedelta(seconds=random.randint(0, 20 * 3600))


def _first_due(now: datetime) -> datetime:
    """A new install's first send: later today, jittered (10 min to 6 h)."""
    return now + timedelta(seconds=random.randint(600, 6 * 3600))


def active(connection, settings: Any) -> tuple[bool, Optional[str]]:
    """(anything to send at all, why not)."""
    why = env_disabled(settings)
    if why:
        return False, why
    sw = switches(connection)
    if not sw["heartbeat"]["on"] and not sw["improve"]["on"]:
        return False, "Heartbeat off and Improve BOSS off"
    return True, None


def _pending(connection) -> Optional[Mapping[str, Any]]:
    return connection.execute(text(
        "SELECT * FROM telemetry_sends WHERE status = 'pending' AND kind = 'daily' "
        "ORDER BY cut_at DESC LIMIT 1")).mappings().first()


def _send_day(connection, now: datetime) -> date:
    due = _schedule(connection).get("next_due_at")
    try:
        when = datetime.fromisoformat(due) if due else now
    except ValueError:
        when = now
    return max(when, now).date()


def cut(connection, settings: Any, *, now: Optional[datetime] = None) -> Optional[Mapping[str, Any]]:
    """The next payload, frozen: the pending row if there is one, else build and store it.
    None when nothing is to be sent (and then nothing is built)."""
    ok, _why = active(connection, settings)
    if not ok:
        return None
    pending = _pending(connection)
    if pending:
        return pending
    now = now or _now()
    sw = switches(connection)
    ident = identity(connection)
    day = _send_day(connection, now)
    envelope: dict[str, Any] = {"v": allow.SCHEMA_VERSION, "day": day.isoformat(),
                                "install_id": ident["id"],
                                "secret_hash": secret_hash(ident["secret"])}
    if sw["heartbeat"]["on"]:
        envelope["heartbeat"] = heartbeat(connection, settings)
    batch_days: list[str] = []
    dropped = 0
    if sw["improve"]["on"]:
        last_until = connection.execute(text(
            "SELECT MAX(until_at) FROM telemetry_batches")).scalar()
        if not connection.execute(text("SELECT 1 FROM telemetry_batches WHERE day = :d"),
                                  {"d": day}).scalar():
            body = build_batch(connection, settings, day=day, since=last_until, until=now)
            connection.execute(text(
                "INSERT INTO telemetry_batches (day, body, since_at, until_at) "
                "VALUES (:d, CAST(:b AS JSONB), :s, :u)"),
                {"d": day, "b": json.dumps(body), "s": last_until, "u": now})
        unsent = connection.execute(text(
            "SELECT day FROM telemetry_batches WHERE status = 'unsent' ORDER BY day DESC")).scalars().all()
        keep, drop = unsent[:MAX_UNSENT_BATCHES], unsent[MAX_UNSENT_BATCHES:]
        if drop:
            connection.execute(text(
                "UPDATE telemetry_batches SET status = 'dropped' WHERE day = ANY(:d)"),
                {"d": list(drop)})
        # Batches lost since the last delivery (over the 7-day cap): the collector is told how
        # many, so a gap in an install's history reads as a gap, not as an idle install.
        dropped = int(connection.execute(text(
            "SELECT COUNT(*) FROM telemetry_batches WHERE status = 'dropped' AND created_at > "
            "COALESCE((SELECT MAX(sent_at) FROM telemetry_sends WHERE status = 'sent' "
            "AND kind = 'daily'), 'epoch')")).scalar() or 0)
        bodies = connection.execute(text(
            "SELECT day, body FROM telemetry_batches WHERE day = ANY(:d) ORDER BY day"),
            {"d": list(keep)}).mappings().all()
        envelope["batches"] = [b["body"] if isinstance(b["body"], dict) else json.loads(b["body"])
                               for b in bodies]
        envelope["dropped_batches"] = max(0, dropped)
        batch_days = [b["day"].isoformat() for b in bodies]
    clean, _ = allow.sanitize(envelope, allow.ENVELOPE)
    clean, notes = _fit(connection, settings, clean, now=now)
    raw, gz = encode(clean)
    from .core import make_id

    sid = make_id("tms")
    connection.execute(text("""
        INSERT INTO telemetry_sends (id, day, kind, status, body_gz, bytes_raw, bytes_gz,
                                     batch_days, aggregated, cut_at)
        VALUES (:id, :day, 'daily', 'pending', :gz, :r, :g, CAST(:bd AS JSONB),
                CAST(:ag AS JSONB), :now)"""),
        {"id": sid, "day": day, "gz": gz, "r": len(raw), "g": len(gz),
         "bd": json.dumps(batch_days), "ag": json.dumps(notes), "now": now})
    return _pending(connection)


def _fit(connection, settings: Any, envelope: dict[str, Any], *, now: datetime
         ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Over 64 KB gzip → aggregate (never truncate): rows → groups, then coarse groups, then
    the oldest batches go, each step recorded in the batch and in the send's note."""
    notes: dict[str, Any] = {}
    _raw, gz = encode(envelope)
    if len(gz) <= SIZE_CAP_GZ or not envelope.get("batches"):
        return envelope, {b["day"]: b.get("aggregated") for b in envelope.get("batches", [])
                          if b.get("aggregated")}
    for level, (force, coarse) in enumerate(((True, False), (True, True)), start=1):
        rebuilt = []
        for b in envelope["batches"]:
            row = connection.execute(text(
                "SELECT since_at, until_at FROM telemetry_batches WHERE day = :d"),
                {"d": b["day"]}).mappings().first()
            if not row:
                rebuilt.append(b)
                continue
            nb = build_batch(connection, settings, day=date.fromisoformat(b["day"]),
                             since=row["since_at"], until=row["until_at"],
                             force_agg=force, coarse=coarse)
            note = nb.setdefault("aggregated", {}).setdefault(
                "scorecards", {"from_rows": 0, "groups": 0, "reason": "size_cap", "level": level})
            note["reason"] = "size_cap"
            note["level"] = level
            connection.execute(text(
                "UPDATE telemetry_batches SET body = CAST(:b AS JSONB) WHERE day = :d"),
                {"b": json.dumps(nb), "d": b["day"]})
            rebuilt.append(nb)
            notes[b["day"]] = nb["aggregated"]
        envelope = {**envelope, "batches": rebuilt}
        envelope, _ = allow.sanitize(envelope, allow.ENVELOPE)
        _raw, gz = encode(envelope)
        if len(gz) <= SIZE_CAP_GZ:
            return envelope, notes
    while len(gz) > SIZE_CAP_GZ and len(envelope.get("batches") or []) > 1:
        oldest = envelope["batches"][0]
        connection.execute(text(
            "UPDATE telemetry_batches SET status = 'dropped' WHERE day = :d"), {"d": oldest["day"]})
        envelope["batches"] = envelope["batches"][1:]
        envelope["dropped_batches"] = int(envelope.get("dropped_batches") or 0) + 1
        notes[oldest["day"]] = {"dropped": "size_cap"}
        _raw, gz = encode(envelope)
    return envelope, notes


def decode(body_gz: bytes) -> dict[str, Any]:
    return json.loads(gzip.decompress(bytes(body_gz)))


def preview(connection, settings: Any) -> dict[str, Any]:
    """The exact next payload (cutting it if needed), decoded for reading."""
    row = cut(connection, settings)
    sched = _schedule(connection)
    if row is None:
        _ok, why = active(connection, settings)
        return {"pending": None, "why_not": why, "next_due_at": sched.get("next_due_at")}
    body = bytes(row["body_gz"])
    return {"pending": {"id": row["id"], "day": row["day"].isoformat(),
                        "cut_at": row["cut_at"], "bytes_raw": row["bytes_raw"],
                        "bytes_gz": row["bytes_gz"], "aggregated": row["aggregated"],
                        "batch_days": row["batch_days"],
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "json": decode(body)},
            "why_not": None, "next_due_at": sched.get("next_due_at")}


def _default_post(url: str, body: bytes) -> tuple[Optional[int], Optional[str]]:
    import httpx

    try:
        with httpx.Client(timeout=TIMEOUT_S, follow_redirects=False) as client:
            r = client.post(url, content=body, headers={
                "Content-Type": "application/json", "Content-Encoding": "gzip",
                "User-Agent": f"boss/{_version()}"})
        return r.status_code, None if 200 <= r.status_code < 300 else f"http_{r.status_code}"
    except httpx.TimeoutException:
        return None, "timeout"
    except httpx.HTTPError as error:
        return None, type(error).__name__.lower()[:40]
    except Exception as error:  # noqa: BLE001
        return None, type(error).__name__.lower()[:40]


def _trim_log(connection) -> None:
    connection.execute(text("""
        DELETE FROM telemetry_sends WHERE status <> 'pending' AND id NOT IN (
          SELECT id FROM telemetry_sends WHERE status <> 'pending'
          ORDER BY cut_at DESC LIMIT :n)"""), {"n": LOG_KEEP})


def send_due(engine, settings: Any, *, now: Optional[datetime] = None,
             post: Optional[Poster] = None) -> dict[str, Any]:
    """The daily send, if due. Three short steps; the network call holds no transaction."""
    from ..db import begin

    now = now or _now()
    with begin(engine) as c:
        if not c.execute(text("SELECT pg_try_advisory_xact_lock(hashtext('boss.improve_send'))")).scalar():
            return {"sent": False, "why": "busy"}
        ok, why = active(c, settings)
        if not ok:
            return {"sent": False, "why": why}
        sched = _schedule(c)
        due = sched.get("next_due_at")
        if not due:
            _put(c, "improve_schedule", {**sched, "next_due_at": _first_due(now).isoformat()})
            return {"sent": False, "why": "scheduled"}
        if datetime.fromisoformat(due) > now:
            return {"sent": False, "why": "not_due", "next_due_at": due}
        if sched.get("last_attempt_day") == now.date().isoformat():
            _put(c, "improve_schedule", {**sched, "next_due_at": _next_due(now).isoformat()})
            return {"sent": False, "why": "already_today"}
        row = cut(c, settings, now=now)
        # Claim the day BEFORE the call: whatever happens next, tomorrow is the next try.
        _put(c, "improve_schedule", {**sched, "last_attempt_day": now.date().isoformat(),
                                     "last_attempt_at": now.isoformat(),
                                     "next_due_at": _next_due(now).isoformat()})
        if row is None:
            return {"sent": False, "why": "nothing"}
        sid, body, days = row["id"], bytes(row["body_gz"]), list(row["batch_days"] or [])
    url = str(getattr(settings, "telemetry_url", "") or "").strip()
    status, error = (post or _default_post)(url, body) if url else (None, "no_url")
    ok_sent = status is not None and 200 <= status < 300
    with begin(engine) as c:
        c.execute(text("""
            UPDATE telemetry_sends SET status = :st, http_status = :code, error = :err,
                   sent_at = now() WHERE id = :id"""),
            {"st": "sent" if ok_sent else "failed", "code": status, "err": error, "id": sid})
        if ok_sent and days:
            c.execute(text("UPDATE telemetry_batches SET status = 'sent' WHERE day = ANY(:d)"),
                      {"d": [date.fromisoformat(d) for d in days]})
        _trim_log(c)
    return {"sent": ok_sent, "status": status, "error": error, "id": sid}


def sends(connection, limit: int = LOG_KEEP) -> list[dict[str, Any]]:
    rows = connection.execute(text("""
        SELECT id, day, kind, status, bytes_raw, bytes_gz, batch_days, aggregated, http_status,
               error, cut_at, sent_at FROM telemetry_sends WHERE status <> 'pending'
        ORDER BY cut_at DESC LIMIT :n"""), {"n": limit}).mappings().all()
    return [dict(r) for r in rows]


def sent_body(connection, send_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text("SELECT body_gz FROM telemetry_sends WHERE id = :id"),
                             {"id": send_id}).scalar()
    return decode(row) if row else None


def export(connection, settings: Any) -> Optional[bytes]:
    """The pending payload's JSON, for an air-gapped install to carry by hand (or not)."""
    row = cut(connection, settings)
    if row is None:
        return None
    raw = gzip.decompress(bytes(row["body_gz"]))
    from .core import make_id

    connection.execute(text("""
        INSERT INTO telemetry_sends (id, day, kind, status, bytes_raw, bytes_gz, batch_days, cut_at)
        VALUES (:id, :day, 'export', 'exported', :r, :g, CAST(:bd AS JSONB), now())"""),
        {"id": make_id("tms"), "day": row["day"], "r": row["bytes_raw"], "g": row["bytes_gz"],
         "bd": json.dumps(list(row["batch_days"] or []))})
    _trim_log(connection)
    return raw


# ------------------------------------------------------------------------------------------
# Delete what was sent; share one run
# ------------------------------------------------------------------------------------------


def collector_url(settings: Any, path: str) -> str:
    base = str(getattr(settings, "telemetry_url", "") or "").strip()
    if base.endswith("/ingest"):
        base = base[: -len("/ingest")]
    return f"{base}/{path}"


def delete_request(ident: Mapping[str, str], ts: Optional[int] = None) -> dict[str, Any]:
    ts = int(ts if ts is not None else time.time())
    sig = hmac.new(ident["secret"].encode(), f"delete|{ident['id']}|{ts}".encode(),
                   hashlib.sha256).hexdigest()
    return {"install_id": ident["id"], "ts": ts, "proof": ident["secret"], "sig": sig}


def forget(engine, settings: Any, *, actor: str, project_id: str,
           post: Optional[Poster] = None) -> dict[str, Any]:
    """Ask the collector to delete everything under this install id; on success, rotate."""
    from ..db import begin

    with begin(engine) as c:
        ident = identity(c)
    body = json.dumps(delete_request(ident), sort_keys=True).encode()
    gz = gzip.compress(body, mtime=0)
    status, error = (post or _default_post)(collector_url(settings, "delete"), gz)
    ok = status is not None and 200 <= status < 300
    with begin(engine) as c:
        from .core import make_id

        c.execute(text("""
            INSERT INTO telemetry_sends (id, day, kind, status, bytes_raw, bytes_gz, http_status,
                                         error, cut_at, sent_at)
            VALUES (:id, CURRENT_DATE, 'delete', :st, :r, :g, :code, :err, now(), now())"""),
            {"id": make_id("tms"), "st": "sent" if ok else "failed", "r": len(body),
             "g": len(gz), "code": status, "err": error})
        if ok:
            rotate(c, actor=actor, project_id=project_id)
            c.execute(text("UPDATE telemetry_batches SET status = 'dropped' WHERE status = 'unsent'"))
        _trim_log(c)
    return {"deleted": ok, "status": status, "error": error}


def share_bundle(connection, project_id: str, unit_kind: str, unit_id: str) -> dict[str, Any]:
    """One run's bundle for "Share this run": steps, agent turns, errors and log tails, through
    the redaction gate. Shown to the person to edit; nothing is sent until they press Send."""
    from .redaction import redact

    lines: list[str] = []
    if unit_kind in ("build", "quick_change"):
        run = connection.execute(text(
            "SELECT status, phase, error, state->>'last_verify_output' AS verify, created_at, "
            "updated_at FROM code_task_runs WHERE id = :id AND project_id = :p"),
            {"id": unit_id, "p": project_id}).mappings().first()
        if not run:
            raise LookupError("No such build.")
        lines.append(f"build {run['status']} at {run['phase']} "
                     f"({run['created_at']:%Y-%m-%d %H:%M} → {run['updated_at']:%H:%M})")
        if run["error"]:
            lines.append(f"error: {run['error']}")
        for t in connection.execute(text("""
                SELECT stage, role_key, direction, tool_name, LEFT(content, 600) AS content
                FROM agent_turns WHERE code_task_run_id = :id ORDER BY created_at DESC LIMIT 60"""),
                {"id": unit_id}).mappings().all()[::-1]:
            lines.append(f"[{t['stage'] or '-'}·{t['role_key'] or '-'}·{t['direction']}"
                         f"{'·' + t['tool_name'] if t['tool_name'] else ''}] {t['content'] or ''}")
        if run["verify"]:
            lines.append("verify output (tail):")
            lines.append(str(run["verify"])[-3000:])
    elif unit_kind == "workflow_run":
        run = connection.execute(text(
            "SELECT status, error FROM workflow_runs WHERE id = :id AND project_id = :p"),
            {"id": unit_id, "p": project_id}).mappings().first()
        if not run:
            raise LookupError("No such run.")
        lines.append(f"workflow run {run['status']}")
        if run["error"]:
            lines.append(f"error: {run['error']}")
        for s in connection.execute(text("""
                SELECT step_id, status, attempt, LEFT(COALESCE(last_error, ''), 600) AS err
                FROM workflow_step_runs WHERE workflow_run_id = :id ORDER BY created_at"""),
                {"id": unit_id}).mappings().all():
            lines.append(f"step {s['step_id']}: {s['status']} (attempt {s['attempt']})"
                         + (f" — {s['err']}" if s["err"] else ""))
    else:
        raise ValueError("Share a build or a workflow run.")
    cleaned, masked = redact("\n".join(lines))
    return {"bundle": cleaned[:400_000], "masked": masked,
            "chars": len(cleaned)}


def share_send(engine, settings: Any, *, bundle: str, note: str,
               post: Optional[Poster] = None) -> dict[str, Any]:
    """Send one edited bundle, once, to the collector. Separate from the switch."""
    from ..db import begin

    with begin(engine) as c:
        ident = identity(c)
    payload, _ = allow.sanitize({"v": 1, "install_id": ident["id"], "note": note or "",
                                 "bundle": bundle or "",
                                 "context": {"version": _version(), "edition": _edition(settings)}},
                                allow.SHARE)
    raw, gz = encode(payload)
    if len(gz) > SHARE_CAP_GZ:
        return {"sent": False, "error": "too_large", "bytes_gz": len(gz)}
    status, error = (post or _default_post)(collector_url(settings, "share"), gz)
    ok = status is not None and 200 <= status < 300
    with begin(engine) as c:
        from .core import make_id

        c.execute(text("""
            INSERT INTO telemetry_sends (id, day, kind, status, bytes_raw, bytes_gz, http_status,
                                         error, cut_at, sent_at)
            VALUES (:id, CURRENT_DATE, 'share', :st, :r, :g, :code, :err, now(), now())"""),
            {"id": make_id("tms"), "st": "sent" if ok else "failed", "r": len(raw), "g": len(gz),
             "code": status, "err": error})
        _trim_log(c)
    return {"sent": ok, "status": status, "error": error, "bytes_gz": len(gz)}


# ------------------------------------------------------------------------------------------
# The tick and the Sharing view
# ------------------------------------------------------------------------------------------

MAINTAIN_EVERY_S = 3600


def tick(engine, settings: Any, *, post: Optional[Poster] = None,
         now: Optional[datetime] = None) -> dict[str, Any]:
    """What the worker's loop calls: scorecard upkeep (hourly), collector retention (daily, in
    collector mode), then the daily send if due. Each part bounded; none raises."""
    from ..db import begin

    now = now or _now()
    out: dict[str, Any] = {}
    try:
        with begin(engine) as c:
            if c.execute(text("SELECT pg_try_advisory_xact_lock(hashtext('boss.improve_maint'))")).scalar():
                state = _get(c, "improve_maint")
                last = state.get("at")
                if not last or (now - datetime.fromisoformat(last)).total_seconds() >= MAINTAIN_EVERY_S:
                    from .scorecards import maintain

                    out["scorecards"] = maintain(c)
                    if switches(c)["collector"]["on"]:
                        from .improve_collector import retention

                        out["retention"] = retention(c, now=now)
                    _put(c, "improve_maint", {"at": now.isoformat()})
    except Exception:  # noqa: BLE001
        log.warning("improve tick: maintenance failed", exc_info=True)
    try:
        out["send"] = send_due(engine, settings, now=now, post=post)
    except Exception:  # noqa: BLE001
        log.warning("improve tick: send failed", exc_info=True)
        out["send"] = {"sent": False, "why": "error"}
    return out


def sharing_view(connection, settings: Any) -> dict[str, Any]:
    from .scorecards import DEFAULT_WEIGHTS, WEIGHT_LABELS, weights

    sw = switches(connection)
    why = env_disabled(settings)
    w, version = weights(connection)
    ident = identity(connection)
    sched = _schedule(connection)
    return {
        "switches": sw, "env_disabled": why,
        "install_id": ident["id"], "url": str(getattr(settings, "telemetry_url", "") or ""),
        "next_due_at": sched.get("next_due_at") if not why else None,
        "last_attempt_at": sched.get("last_attempt_at"),
        "sends": sends(connection),
        "unsent_batches": int(connection.execute(text(
            "SELECT COUNT(*) FROM telemetry_batches WHERE status = 'unsent'")).scalar() or 0),
        "score": {"version": version,
                  "weights": [{"key": k, "label": WEIGHT_LABELS[k], "value": w[k],
                               "default": DEFAULT_WEIGHTS[k]} for k in DEFAULT_WEIGHTS]},
        "heartbeat_fields": list(allow.HEARTBEAT),
    }
