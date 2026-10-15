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
import re
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

#: THE ONE LIST of words that turn sharing off (documented in .env.example and the doc;
#: scripts/install/checkup.sh matches them, and a test holds the two together).
TELEMETRY_OFF_WORDS = ("off", "0", "false", "no", "disabled", "disable", "none")
DNT_ON_WORDS = ("1", "true", "yes", "on")
_OFF_WORDS = set(TELEMETRY_OFF_WORDS)
_ON_WORDS = set(DNT_ON_WORDS)
NO_COLLECTOR = "No collector set"

#: A send function: (url, gzip body) -> (http status or None, short error or None).
Poster = Callable[[str, bytes], tuple[Optional[int], Optional[str]]]


# ------------------------------------------------------------------------------------------
# Switches
# ------------------------------------------------------------------------------------------


def _env_word(value: Any) -> str:
    return str(value or "").strip().strip("\"'").strip().lower()


def env_disabled(settings: Any) -> Optional[str]:
    """Why the environment turns everything off, or None. Read on every call. Quotes are
    stripped, as checkup.sh strips them: `"off"` is off."""
    tel = _env_word(getattr(settings, "telemetry", "") or os.environ.get("BOSS_TELEMETRY", ""))
    if tel in _OFF_WORDS:
        return "BOSS_TELEMETRY=off"
    dnt = _env_word(getattr(settings, "do_not_track", "") or os.environ.get("DO_NOT_TRACK", ""))
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


def _when(value: Any) -> Optional[datetime]:
    try:
        when = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def consent_moment(connection) -> Optional[datetime]:
    """The moment from which batches may cover anything: the later of when "Improve BOSS" was last
    turned ON and the last "Delete what was sent" (`improve_floor`). None while it is off.

    THE FLOOR IS WHAT MAKES A DELETE STICK (re-check item 1): a delete clears the local batches, and
    without it the next cut fell back to the consent moment and re-uploaded everything the delete
    had just removed. Nothing from before a delete is ever sent again."""
    got = _get(connection, "improve_boss")
    if not got.get("on") or not got.get("at"):
        return None
    on_at = _when(got["at"])
    floor = _when(_get(connection, "improve_floor").get("at"))
    moments = [t for t in (on_at, floor) if t]
    return max(moments) if moments else None


def set_switch(connection, name: str, on: bool, *, actor: str, project_id: str) -> dict:
    """Flip one switch; record who and when in Govern's log; apply what it means at once."""
    if name not in SWITCHES:
        raise ValueError(f"switch must be one of {', '.join(SWITCHES)}")
    key, default, label = SWITCHES[name]
    current = switches(connection)[name]
    before = current["on"]
    # The moment of the last CHANGE is kept: pressing On again must not move consent.
    at = current["at"] if current["asked"] and before == bool(on) and current["at"] else _now().isoformat()
    _put(connection, key, {"on": bool(on), "by": actor, "at": at})
    if name in ("improve", "heartbeat"):
        _discard_pending(connection)
    if name == "improve" and not on:
        # Revoking stops sending immediately: batches not yet delivered never will be.
        connection.execute(text(
            "UPDATE telemetry_batches SET status = 'dropped' WHERE status = 'unsent'"))
    if name == "improve" and on and not before:
        # Turned on again: what was cut before (and dropped) is gone; today's batch can be cut
        # afresh, starting at this moment (P3, defect 18).
        connection.execute(text("DELETE FROM telemetry_batches WHERE status <> 'sent'"))
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


_PEP440_PRE = re.compile(r"^(\d+\.\d+\.\d+)\.?(a|alpha|b|beta|rc|dev)\.?(\d*)(?:\.post(\d+))?$")
_PRE_LABEL = {"a": "alpha", "alpha": "alpha", "b": "beta", "beta": "beta", "rc": "rc", "dev": "dev"}


def _version() -> str:
    """The release version as it is published (`1.0.0-rc.9`), not Python's spelling of it
    (`1.0.0rc9`): the allow-list only carries the release form, so the raw package version
    would be dropped from every heartbeat and context."""
    try:
        from .. import __version__

        raw = str(__version__)
    except Exception:  # noqa: BLE001
        return "0.0.0"
    pre = _PEP440_PRE.match(raw)
    if pre:
        base, label, n, post = pre.groups()
        # A point release of a candidate (rc10.post7) is published as 1.0.0-rc.10.7.
        return (f"{base}-{_PRE_LABEL[label]}" + (f".{n}" if n else "")
                + (f".{post}" if post else ""))
    return raw


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


#: Aggregate levels, finest first. A level is used only if its groups fit MAX_GROUPS and the
#: gzip fits SIZE_CAP_GZ; otherwise the next, coarser one. Every level covers EVERY unit — nothing
#: is cut: coarsening only merges groups, and the note says which level and how many units.
AGG_LEVELS: tuple[tuple[str, ...], ...] = (
    ("unit_kind", "harness", "model", "role_key", "workflow_key", "terminal_state", "experiment_arm"),
    ("unit_kind", "harness", "model", "role_key", "terminal_state"),
    ("unit_kind", "harness", "model", "terminal_state"),
    ("unit_kind", "terminal_state"),
)
MAX_GROUPS = allow.MAX_AGG_GROUPS


def _fold_map(connection, column: str, where: str, params: Mapping[str, Any]) -> str:
    """{raw value: what may travel} for one dimension's DISTINCT values in the window, as JSON
    for the SQL to look up — so 50,000 rows are folded by the database, not in Python."""
    vals = connection.execute(text(f"SELECT DISTINCT {column} FROM run_scorecards WHERE {where}"),
                              dict(params)).scalars().all()
    fold: dict[str, Optional[str]] = {}
    for v in vals:
        if v is None:
            continue
        if column == "role_key":
            fold[v] = allow.catalogue_name("roles", v)
        elif column == "workflow_key":
            fold[v] = allow.catalogue_name("workflows", v)
        elif column == "model":
            fold[v] = allow.public_model(v)
        elif column == "harness":
            fold[v] = v if v in allow.HARNESSES else "other"
        else:
            fold[v] = v
    return json.dumps(fold)


def _scorecard_window(since: Optional[datetime], consent_at: Optional[datetime]) -> str:
    """Rows recomputed in this batch's window, of units that ENDED after consent: a backfill or a
    late outcome after the switch went on must not carry a run from before it."""
    where = "computed_at <= :u"
    if since:
        where += " AND computed_at > :s"
    if consent_at:
        where += " AND ended_at > :c"
    return where


def _scorecards(connection, secret: str, since: Optional[datetime], until: datetime,
                *, consent_at: Optional[datetime] = None, force_agg: bool = False,
                level: int = 1) -> tuple[dict, dict]:
    """Per-run rows (≤ ROW_THRESHOLD) or aggregates at `level` or coarser. (block, note)."""
    window = _scorecard_window(since, consent_at)
    params = {"u": until, "s": since, "c": consent_at}
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
    folds = {d: _fold_map(connection, d, window, params)
             for d in ("role_key", "workflow_key", "model", "harness")}
    expr = {d: f"(CAST(:f_{d} AS JSONB) ->> {d})" if d in folds else d
            for d in AGG_LEVELS[0]}
    hist = ", ".join(
        f"COUNT(*) FILTER (WHERE width_bucket(LEAST(GREATEST(score, 0), 0.9999), 0, 1, 10) = {k}) AS h{k}"
        for k in range(1, 11))
    groups: list = []
    used = 0
    for i in range(max(1, level) - 1, len(AGG_LEVELS)):
        dims = AGG_LEVELS[i]
        sel = ", ".join(f"{expr[d]} AS {d}" for d in dims)
        count = _count(connection, f"""SELECT COUNT(*) FROM (SELECT 1 FROM run_scorecards
            WHERE {window} GROUP BY {', '.join(expr[d] for d in dims)}, score_version) g""",
                       {**params, **{f"f_{d}": v for d, v in folds.items()}})
        if count > MAX_GROUPS and i < len(AGG_LEVELS) - 1:
            continue
        used = i + 1
        groups = connection.execute(text(f"""
            SELECT {sel}, score_version, COUNT(*) AS n,
                   COUNT(*) FILTER (WHERE pr_merged) AS merged,
                   COUNT(*) FILTER (WHERE COALESCE(gates_failed, 0) > 0) AS gates_failed_units,
                   COALESCE(SUM(steers), 0) AS steers, COALESCE(SUM(turns), 0) AS turns,
                   COALESCE(SUM(cost_usd), 0) AS cost_usd, AVG(score) AS score_mean, {hist}
            FROM run_scorecards WHERE {window}
            GROUP BY {', '.join(expr[d] for d in dims)}, score_version"""),
            {**params, **{f"f_{d}": v for d, v in folds.items()}}).mappings().all()
        break
    agg = []
    for g in groups:
        row = {d: g[d] for d in AGG_LEVELS[used - 1] if g[d] is not None}
        row.update({"n": int(g["n"]), "merged": int(g["merged"]),
                    "gates_failed_units": int(g["gates_failed_units"]),
                    "steers": int(g["steers"]), "turns": int(g["turns"]),
                    "cost_usd": round(float(g["cost_usd"]), 2),
                    "score_mean": None if g["score_mean"] is None else round(float(g["score_mean"]), 3),
                    "score_hist": [int(g[f"h{k}"]) for k in range(1, 11)]})
        if g["score_version"] is not None:
            row["score_version"] = g["score_version"]
        if row["score_mean"] is None:
            row.pop("score_mean")
        agg.append(row)
    covered = sum(r["n"] for r in agg)
    note = {"scorecards": {"from_rows": n, "covered": covered, "groups": len(agg), "level": used,
                           "reason": "size_cap" if force_agg and (n <= ROW_THRESHOLD or level > 1)
                           else "rows_over_threshold"}}
    return {"rows": [], "agg": agg}, note


def _authored_skill_count(connection: Any) -> int:
    """How many skills operators authored on this install: the authored store's own count."""
    from .skill_definitions import count_authored

    try:
        return count_authored(connection)
    except Exception:  # noqa: BLE001 — a missing table is zero, as _count treats it
        return 0


def build_batch(connection, settings: Any, *, day: date, since: Optional[datetime],
                until: datetime, consent_at: Optional[datetime] = None, force_agg: bool = False,
                level: int = 1) -> dict[str, Any]:
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
        "skills": allow.count_range(_authored_skill_count(connection)),
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
    cards, note = _scorecards(connection, ident["secret"], since, until, consent_at=consent_at,
                              force_agg=force_agg, level=level)
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


def endpoint(connection, settings: Any) -> tuple[str, str]:
    """(collector ingest URL, where it came from: env | govern | none). No default (operator
    decision 2026-10-05): with none set, nothing is sent, the heartbeat included."""
    env = str(getattr(settings, "telemetry_url", "") or "").strip()
    if env:
        return env, "env"
    got = str(_get(connection, "improve_endpoint").get("url") or "").strip()
    return (got, "govern") if got else ("", "none")


def blocked(connection, settings: Any) -> Optional[str]:
    """Why NOTHING may leave this install right now — env off, or no collector — or None."""
    return env_disabled(settings) or (None if endpoint(connection, settings)[0] else NO_COLLECTOR)


def check_url(url: str) -> str:
    """A collector URL an operator may set: https (or http on this machine), no credentials,
    the ingest path. Raises ValueError with the reason."""
    from urllib.parse import urlsplit

    url = (url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    local = parts.hostname in ("127.0.0.1", "localhost", "::1")
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise ValueError("Must be https (http only on this machine)")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Must not carry credentials, a query or a fragment")
    if not parts.hostname or len(url) > 300:
        raise ValueError("Must be a full collector URL")
    if not parts.path.rstrip("/").endswith("/telemetry/ingest"):
        raise ValueError("Must end in /v1/telemetry/ingest")
    return url.rstrip("/")


def set_endpoint(connection, url: str, *, actor: str, project_id: str) -> None:
    clean = check_url(url)
    before = str(_get(connection, "improve_endpoint").get("url") or "")
    _put(connection, "improve_endpoint", {"url": clean, "by": actor, "at": _now().isoformat()})
    _discard_pending(connection)
    if before != clean:
        from urllib.parse import urlsplit
        from .scorecards import _log_change

        host = lambda u: urlsplit(u).hostname or "none"  # noqa: E731
        _log_change(connection, project_id, actor, "improve_endpoint", "Collector",
                    host(before) if before else "none", host(clean) if clean else "none")


MASK_WORDS_MAX = 50


def mask_words(connection) -> list[str]:
    """Words a shared run always masks, set in Govern › Sharing (a client, a codename)."""
    return [str(w) for w in (_get(connection, "improve_mask").get("words") or []) if str(w).strip()]


def set_mask_words(connection, words: Any, *, actor: str, project_id: str) -> list[str]:
    if isinstance(words, str):
        words = re.split(r"[,\n]", words)
    clean = []
    for w in words or []:
        w = str(w).strip()
        if not w:
            continue
        if len(w) < 3 or len(w) > 80:
            raise ValueError(f"'{w[:20]}': a word to mask is 3 to 80 characters.")
        if w.lower() not in (c.lower() for c in clean):
            clean.append(w)
    if len(clean) > MASK_WORDS_MAX:
        raise ValueError(f"At most {MASK_WORDS_MAX} words.")
    before = mask_words(connection)
    _put(connection, "improve_mask", {"words": clean, "by": actor, "at": _now().isoformat()})
    if before != clean:
        from .scorecards import _log_change

        # The words themselves are private: the log says how many, never which.
        _log_change(connection, project_id, actor, "improve_mask", "Always mask",
                    f"{len(before)} words", f"{len(clean)} words")
    return clean


def active(connection, settings: Any, *, need_collector: bool = True) -> tuple[bool, Optional[str]]:
    """(anything to send at all, why not). Preview and Export do not need a collector — an
    air-gapped install exports by hand — but sending does."""
    why = blocked(connection, settings) if need_collector else env_disabled(settings)
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
    None when nothing is to be sent (and then nothing is built). Built with or without a
    collector set — it is shown and exportable either way; only transmit() sends."""
    ok, _why = active(connection, settings, need_collector=False)
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
        # NOTHING FROM BEFORE CONSENT (P3): a batch starts at the later of the last batch's end
        # and the moment the switch was last turned ON — so the first batch starts at consent,
        # and an off period is never covered after the switch is turned on again.
        consent_at = consent_moment(connection)
        last_until = connection.execute(text(
            "SELECT MAX(until_at) FROM telemetry_batches")).scalar()
        since = max([t for t in (last_until, consent_at) if t] or [None],
                    key=lambda t: t or datetime.min.replace(tzinfo=timezone.utc))
        if not connection.execute(text("SELECT 1 FROM telemetry_batches WHERE day = :d"),
                                  {"d": day}).scalar():
            body = build_batch(connection, settings, day=day, since=since, until=now,
                               consent_at=consent_at)
            connection.execute(text(
                "INSERT INTO telemetry_batches (day, body, since_at, until_at, consent_at) "
                "VALUES (:d, CAST(:b AS JSONB), :s, :u, :c)"),
                {"d": day, "b": json.dumps(body), "s": since, "u": now, "c": consent_at})
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
    """Over 64 KB gzip → aggregate, never truncate: per-run rows become groups, and the groups
    coarsen level by level (fewer dimensions) until it fits. Every level still counts every unit;
    the batch's `aggregated` note says the level, the groups and units covered of units in total.
    Only if the coarsest level of every batch still does not fit do the OLDEST batches go, and
    the envelope says how many (`dropped_batches`)."""
    notes: dict[str, Any] = {}
    _raw, gz = encode(envelope)
    if len(gz) <= SIZE_CAP_GZ or not envelope.get("batches"):
        return envelope, {b["day"]: b.get("aggregated") for b in envelope.get("batches", [])
                          if b.get("aggregated")}
    for level in range(1, len(AGG_LEVELS) + 1):
        rebuilt = []
        for b in envelope["batches"]:
            row = connection.execute(text(
                "SELECT since_at, until_at, consent_at FROM telemetry_batches WHERE day = :d"),
                {"d": b["day"]}).mappings().first()
            if not row:
                rebuilt.append(b)
                continue
            nb = build_batch(connection, settings, day=date.fromisoformat(b["day"]),
                             since=row["since_at"], until=row["until_at"],
                             consent_at=row["consent_at"], force_agg=True, level=level)
            note = (nb.get("aggregated") or {}).get("scorecards")
            if note:
                note["reason"] = "size_cap"
            connection.execute(text(
                "UPDATE telemetry_batches SET body = CAST(:b AS JSONB) WHERE day = :d"),
                {"b": json.dumps(nb), "d": b["day"]})
            rebuilt.append(nb)
            if nb.get("aggregated"):
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
        _ok, why = active(connection, settings, need_collector=False)
        return {"pending": None, "why_not": why, "blocked": blocked(connection, settings),
                "next_due_at": sched.get("next_due_at")}
    body = bytes(row["body_gz"])
    return {"pending": {"id": row["id"], "day": row["day"].isoformat(),
                        "cut_at": row["cut_at"], "bytes_raw": row["bytes_raw"],
                        "bytes_gz": row["bytes_gz"], "aggregated": row["aggregated"],
                        "batch_days": row["batch_days"],
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "json": decode(body)},
            "why_not": None, "blocked": blocked(connection, settings),
            "next_due_at": sched.get("next_due_at") if not blocked(connection, settings) else None}


def _default_post(url: str, body: bytes) -> tuple:
    """(status, error, the collector's JSON answer or None). Reads at most 64 KB of the answer."""
    import httpx

    try:
        with httpx.Client(timeout=TIMEOUT_S, follow_redirects=False) as client:
            r = client.post(url, content=body, headers={
                "Content-Type": "application/json", "Content-Encoding": "gzip",
                "User-Agent": f"boss/{_version()}"})
        answer = None
        try:
            if len(r.content) <= 65536:
                answer = r.json()
        except ValueError:
            answer = None
        return (r.status_code, None if 200 <= r.status_code < 300 else f"http_{r.status_code}",
                answer)
    except httpx.TimeoutException:
        return None, "timeout"
    except httpx.HTTPError as error:
        return None, type(error).__name__.lower()[:40]
    except Exception as error:  # noqa: BLE001
        return None, type(error).__name__.lower()[:40]


def transmit(settings: Any, url: str, body: bytes, post: Optional[Poster] = None
             ) -> tuple[Optional[int], Optional[str]]:
    """THE ONE DOOR every outbound telemetry byte goes through: the daily send, a shared run, a
    delete, a rotation's delete. It refuses — before any poster, injected or real, is called —
    when the environment turns sharing off or no collector is set (P2)."""
    status, error, _answer = transmit_answer(settings, url, body, post)
    return status, error


def transmit_answer(settings: Any, url: str, body: bytes, post: Optional[Poster] = None
                    ) -> tuple[Optional[int], Optional[str], Optional[dict]]:
    """transmit(), keeping what the collector answered (a delete's counts)."""
    why = env_disabled(settings)
    if why:
        return None, "disabled_by_env", None
    if not url:
        return None, "no_collector", None
    got = tuple((post or _default_post)(url, body))
    answer = got[2] if len(got) > 2 and isinstance(got[2], dict) else None
    return got[0], got[1], answer


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
        url = endpoint(c, settings)[0]
    status, error = transmit(settings, url, body, post)
    ok_sent = status is not None and 200 <= status < 300
    with begin(engine) as c:
        c.execute(text("""
            UPDATE telemetry_sends SET status = :st, http_status = :code, error = :err,
                   sent_at = now() WHERE id = :id"""),
            {"st": "sent" if ok_sent else "failed", "code": status, "err": error, "id": sid})
        if ok_sent and days:
            c.execute(text("UPDATE telemetry_batches SET status = 'sent' WHERE day = ANY(:d)"),
                      {"d": [date.fromisoformat(d) for d in days]})
        if ok_sent:
            # Something now sits at the collector under this id: a rotation must delete it.
            ident = identity(c)
            if not ident.get("sent"):
                _put(c, "improve_identity", {**ident, "sent": True})
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


def collector_url(base: str, path: str) -> str:
    base = (base or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/ingest"):
        base = base[: -len("/ingest")]
    return f"{base}/{path}"


def delete_request(ident: Mapping[str, str], ts: Optional[int] = None) -> dict[str, Any]:
    ts = int(ts if ts is not None else time.time())
    sig = hmac.new(ident["secret"].encode(), f"delete|{ident['id']}|{ts}".encode(),
                   hashlib.sha256).hexdigest()
    return {"install_id": ident["id"], "ts": ts, "proof": ident["secret"], "sig": sig}


def _retired(connection) -> list[dict[str, str]]:
    """Old ids whose data may still sit at the collector, kept WITH their secret until a signed
    delete for each goes through (P4). Never sent anywhere but in that delete."""
    return list(_get(connection, "improve_retired").get("ids") or [])


def _log_send(connection, kind: str, ok: bool, raw: int, gz: int, status, error) -> None:
    from .core import make_id

    connection.execute(text("""
        INSERT INTO telemetry_sends (id, day, kind, status, bytes_raw, bytes_gz, http_status,
                                     error, cut_at, sent_at)
        VALUES (:id, CURRENT_DATE, :k, :st, :r, :g, :code, :err, now(), now())"""),
        {"id": make_id("tms"), "k": kind, "st": "sent" if ok else "failed", "r": raw, "g": gz,
         "code": status, "err": error})


_LAST_DELETE_COUNTS: dict[str, int] = {}


def _delete_ids(engine, settings: Any, idents: list[Mapping[str, str]],
                post: Optional[Poster]) -> tuple[list[str], list[str], Optional[str]]:
    """Signed deletes, one per id, through the one door. (deleted ids, failed ids, last error).
    What the collector says it removed, per id, is kept in `_LAST_DELETE_COUNTS` — the screen
    reports those numbers, never "deleted" for an id the collector held nothing for."""
    from ..db import begin

    with begin(engine) as c:
        url = collector_url(endpoint(c, settings)[0], "delete")
    done, failed, last_error = [], [], None
    _LAST_DELETE_COUNTS.clear()
    for ident in idents:
        body = json.dumps(delete_request(ident), sort_keys=True).encode()
        gz = gzip.compress(body, mtime=0)
        status, error, answer = transmit_answer(settings, url, gz, post)
        ok = status is not None and 200 <= status < 300
        if ok:
            try:
                _LAST_DELETE_COUNTS[ident["id"]] = int((answer or {}).get("deleted") or 0)
            except (TypeError, ValueError):
                _LAST_DELETE_COUNTS[ident["id"]] = 0
        with begin(engine) as c:
            _log_send(c, "delete", ok, len(body), len(gz), status, error)
        (done if ok else failed).append(ident["id"])
        if not ok:
            last_error = error
    return done, failed, last_error


def _settle_retired(connection, deleted: list[str]) -> list[dict[str, str]]:
    left = [r for r in _retired(connection) if r["id"] not in deleted]
    _put(connection, "improve_retired", {"ids": left})
    return left


def rotate(engine, settings: Any, *, actor: str, project_id: str,
           post: Optional[Poster] = None) -> dict[str, Any]:
    """A new random id. The OLD one's data is deleted at the collector first (signed); if that
    cannot happen now, the old id and its secret are kept until it can (P4) — and the answer
    says which, so the screen can tell the truth."""
    from ..db import begin

    with begin(engine) as c:
        old = identity(c)
        new = _new_identity()
        reachable = blocked(c, settings) is None
        # The old id is deleted at the collector whenever one can be reached — not only when this
        # install remembers sending (a shared run, an export carried by hand, a flag lost to a
        # restore). It is KEPT, with its secret, when something was sent and no delete can go.
        if old.get("sent") or reachable:
            ids = _retired(c) + [{"id": old["id"], "secret": old["secret"], "sent": bool(old.get("sent"))}]
            _put(c, "improve_retired", {"ids": ids})
        _put(c, "improve_identity", new)
        _discard_pending(c)
        from .scorecards import _log_change

        _log_change(c, project_id, actor, "improve_identity", "Install id",
                    old["id"][:7] + "…", new["id"][:7] + "…")
        pending = _retired(c)
    deleted, failed, error = (_delete_ids(engine, settings, pending, post) if pending
                              else ([], [], None))
    counts = dict(_LAST_DELETE_COUNTS)
    with begin(engine) as c:
        # A never-sent id whose delete could not go is not worth keeping a secret for.
        stale = [r["id"] for r in _retired(c) if r["id"] == old["id"] and not r.get("sent")
                 and old["id"] in failed]
        left = _settle_retired(c, deleted + stale)
    removed = counts.get(old["id"])
    return {"install_id": new["id"],
            "old": ("deleted" if removed else "nothing_held") if old["id"] in deleted
            else ("kept" if old.get("sent") else "nothing_sent"),
            "removed": removed or 0, "waiting": len(left), "error": error}


def forget(engine, settings: Any, *, actor: str, project_id: str,
           post: Optional[Poster] = None) -> dict[str, Any]:
    """Delete what was sent: a signed delete for EVERY id this install has used (the current one
    and any earlier one not yet deleted); then a new id, and nothing cut locally kept. An id whose
    delete fails is kept, with its secret, to try again."""
    from ..db import begin

    with begin(engine) as c:
        cur = identity(c)
        targets = _retired(c) + [{"id": cur["id"], "secret": cur["secret"]}]
    deleted, failed, error = _delete_ids(engine, settings, targets, post)
    with begin(engine) as c:
        if cur["id"] in deleted:
            # The current id's data is gone: it is never used again.
            new = _new_identity()
            _put(c, "improve_identity", new)
            from .scorecards import _log_change

            _log_change(c, project_id, actor, "improve_identity", "Install id",
                        cur["id"][:7] + "…", new["id"][:7] + "…")
        left = _settle_retired(c, deleted)
        if not failed:
            # Nothing of ours is at the collector any more: start clean, and the day's send may
            # happen again — but only from THIS moment: the floor keeps everything before the
            # delete from ever being cut again (re-check item 1).
            _put(c, "improve_floor", {"at": _now().isoformat(), "by": actor})
            c.execute(text("DELETE FROM telemetry_batches"))
            _discard_pending(c)
            sched = _schedule(c)
            sched.pop("last_attempt_day", None)
            _put(c, "improve_schedule", sched)
        _trim_log(c)
    removed = sum(_LAST_DELETE_COUNTS.values())
    return {"deleted": not failed, "removed": removed, "ids_deleted": len(deleted), "ids_failed": len(failed),
            "waiting": len(left), "error": error}


def share_bundle(connection, project_id: str, unit_kind: str, unit_id: str) -> dict[str, Any]:
    """One run's bundle for "Share this run": steps, agent turns, errors and log tails, through
    the share scrub (services/share_scrub.py). Shown to the person to edit; nothing is sent until
    they press Send."""

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
    from .share_scrub import install_names, scrub

    cleaned, masked = scrub("\n".join(lines), install_names(connection, project_id))
    return {"bundle": cleaned[:400_000], "masked": masked,
            "chars": len(cleaned)}


def share_send(engine, settings: Any, *, bundle: str, note: str,
               post: Optional[Poster] = None) -> dict[str, Any]:
    """Send one edited bundle, once, to the collector. Separate from the switch."""
    from ..db import begin

    with begin(engine) as c:
        ident = identity(c)
        url = collector_url(endpoint(c, settings)[0], "share")
    payload, _ = allow.sanitize({"v": 1, "install_id": ident["id"],
                                 "secret_hash": secret_hash(ident["secret"]), "note": note or "",
                                 "bundle": bundle or "",
                                 "context": {"version": _version(), "edition": _edition(settings)}},
                                allow.SHARE)
    raw, gz = encode(payload)
    if len(gz) > SHARE_CAP_GZ:
        return {"sent": False, "error": "too_large", "bytes_gz": len(gz)}
    status, error = transmit(settings, url, gz, post)
    ok = status is not None and 200 <= status < 300
    if error in ("disabled_by_env", "no_collector"):
        return {"sent": False, "status": None, "error": error, "bytes_gz": len(gz)}
    with begin(engine) as c:
        _log_send(c, "share", ok, len(raw), len(gz), status, error)
        if ok:
            # A shared run sits at the collector under this id: rotate and Delete must remove it.
            cur = identity(c)
            if cur["id"] == ident["id"] and not cur.get("sent"):
                _put(c, "improve_identity", {**cur, "sent": True})
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
    try:
        # Old ids still waiting for their delete (P4): retried with the day's send, never more.
        if (out.get("send") or {}).get("id"):
            with begin(engine) as c:
                pending = _retired(c)
            if pending:
                deleted, _f, _e = _delete_ids(engine, settings, pending, post)
                with begin(engine) as c:
                    _settle_retired(c, deleted)
    except Exception:  # noqa: BLE001
        log.warning("improve tick: retired-id delete failed", exc_info=True)
    return out


def person_name(connection, member_id: Optional[str]) -> Optional[str]:
    """A person's display name for the screen — never their raw member id (defect 13)."""
    if not member_id:
        return None
    got = connection.execute(text(
        "SELECT display_name FROM project_members WHERE member_id = :m AND display_name <> '' "
        "UNION ALL SELECT display_name FROM org_members WHERE member_id = :m AND display_name <> '' "
        "LIMIT 1"), {"m": member_id}).scalar()
    return str(got) if got else str(member_id).replace("_", " ").title()


def sharing_view(connection, settings: Any) -> dict[str, Any]:
    from .scorecards import DEFAULT_WEIGHTS, WEIGHT_LABELS, weights

    sw = switches(connection)
    for one in sw.values():
        one["by_name"] = person_name(connection, one.get("by"))
    why = env_disabled(settings)
    url, source = endpoint(connection, settings)
    w, version = weights(connection)
    ident = identity(connection)
    sched = _schedule(connection)
    stop = blocked(connection, settings)
    return {
        "switches": sw, "env_disabled": why, "blocked": stop,
        "install_id": ident["id"], "url": url, "url_source": source,
        "retired_waiting": len(_retired(connection)),
        "mask_words": mask_words(connection),
        "next_due_at": sched.get("next_due_at") if not stop else None,
        "last_attempt_at": sched.get("last_attempt_at"),
        "sends": sends(connection),
        "unsent_batches": int(connection.execute(text(
            "SELECT COUNT(*) FROM telemetry_batches WHERE status = 'unsent'")).scalar() or 0),
        "score": {"version": version,
                  "weights": [{"key": k, "label": WEIGHT_LABELS[k], "value": w[k],
                               "default": DEFAULT_WEIGHTS[k]} for k in DEFAULT_WEIGHTS]},
        "heartbeat_fields": list(allow.HEARTBEAT),
    }
