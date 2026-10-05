"""Observability's one view of whether it is alive (infra-observability spec §4.9-§4.13, §6.2).

THE CONTRACT, frozen in Phase 0. apps/web/lib/obs-view.ts parses exactly these shapes. Change
it with the lead, never inside a stream.

Four tabs, four builders, one window control (`win`: 24h | 7d | 30d) that drives every number
and chart on a tab:

  Apps     `pulse()` + `app_detail()`   deployment_health_samples, bucketed in ONE grouped query
  Models   `models()`                   llm_calls, one grouped aggregate per (window, group)
  System   `system()`                   the stability work's real data: admission.sample() /
                                        snapshot() (GET /v1/system/health), system_doctor
                                        findings + recent actions, the queue, stalls, the build
  Sources  `sources()`                  the monitoring stack (status cached 60 s — never an SSH
                                        probe per poll), scraped apps, telemetry sources, gaps

NEVER A GUESS: a bucket with no probes is a gap (n == 0), never green. Spend is the sum of
`cost_usd_micros` where not `included_in_subscription`; `estimated` is true iff any row in the
window has `price_estimated`. There is no provider column on llm_calls, so none is drawn.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional, TypedDict
from urllib.parse import urlparse

from sqlalchemy import text

Window = Literal["24h", "7d", "30d"]

#: window → (seconds, default cells). `cells` may be overridden by the narrow layouts: 48 (30 min
#: at 24h) and 24 (1 h at 24h); the server re-buckets, the web never does.
WINDOWS: dict[str, tuple[int, int]] = {
    "24h": (24 * 3600, 96),
    "7d": (7 * 24 * 3600, 84),
    "30d": (30 * 24 * 3600, 90),
}
ALLOWED_CELLS = (24, 32, 48, 84, 90, 96)
#: Models sparklines and the stacked stream: 24 points over the window.
SPARK_POINTS = 24

Group = Literal["model", "stage", "role", "engine"]
Metric = Literal["tokens", "calls", "spend", "latency"]


def window_of(win: Optional[str]) -> str:
    return win if win in WINDOWS else "24h"


def etag_of(material: Any) -> str:
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode())
    return f'"obs-{digest.hexdigest()[:24]}"'


# ── Apps: the Pulse (§4.9) ──────────────────────────────────────────────────────────────────

#: One Pulse cell: [ok, n, p95_ms|None]. n == 0 is a gap (drawn outlined, never green).
PulseCell = list


class Pin(TypedDict, total=False):
    at: str               # finished_at
    sha7: Optional[str]
    word: str             # Live | Failed | Rolled back ...
    tone: str
    ref: str              # "<kind>:<id>" → Infra deploy drawer


class PulseRow(TypedDict, total=False):
    target_id: str
    app: str
    env: str
    host: Optional[dict[str, str]]   # {id, name}
    state: str                       # up | degraded | down | unknown | not_deployed
    word: str
    tone: str
    since: Optional[str]             # status_since
    uptime: Optional[float]          # ok/n over the window (ratio 0..1); None with no probes
    p95_ms: Optional[int]            # None → "—" (No latency recorded)
    strip: list[PulseCell]
    pins: list[Pin]
    incidents: int                   # in the window


class WindowInfo(TypedDict):
    win: str
    from_: str            # serialised as "from"
    to: str
    cell_s: int
    cells: int


class Pulse(TypedDict, total=False):
    counts: dict[str, int]           # up, slow, down, no_signal, not_deployed
    rows: list[PulseRow]             # ordered Down, Slow, No signal, Up, Not deployed; then name
    not_watched: list[dict[str, Any]]   # [{app, env, scrape_url, dashboard_url}]
    window: dict[str, Any]           # {win, from, to, cell_s, cells}
    attention: list[dict[str, Any]]  # area=observability items (app_down, system)
    #: The header's spend: metered spend of model calls in the last 24 h (a sum over llm_calls,
    #: not_included_in_subscription), and whether any of it was estimated. 0 with calls that all
    #: ran on a subscription → the header says nothing about money (cost only when > 0).
    spend_24h_micros: int
    spend_24h_estimated: bool
    partial: list[str]
    etag: str


class AppDetail(TypedDict, total=False):
    target_id: str
    app: str
    env: str
    state: str
    word: str
    tone: str
    since: Optional[str]
    facts: dict[str, Any]   # uptime_24h, uptime_7d, p95_ms, avg_ms, last_probe_at, probes, failed,
                            # build_sha7, deployed_at, observed_version, serving_mismatch(bool),
                            # host{id,name}, open_url, dashboard_url, last_error
    series: list[dict[str, Any]]     # ≤ 288: {at, p95, avg, ok, n}
    incidents: list[dict[str, Any]]  # {start, end|None, duration_s, first_error, deploy_before{ref, sha7}|None}
    pins: list[Pin]
    window: dict[str, Any]
    etag: str


# ── Models (§4.10) ──────────────────────────────────────────────────────────────────────────


class ModelGroupRow(TypedDict, total=False):
    key: str
    label: str
    role_key: Optional[str]          # group=role: the face
    guest: bool                      # group=engine: a `guest:` runner (chip)
    calls: int
    tokens_in: int
    tokens_out: int
    spend_micros: int                # metered only (not included_in_subscription)
    included: bool                   # every call ran on a subscription
    estimated: bool
    avg_ms: Optional[int]
    p95_ms: Optional[int]
    failed: int
    retries: int
    trend: list[int]                 # SPARK_POINTS values of the chosen metric


class Models(TypedDict, total=False):
    window: dict[str, Any]
    group: str
    metric: str
    totals: dict[str, Any]   # calls, tokens_in, tokens_out, spend_micros, included_calls,
                             # estimated(bool), p95_ms, failed
    spark: dict[str, list[float]]    # calls, tokens, spend, p95, errors — SPARK_POINTS each
    series: dict[str, Any]           # {buckets:[iso...], bands:[{key,label,values[]}]} top 5 + Other
    groups: list[ModelGroupRow]      # ≤ 50
    failed: list[dict[str, Any]]     # 5 newest failed: {at, role_key, stage, model, error_word, retries, run_id}
    routing: dict[str, Any]          # {rows[≤8], filters{provider[],budget[],boundary[],outcome[]}, total}
    etag: str


# ── System (§4.11; the stability work fills it) ─────────────────────────────────────────────


class System(TypedDict, total=False):
    state: str               # ok | warning | critical (the worst band); drives the tab dot
    reason: Optional[str]    # the first reason, for the tab dot's tooltip
    machine: dict[str, Any]  # GET /v1/system/health body: scope, scope_label, level, level_label,
                             # gauges{...}, plus bands{memory,swap,cpu,disk:{value,unit,band,word}}
    build: dict[str, Any]    # {sha7, built_at}
    oom_kills_new: Optional[int]
    work: dict[str, Any]     # queue, leased, running, in_flight, builds_at_once{label}
    stalls: dict[str, Any]   # {strip:[n×96], rows:[≤8 {at, word, subject, did, shadow, run_id}], total}
    findings: list[dict[str, Any]]   # AttentionItem wire, kind system_health (system_doctor)
    actions: list[dict[str, Any]]    # ≤ 8 newest doctor actions {at, action, target, outcome, automatic, improved}
    #: The host advice line (run_retrospective.host_advice; the HostAdviceBanner Workflows and
    #: Factory show), when there is one: {title ≤ 6 words, tip ≤ 14 words, href|None}.
    advice: Optional[dict[str, Any]]
    partial: list[str]
    etag: str


# ── Sources (§4.13) ─────────────────────────────────────────────────────────────────────────


class Sources(TypedDict, total=False):
    stack: dict[str, Any]    # {state, word, tone, host{id,name}|None, grafana_url, containers[{name,status}],
                             #  checked_at, error, can_deploy, needs_machine}
    scraped: list[dict[str, Any]]    # {app, env, scrape_url, dashboard_url, watched(bool)}
    sources: list[dict[str, Any]]    # {id, kind, name, url, enabled}
    gaps: list[dict[str, str]]       # {id, label, tip}
    counts: dict[str, int]
    partial: list[str]
    etag: str


# ── shared helpers ──────────────────────────────────────────────────────────────────────────

log = logging.getLogger(__name__)

#: How many newest samples decide a target's state and `since` — the same bound
#: deployment_health.health_view has always read (7 days, 2000 rows).
STATE_SAMPLES = 2000
STATE_LOOKBACK = timedelta(days=7)
#: The app drawer's latency chart: at most this many points over the window.
SERIES_POINTS = 288
#: A deploy that finished this long before an incident began is offered as "deploy before it".
DEPLOY_BEFORE = timedelta(hours=2)
#: Pins per Pulse row (a busy target redeploying every few minutes stays a readable row).
MAX_PINS = 48
#: Sources: the composed body is reused this long; `fresh=True` bypasses it.
SOURCES_TTL_S = 60.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _aware(value: Any) -> Any:
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _ratio(ok: int, n: int) -> Optional[float]:
    """ok/n as a measured ratio (0..1), None with nothing measured — never 0 for 'no probes'."""
    return round(ok / n, 4) if n else None


def _int(value: Any) -> Optional[int]:
    return None if value is None else int(round(float(value)))


def _p95(values: list[float]) -> Optional[float]:
    """percentile_cont(0.95): linear interpolation, the same answer Postgres gives."""
    if not values:
        return None
    ordered = sorted(values)
    pos = 0.95 * (len(ordered) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def window_bounds(win: str, cells: Optional[int] = None, *,
                  now: Optional[datetime] = None) -> dict[str, Any]:
    """The window, aligned to its cell width so the body (and its etag) stays still between
    cell boundaries. The newest cell is the one in progress; `to` is its end."""
    win = window_of(win)
    span, default = WINDOWS[win]
    n = cells if cells in ALLOWED_CELLS else default
    cell_s = max(1, span // n)
    epoch = int((now or _now()).timestamp())
    end = (epoch // cell_s + 1) * cell_s
    start = end - n * cell_s
    return {"win": win, "cells": n, "cell_s": cell_s, "start_epoch": start,
            "start": datetime.fromtimestamp(start, timezone.utc),
            "end": datetime.fromtimestamp(end, timezone.utc)}


def _window_wire(w: dict[str, Any]) -> dict[str, Any]:
    return {"win": w["win"], "from": _iso(w["start"]), "to": _iso(w["end"]),
            "cell_s": w["cell_s"], "cells": w["cells"]}


def _bucket_sql(column: str) -> str:
    return f"floor((extract(epoch FROM {column}) - :s0) / :cs)::int"


def _sealed(body: dict[str, Any]) -> dict[str, Any]:
    body.pop("etag", None)
    body["etag"] = etag_of({k: v for k, v in body.items() if k != "generated_at"})
    return body


def _health_word(state: str) -> dict[str, str]:
    from .infra_view import word
    return dict(word("health", state))


# ── targets, state, pins (shared by the Pulse and the drawer) ───────────────────────────────


def _targets(connection: Any, project_id: str, target_id: Optional[str] = None) -> list[dict]:
    rows = connection.execute(text(
        "SELECT t.id, t.app_name, t.environment, t.domains, t.public_url, t.host_id, "
        "       h.name AS host_name "
        "FROM deployment_targets t LEFT JOIN deployment_hosts h ON h.id = t.host_id "
        "WHERE t.project_id = :p AND t.status = 'active'"
        + (" AND t.id = :t" if target_id else "")
        + " ORDER BY t.app_name, t.environment"),
        {"p": project_id, **({"t": target_id} if target_id else {})}).mappings().all()
    return [dict(r) for r in rows]


def _deployed(connection: Any, ids: list[str]) -> dict[str, dict]:
    """target → its newest succeeded deployment_run (deployment_health's DEPLOYED rule)."""
    if not ids:
        return {}
    rows = connection.execute(text(
        "SELECT DISTINCT ON (deployment_target_id) deployment_target_id AS t, id, commit_ref, "
        "       finished_at "
        "FROM deployment_runs WHERE deployment_target_id = ANY(:ids) AND status = 'succeeded' "
        "ORDER BY deployment_target_id, finished_at DESC NULLS LAST"),
        {"ids": ids}).mappings().all()
    return {r["t"]: dict(r) for r in rows}


def _newest_samples(connection: Any, ids: list[str], now: datetime) -> dict[str, list[dict]]:
    """target → newest-first samples (≤ STATE_SAMPLES within 7 days): ONE lateral query for
    every target, replacing health_view's query per target."""
    out: dict[str, list[dict]] = {i: [] for i in ids}
    if not ids:
        return out
    rows = connection.execute(text(
        "SELECT x.id AS t, s.checked_at, s.ok, s.status_code, s.latency_ms, s.error, "
        "       s.observed_version "
        "FROM unnest(CAST(:ids AS text[])) AS x(id) CROSS JOIN LATERAL ("
        "  SELECT checked_at, ok, status_code, latency_ms, error, observed_version "
        "  FROM deployment_health_samples "
        "  WHERE deployment_target_id = x.id AND checked_at >= :cut "
        "  ORDER BY checked_at DESC LIMIT :lim) s"),
        {"ids": ids, "cut": now - STATE_LOOKBACK, "lim": STATE_SAMPLES}).mappings().all()
    for r in rows:
        d = dict(r)
        d["checked_at"] = _aware(d["checked_at"])
        out[d.pop("t")].append(d)
    return out


def _state_of(samples: list[dict], deployed: bool) -> tuple[str, Optional[str]]:
    from .deployment_health import status_since, verdict

    if not deployed:
        return "not_deployed", None
    since = status_since(samples)
    return verdict(samples), _iso(since)


def _pins_fallback(connection: Any, ids: list[str], start: datetime,
                   end: Optional[datetime] = None) -> dict[str, list[dict[str, Any]]]:
    """Stack deploys and registry runs that FINISHED in the window, oldest first."""
    from .infra_view import deploy_word

    out: dict[str, list[dict[str, Any]]] = {i: [] for i in ids}
    if not ids:
        return out
    rows = connection.execute(text(
        "SELECT * FROM ("
        " SELECT 'stack' AS kind, id, target_id AS t, commit_sha AS sha, status, finished_at "
        " FROM stack_deployments WHERE target_id = ANY(:ids) AND finished_at >= :a "
        " UNION ALL "
        " SELECT 'registry', id, deployment_target_id, commit_ref, status, finished_at "
        " FROM deployment_runs WHERE deployment_target_id = ANY(:ids) AND finished_at >= :a"
        ") d" + (" WHERE finished_at < :b" if end else "") + " ORDER BY finished_at, id"),
        {"ids": ids, "a": start, **({"b": end} if end else {})}).mappings().all()
    for r in rows:
        out[r["t"]].append(dict(r))
    pins: dict[str, list[dict[str, Any]]] = {}
    for t, deploys in out.items():
        rows_t = deploys[-MAX_PINS:]
        pins[t] = []
        for i, d in enumerate(rows_t):
            w = deploy_word(d["status"], newest_for_target=(i == len(rows_t) - 1))
            pins[t].append({"at": _iso(_aware(d["finished_at"])),
                            "sha7": (str(d["sha"])[:7] if d["sha"] else None),
                            "word": w["word"], "tone": w["tone"],
                            "ref": f"{d['kind']}:{d['id']}"})
    return pins


def _pins(connection: Any, ids: list[str], start: datetime, end: datetime,
          partial: list[str]) -> dict[str, list[dict[str, Any]]]:
    """infra_view.deploy_pins (Infra's one derivation) when it is built; this stream's own
    query until then. A reader that fails is a `partial`, never an error."""
    from . import infra_view

    try:
        got = infra_view.deploy_pins(connection, ids, start)
        return {i: [p for p in (got.get(i) or []) if str(p.get("at") or "") < _iso(end)]
                [-MAX_PINS:] for i in ids}
    except NotImplementedError:
        pass
    except Exception:  # noqa: BLE001
        log.exception("obs_view: infra_view.deploy_pins failed")
    try:
        with connection.begin_nested():
            return _pins_fallback(connection, ids, start, end)
    except Exception:  # noqa: BLE001
        log.exception("obs_view: deploy pins failed")
        partial.append("pins")
        return {i: [] for i in ids}


def _grafana_base(connection: Any, project_id: str) -> Optional[str]:
    """Where Grafana is, from the registry (the registered boss-prometheus source's host),
    never from a live probe. None when the stack was never deployed."""
    from .observability_stack import DEFAULT_PORTS

    row = connection.execute(text(
        "SELECT config FROM telemetry_sources WHERE project_id = :p AND kind = 'prometheus' "
        "AND name = 'boss-prometheus'"), {"p": project_id}).mappings().first()
    if not row:
        return None
    cfg = row["config"] or {}
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except json.JSONDecodeError:
            cfg = {}
    host = urlparse(str(cfg.get("base_url") or "")).hostname
    return f"http://{host}:{DEFAULT_PORTS['grafana']}" if host else None


def _dashboard_url(base: Optional[str], app: str, project_id: str) -> Optional[str]:
    """A link only to a dashboard that exists, when the last `Check now` asked Grafana; the
    link as before when Grafana was never asked (observability_stack's rule)."""
    from .observability_stack import dashboard_uid, grafana_url

    if not base:
        return None
    probe = _STACK_PROBES.get(project_id) or {}
    existing = probe.get("dashboards")
    if existing is not None and dashboard_uid(app) not in existing:
        return None
    return grafana_url(base, app)


# ── Apps: the Pulse (§4.9) ──────────────────────────────────────────────────────────────────

#: Down, Slow, No signal, Up, Not deployed.
_ORDER = {"down": 0, "degraded": 1, "unknown": 2, "up": 3, "not_deployed": 4}


def _strips(connection: Any, ids: list[str], w: dict[str, Any]
            ) -> tuple[dict[str, list[list]], dict[str, dict[str, Any]]]:
    """ONE scan, one grouped query: per (target, bucket) and per target — count, ok, the p95
    of the successful probes' latency (a timeout's duration is not a latency), and per target
    the incidents that opened in the window (`_opened_sql`)."""
    strips = {i: [[0, 0, None] for _ in range(w["cells"])] for i in ids}
    rollup: dict[str, dict[str, Any]] = {
        i: {"n": 0, "ok": 0, "p95": None, "incidents": 0} for i in ids}
    if not ids:
        return strips, rollup
    lags, opened = _opened_sql()
    rows = connection.execute(text(
        "SELECT t, b, GROUPING(b) AS whole, count(*) AS n, "
        "       sum(CASE WHEN ok THEN 1 ELSE 0 END) AS okn, "
        "       percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) "
        "         FILTER (WHERE ok AND latency_ms IS NOT NULL) AS p95, "
        "       count(*) FILTER (WHERE opened) AS incidents "
        "FROM (SELECT t, ok, latency_ms, b, (NOT ok AND " + opened + ") AS opened "
        "      FROM (SELECT deployment_target_id AS t, ok, latency_ms, "
        f"                   {_bucket_sql('checked_at')} AS b, {lags} "
        "            FROM deployment_health_samples "
        "            WHERE deployment_target_id = ANY(:ids) AND checked_at >= :a "
        "              AND checked_at < :z "
        "            WINDOW w AS (PARTITION BY deployment_target_id ORDER BY checked_at DESC)"
        "           ) l"
        "     ) s "
        "GROUP BY GROUPING SETS ((t, b), (t))"),
        {"ids": ids, "a": w["start"], "z": w["end"], "s0": w["start_epoch"],
         "cs": w["cell_s"]}).mappings().all()
    for r in rows:
        if r["whole"]:
            rollup[r["t"]] = {"n": int(r["n"]), "ok": int(r["okn"] or 0), "p95": _int(r["p95"]),
                              "incidents": int(r["incidents"] or 0)}
        elif 0 <= int(r["b"]) < w["cells"]:
            strips[r["t"]][int(r["b"])] = [int(r["okn"] or 0), int(r["n"]), _int(r["p95"])]
    return strips, rollup


def _opened_sql() -> tuple[str, str]:
    """An incident OPENS on the k-th consecutive failed probe (deployment_health.incidents'
    rule): the k-1 probes before it failed and the k-th before it did not, or does not exist.
    Returns (the lag columns over WINDOW w, the predicate over them)."""
    from .deployment_health import DOWN_AFTER_CONSECUTIVE_FAILURES as k

    # `lead` over newest-first is the probe i before it in time: the window then reads the
    # (target, checked_at DESC) index in its own order, with no sort.
    lags = ", ".join(f"lead(ok, {i}) OVER w AS l{i}" for i in range(1, k + 1))
    opened = " AND ".join([f"l{i} IS FALSE" for i in range(1, k)] + [f"l{k} IS NOT FALSE"])
    return lags, f"({opened})"


def _not_watched(connection: Any, project_id: str, targets: list[dict],
                 base: Optional[str]) -> list[dict[str, Any]]:
    """Scraped by Prometheus with no health target watching it (today's monitoring-orphans):
    the scrape list and the health list are keyed differently and can legitimately differ."""
    from .observability_stack import scrape_targets

    watched = {(t["app_name"], t["environment"]) for t in targets}
    return [{"app": s["app"], "env": s["environment"],
             "scrape_url": f"{s['scrape_url']}/metrics",
             "dashboard_url": _dashboard_url(base, s["app"], project_id)}
            for s in scrape_targets(connection, project_id)
            if (s["app"], s["environment"]) not in watched]


def _attention(connection: Any, settings: Any, project_id: str,
               partial: list[str]) -> list[dict[str, Any]]:
    from . import infra_view, system_doctor

    items: list[dict[str, Any]] = []
    try:
        items += [i for i in infra_view.attention(connection, settings, project_id)
                  if i.get("area") == "observability"]
    except NotImplementedError:
        pass
    except Exception:  # noqa: BLE001
        log.exception("obs_view: infra attention failed")
        partial.append("attention")
    try:
        with connection.begin_nested():
            items += system_doctor.card_items(connection, project_id)
    except Exception:  # noqa: BLE001
        log.exception("obs_view: system findings failed")
        partial.append("findings")
    return items


def _spend_24h(connection: Any, project_id: str) -> tuple[int, bool]:
    row = connection.execute(text(
        "SELECT COALESCE(sum(cost_usd_micros) FILTER (WHERE NOT included_in_subscription), 0) "
        "         AS spend, COALESCE(bool_or(price_estimated), false) AS est "
        "FROM llm_calls WHERE project_id = :p AND created_at >= now() - interval '24 hours'"),
        {"p": project_id}).mappings().first()
    return int(row["spend"] or 0), bool(row["est"])


def pulse(connection: Any, settings: Any, project_id: str, *, win: str = "24h",
          cells: Optional[int] = None) -> Pulse:
    now = _now()
    w = window_bounds(win, cells, now=now)
    partial: list[str] = []
    targets = _targets(connection, project_id)
    ids = [t["id"] for t in targets]
    deployed = _deployed(connection, ids)
    samples = _newest_samples(connection, ids, now)
    strips, rollup = _strips(connection, ids, w)
    pins = _pins(connection, ids, w["start"], w["end"], partial)

    rows: list[PulseRow] = []
    for t in targets:
        tid = t["id"]
        state, since = _state_of(samples[tid], tid in deployed)
        word = _health_word(state)
        rows.append({
            "target_id": tid, "app": t["app_name"], "env": t["environment"],
            "host": ({"id": t["host_id"], "name": t["host_name"] or t["host_id"]}
                     if t["host_id"] else None),
            "state": state, "word": word["word"], "tone": word["tone"], "since": since,
            "uptime": _ratio(rollup[tid]["ok"], rollup[tid]["n"]),
            "p95_ms": rollup[tid]["p95"], "strip": strips[tid], "pins": pins.get(tid, []),
            "incidents": rollup[tid]["incidents"],
        })
    rows.sort(key=lambda r: (_ORDER.get(r["state"], 9), r["app"].lower(), r["env"]))
    counts = {"up": 0, "slow": 0, "down": 0, "no_signal": 0, "not_deployed": 0}
    key = {"up": "up", "degraded": "slow", "down": "down", "unknown": "no_signal",
           "not_deployed": "not_deployed"}
    for r in rows:
        counts[key[r["state"]]] += 1

    not_watched: list[dict[str, Any]] = []
    try:
        with connection.begin_nested():
            not_watched = _not_watched(connection, project_id, targets,
                                       _grafana_base(connection, project_id))
    except Exception:  # noqa: BLE001
        log.exception("obs_view: not-watched failed")
        partial.append("not_watched")
    spend, estimated = 0, False
    try:
        with connection.begin_nested():
            spend, estimated = _spend_24h(connection, project_id)
    except Exception:  # noqa: BLE001
        partial.append("spend")
    body: Pulse = {
        "counts": counts, "rows": rows, "not_watched": not_watched,
        "window": _window_wire(w),
        "attention": _attention(connection, settings, project_id, partial),
        "spend_24h_micros": spend, "spend_24h_estimated": estimated,
        "partial": partial,
    }
    return _sealed(body)  # type: ignore[return-value]


# ── Apps: one app's drawer ──────────────────────────────────────────────────────────────────


def app_detail(connection: Any, settings: Any, project_id: str, target_id: str, *,
               win: str = "24h") -> AppDetail:
    from .deployment_health import incidents as derive_incidents

    now = _now()
    # The drawer's window ends where the Pulse's does; its chart is ≤ 288 buckets of it.
    pw = window_bounds(win, None, now=now)
    span = pw["cells"] * pw["cell_s"]
    cell_s = max(1, -(-span // SERIES_POINTS))
    points = -(-span // cell_s)
    targets = _targets(connection, project_id, target_id)
    if not targets:
        raise LookupError(f"No app {target_id} in this project.")
    t = targets[0]
    partial: list[str] = []
    dep = _deployed(connection, [target_id]).get(target_id)

    since_at = min(pw["start"], now - STATE_LOOKBACK)
    rows = [dict(r) for r in connection.execute(text(
        "SELECT checked_at, ok, status_code, latency_ms, error, observed_version "
        "FROM deployment_health_samples WHERE deployment_target_id = :t "
        "AND checked_at >= :a AND checked_at < :z ORDER BY checked_at DESC"),
        {"t": target_id, "a": since_at, "z": pw["end"]}).mappings().all()]
    for r in rows:
        r["checked_at"] = _aware(r["checked_at"])
    state, since = _state_of(rows[:STATE_SAMPLES], dep is not None)
    word = _health_word(state)
    in_win = [r for r in rows if r["checked_at"] >= pw["start"]]

    def uptime(after: datetime) -> Optional[float]:
        got = [r for r in rows if r["checked_at"] >= after]
        return _ratio(sum(1 for r in got if r["ok"]), len(got))

    lat = [float(r["latency_ms"]) for r in in_win if r["ok"] and r["latency_ms"] is not None]
    buckets: list[list[dict]] = [[] for _ in range(points)]
    for r in in_win:
        b = int((r["checked_at"] - pw["start"]).total_seconds() // cell_s)
        if 0 <= b < points:
            buckets[b].append(r)
    series = []
    for i, got in enumerate(buckets):
        ms = [float(r["latency_ms"]) for r in got if r["ok"] and r["latency_ms"] is not None]
        series.append({
            "at": _iso(pw["start"] + timedelta(seconds=i * cell_s)),
            "p95": _int(_p95(ms)), "avg": _int(sum(ms) / len(ms)) if ms else None,
            "ok": sum(1 for r in got if r["ok"]), "n": len(got)})

    deploys = []
    try:
        with connection.begin_nested():
            deploys = _pins_fallback(connection, [target_id], pw["start"] - DEPLOY_BEFORE,
                                     pw["end"])[target_id]
    except Exception:  # noqa: BLE001
        partial.append("deploys")
    incidents = []
    for inc in derive_incidents(in_win):
        start = inc["started_at"]
        before = None
        for d in reversed(deploys):
            at = datetime.fromisoformat(d["at"])
            if at <= start and start - at <= DEPLOY_BEFORE:
                before = {"ref": d["ref"], "sha7": d["sha7"]}
                break
        incidents.append({"start": _iso(start), "end": _iso(inc["ended_at"]),
                          "duration_s": inc["duration_seconds"], "first_error": inc["error"],
                          "deploy_before": before})

    newest = rows[0] if rows else None
    observed = newest.get("observed_version") if newest else None
    commit = (dep or {}).get("commit_ref")
    mismatch = bool(observed and commit and not (
        str(commit).startswith(str(observed)) or str(observed).startswith(str(commit))))
    base = None
    try:
        with connection.begin_nested():
            base = _grafana_base(connection, project_id)
    except Exception:  # noqa: BLE001
        partial.append("dashboard")
    body: AppDetail = {
        "target_id": target_id, "app": t["app_name"], "env": t["environment"],
        "state": state, "word": word["word"], "tone": word["tone"], "since": since,
        "facts": {
            "uptime_24h": uptime(now - timedelta(hours=24)),
            "uptime_7d": uptime(now - timedelta(days=7)),
            "p95_ms": _int(_p95(lat)), "avg_ms": _int(sum(lat) / len(lat)) if lat else None,
            "last_probe_at": _iso(newest["checked_at"]) if newest else None,
            "probes": len(in_win), "failed": sum(1 for r in in_win if not r["ok"]),
            "build_sha7": str(commit)[:7] if commit else None,
            "deployed_at": _iso(_aware((dep or {}).get("finished_at"))),
            "observed_version": observed, "serving_mismatch": mismatch,
            "host": ({"id": t["host_id"], "name": t["host_name"] or t["host_id"]}
                     if t["host_id"] else None),
            "open_url": t.get("public_url") or ((t.get("domains") or [None])[0]),
            "dashboard_url": _dashboard_url(base, t["app_name"], project_id),
            "last_error": newest.get("error") if newest else None,
        },
        "series": series,
        "incidents": incidents,
        "pins": _pins(connection, [target_id], pw["start"], pw["end"], partial)[target_id],
        "window": {**_window_wire(pw), "series_cell_s": cell_s},
    }
    if partial:
        body["partial"] = partial  # type: ignore[typeddict-unknown-key]
    return _sealed(body)  # type: ignore[return-value]


def probe_app(engine: Any, settings: Any, project_id: str, target_id: str) -> dict[str, Any]:
    """`Probe now` for ONE app (operator decision §11.5): probe it through the connector the
    sweep uses, record the sample, answer the state word it leaves the app in."""
    from ..db import begin
    from .core import make_id
    from .deployment_health import probe

    with begin(engine) as c:
        row = c.execute(text(
            "SELECT * FROM deployment_targets WHERE id = :t AND project_id = :p "
            "AND status = 'active'"), {"t": target_id, "p": project_id}).mappings().first()
        if not row:
            raise LookupError(f"No app {target_id} in this project.")
        target = dict(row)
        deployed = target_id in _deployed(c, [target_id])
    if not deployed:
        w = _health_word("not_deployed")
        return {"word": w["word"], "tone": w["tone"], "checked_at": None, "probed": False}
    sample = probe(target, settings)                # outside a transaction: network I/O
    at = _now()
    with begin(engine) as c:
        c.execute(text(
            "INSERT INTO deployment_health_samples (id, project_id, deployment_target_id, "
            " checked_at, ok, status_code, latency_ms, error) "
            "VALUES (:id, :p, :t, :at, :ok, :sc, :ms, :err)"),
            {"id": make_id("dhs"), "p": project_id, "t": target_id, "at": at,
             "ok": sample["ok"], "sc": sample["status_code"], "ms": sample["latency_ms"],
             "err": sample["error"]})
        state, _since = _state_of(_newest_samples(c, [target_id], at)[target_id], True)
    w = _health_word(state)
    return {"word": w["word"], "tone": w["tone"], "checked_at": _iso(at), "probed": True,
            "ok": bool(sample["ok"])}


# ── Models (§4.10) ──────────────────────────────────────────────────────────────────────────

#: model_failures.explain kinds → the ≤ 4-word error word FAILED CALLS shows.
_ERROR_WORDS = {
    "storage": "Database error", "credential_store_wait": "Secret store waiting",
    "credential_unreadable": "Login unreadable", "capacity_wait": "Capacity busy",
    "login_rejected": "Login rejected", "budget_hold": "Held by budget",
    "budget_wait": "Waiting for budget", "budget_refused": "Budget refused",
    "capacity_owner": "Not your subscription", "provider_requirement": "Provider not connected",
    "no_model": "No model connected", "no_direct_answer": "No direct answer",
    "refused_by_api": "API refused", "reconcile": "Needs reconciling",
}


def error_word(error: Any) -> str:
    """≤ 4 words for a failed model call: model_failures' classification when it has one."""
    text_ = str(error or "").lower()
    if not text_:
        return "Failed"
    if "rate limit" in text_ or "rate_limit" in text_ or " 429" in text_:
        return "Rate limited"
    if "timed out" in text_ or "timeout" in text_:
        return "Timed out"
    try:
        from .model_failures import explain
        return _ERROR_WORDS.get(str(explain(error).get("kind")), "Failed")
    except Exception:  # noqa: BLE001
        return "Failed"


def _group_label(group: str, key: str) -> tuple[str, bool]:
    if key == "?":
        return "Unknown", False
    if group == "engine" and key.startswith("guest:"):
        return key[len("guest:"):], True
    return key, False


def _metric_of(metric: str, cell: dict[str, Any]) -> float:
    if metric == "calls":
        return cell["calls"]
    if metric == "spend":
        return cell["spend_micros"]
    if metric == "latency":
        return cell["p95_ms"] or 0
    return cell["tokens_in"] + cell["tokens_out"]


def _routing(connection: Any, project_id: str, w: dict[str, Any]) -> dict[str, Any]:
    outcome = ("CASE WHEN denial_reason IS NOT NULL THEN 'Denied' "
               "WHEN selected_source_id IS NOT NULL THEN 'Routed' ELSE 'No route' END")
    params = {"p": project_id, "a": w["start"], "z": w["end"]}
    rows = connection.execute(text(
        f"SELECT id, created_at, selected_provider, budget_state, data_boundary, "
        f"       selected_model_id, {outcome} AS outcome "
        f"FROM routing_logs WHERE project_id = :p AND created_at >= :a AND created_at < :z "
        f"ORDER BY created_at DESC, id LIMIT 8"), params).mappings().all()
    agg = connection.execute(text(
        f"SELECT count(*) AS total, "
        f"  array_remove(array_agg(DISTINCT selected_provider), NULL) AS provider, "
        f"  array_remove(array_agg(DISTINCT budget_state), NULL) AS budget, "
        f"  array_remove(array_agg(DISTINCT data_boundary), NULL) AS boundary, "
        f"  array_agg(DISTINCT {outcome}) AS outcome "
        f"FROM routing_logs WHERE project_id = :p AND created_at >= :a AND created_at < :z"),
        params).mappings().first()
    return {
        "rows": [{"id": r["id"], "at": _iso(_aware(r["created_at"])),
                  "provider": r["selected_provider"], "budget": r["budget_state"],
                  "boundary": r["data_boundary"], "outcome": r["outcome"],
                  "model": r["selected_model_id"]} for r in rows],
        "filters": {k: sorted(str(v) for v in (agg[k] or []) if v is not None)
                    for k in ("provider", "budget", "boundary", "outcome")},
        "total": int(agg["total"] or 0),
    }


def models(connection: Any, settings: Any, project_id: str, *, win: str = "24h",
           group: str = "model", metric: str = "tokens") -> Models:
    """ONE grouped aggregate over llm_calls for the window: totals, the 24-point sparks, every
    group's row and trend, and the stacked series — all GROUPING SETS of the same scan."""
    from .llm_telemetry import _SUMMARY_GROUP_COLS

    group = group if group in _SUMMARY_GROUP_COLS else "model"
    metric = metric if metric in ("tokens", "calls", "spend", "latency") else "tokens"
    w = window_bounds(win, SPARK_POINTS)
    n = w["cells"]
    partial: list[str] = []
    rows = connection.execute(text(
        "SELECT g, b, GROUPING(g) AS no_g, GROUPING(b) AS no_b, count(*) AS calls, "
        "  COALESCE(sum(prompt_tokens), 0) AS tin, COALESCE(sum(completion_tokens), 0) AS tout, "
        "  COALESCE(sum(cost_usd_micros) FILTER (WHERE NOT included_in_subscription), 0) AS spend,"
        "  count(*) FILTER (WHERE included_in_subscription) AS included, "
        "  COALESCE(bool_or(price_estimated), false) AS est, "
        "  avg(latency_ms) FILTER (WHERE latency_ms > 0) AS avg_ms, "
        "  percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) "
        "    FILTER (WHERE latency_ms > 0) AS p95, "
        "  count(*) FILTER (WHERE NOT ok) AS failed, COALESCE(sum(retries), 0) AS retries "
        f"FROM (SELECT {_SUMMARY_GROUP_COLS[group]} AS g, {_bucket_sql('created_at')} AS b, "
        "        prompt_tokens, completion_tokens, cost_usd_micros, included_in_subscription, "
        "        price_estimated, latency_ms, ok, retries "
        "      FROM llm_calls WHERE project_id = :p AND created_at >= :a AND created_at < :z) c "
        "GROUP BY GROUPING SETS ((g, b), (g), (b), ())"),
        {"p": project_id, "a": w["start"], "z": w["end"], "s0": w["start_epoch"],
         "cs": w["cell_s"]}).mappings().all()

    def cell(r: Any) -> dict[str, Any]:
        return {"calls": int(r["calls"]), "tokens_in": int(r["tin"]),
                "tokens_out": int(r["tout"]), "spend_micros": int(r["spend"]),
                "included_calls": int(r["included"]), "estimated": bool(r["est"]),
                "avg_ms": _int(r["avg_ms"]), "p95_ms": _int(r["p95"]),
                "failed": int(r["failed"]), "retries": int(r["retries"])}

    total = cell({"calls": 0, "tin": 0, "tout": 0, "spend": 0, "included": 0, "est": False,
                  "avg_ms": None, "p95": None, "failed": 0, "retries": 0})
    by_bucket: dict[int, dict[str, Any]] = {}
    by_group: dict[str, dict[str, Any]] = {}
    group_buckets: dict[str, dict[int, dict[str, Any]]] = {}
    for r in rows:
        if r["no_g"] and r["no_b"]:
            total = cell(r)
        elif r["no_g"]:
            by_bucket[int(r["b"])] = cell(r)
        elif r["no_b"]:
            by_group[str(r["g"])] = cell(r)
        else:
            group_buckets.setdefault(str(r["g"]), {})[int(r["b"])] = cell(r)

    empty = cell({"calls": 0, "tin": 0, "tout": 0, "spend": 0, "included": 0, "est": False,
                  "avg_ms": None, "p95": None, "failed": 0, "retries": 0})
    buckets = [by_bucket.get(i, empty) for i in range(n)]
    spark = {
        "calls": [c["calls"] for c in buckets],
        "tokens": [c["tokens_in"] + c["tokens_out"] for c in buckets],
        "spend": [c["spend_micros"] for c in buckets],
        "p95": [c["p95_ms"] or 0 for c in buckets],
        "errors": [c["failed"] for c in buckets],
    }

    def trend(key: str) -> list[float]:
        got = group_buckets.get(key, {})
        return [_metric_of(metric, got.get(i, empty)) for i in range(n)]

    ranked = sorted(by_group, key=lambda k: (-by_group[k]["spend_micros"],
                                             -by_group[k]["calls"], k))
    groups: list[ModelGroupRow] = []
    for k in ranked[:50]:
        g = by_group[k]
        label, guest = _group_label(group, k)
        groups.append({
            "key": k, "label": label,
            "role_key": (k if group == "role" and k != "?" else None), "guest": guest,
            "calls": g["calls"], "tokens_in": g["tokens_in"], "tokens_out": g["tokens_out"],
            "spend_micros": g["spend_micros"],
            "included": g["calls"] > 0 and g["included_calls"] == g["calls"],
            "estimated": g["estimated"], "avg_ms": g["avg_ms"], "p95_ms": g["p95_ms"],
            "failed": g["failed"], "retries": g["retries"], "trend": trend(k),
        })

    # Bands: the top 5 groups by the chosen metric over the window, plus Other (the rest, by
    # subtraction from the bucket totals — the same scan). Latency is a p95 line per group:
    # a percentile cannot be subtracted, so it has no Other.
    top = sorted(by_group, key=lambda k: (-_metric_of(metric, by_group[k]), k))[:5]
    bands = []
    for k in top:
        bands.append({"key": k, "label": _group_label(group, k)[0], "values": trend(k)})
    if metric != "latency" and len(by_group) > len(top):
        whole = [_metric_of(metric, c) for c in buckets]
        other = [max(0, whole[i] - sum(b["values"][i] for b in bands)) for i in range(n)]
        bands.append({"key": "__other__", "label": "Other", "values": other})

    failed: list[dict[str, Any]] = []
    try:
        with connection.begin_nested():
            for r in connection.execute(text(
                    "SELECT id, created_at, role, stage, model, error, retries, code_task_run_id "
                    "FROM llm_calls WHERE project_id = :p AND NOT ok AND created_at >= :a "
                    "AND created_at < :z ORDER BY created_at DESC, id LIMIT 5"),
                    {"p": project_id, "a": w["start"], "z": w["end"]}).mappings():
                failed.append({"id": r["id"], "at": _iso(_aware(r["created_at"])),
                               "role_key": r["role"], "stage": r["stage"], "model": r["model"],
                               "error_word": error_word(r["error"]),
                               "retries": int(r["retries"] or 0),
                               "run_id": r["code_task_run_id"]})
    except Exception:  # noqa: BLE001
        log.exception("obs_view: failed calls")
        partial.append("failed")
    routing: dict[str, Any] = {"rows": [], "filters": {"provider": [], "budget": [],
                                                       "boundary": [], "outcome": []},
                               "total": 0}
    try:
        with connection.begin_nested():
            routing = _routing(connection, project_id, w)
    except Exception:  # noqa: BLE001
        log.exception("obs_view: routing")
        partial.append("routing")

    body: Models = {
        "window": _window_wire(w), "group": group, "metric": metric,
        "totals": {"calls": total["calls"], "tokens_in": total["tokens_in"],
                   "tokens_out": total["tokens_out"], "spend_micros": total["spend_micros"],
                   "included_calls": total["included_calls"], "estimated": total["estimated"],
                   "p95_ms": total["p95_ms"], "failed": total["failed"]},
        "spark": spark,  # type: ignore[typeddict-item]
        "series": {"buckets": [_iso(w["start"] + timedelta(seconds=i * w["cell_s"]))
                               for i in range(n)], "bands": bands},
        "groups": groups, "failed": failed, "routing": routing,
    }
    if partial:
        body["partial"] = partial  # type: ignore[typeddict-unknown-key]
    return _sealed(body)  # type: ignore[return-value]


# ── System (§4.11): the stability work's real data ──────────────────────────────────────────

#: shepherd_watch classes that are a stall of some kind → the short word System shows.
STALL_WORDS = {2: "Orphaned", 3: "Stalled in phase", 4: "Over usual time", 5: "Starved",
               6: "Abandoned", 9: "Killed by platform"}
_STALL_SUBJECTS = {"api": "api", "worker.idle_wait": "worker"}


def health_body(cap: Any, state: dict[str, Any]) -> dict[str, Any]:
    """GET /v1/system/health's body: one derivation, the route and System both read it."""
    from . import admission

    scope_words = {"machine": "This server", "docker_vm": "Docker Desktop VM, not your Mac",
                   "container": "This BOSS container's memory cap"}
    level = int(state.get("level") or 0)
    return {
        "scope": cap.scope, "scope_label": scope_words.get(cap.scope, cap.scope),
        "level": level, "level_label": admission.LEVEL_WORDS.get(level, str(level)),
        "gauges": {
            "memory_total_mb": round(cap.total_mb), "memory_available_mb": round(cap.available_mb),
            "memory_reserved_for_boss_mb": round(cap.reserve_mb),
            "memory_pressure_full_avg10": cap.psi_full_avg10,
            "memory_pressure_full_avg60": cap.psi_full_avg60,
            "swap_used_mb": round(cap.swap_used_mb), "disk_free_gb": cap.disk_free_gb,
            "disk_used_pct": cap.disk_used_pct, "load_per_core": cap.load_per_core,
            "cores": cap.cores,
        },
        "admission": state,
    }


def _pressure(cap: Any) -> dict[str, Any]:
    """self_observability.pressure_now(), minus its preview-tree walk: disk is the statvfs
    reading admission.sample() already took (System is polled every 10 s)."""
    from .self_observability import cpu_snapshot, memory_snapshot, new_oom_kills, pressure

    memory = memory_snapshot()
    memory["oom_kills_new"] = new_oom_kills(memory.get("oom_kills"))
    disk = {"used_pct": cap.disk_used_pct} if cap.disk_used_pct else {}
    got = pressure(memory, cpu_snapshot(), disk)
    got["oom_kills_new"] = memory.get("oom_kills_new")
    got["swap_measured"] = memory.get("host_swap_used_mb") is not None and bool(
        memory.get("host_total_mb"))
    return got


def _bands(p: dict[str, Any], cap: Any) -> dict[str, Any]:
    from .infra_view import word

    tip = ("BOSS's Docker VM, not this computer" if p.get("scope") == "vm"
           else "Measured inside the BOSS container, not your deploy hosts")
    raw = p.get("bands") or {}
    out: dict[str, Any] = {}

    def band(value: Any, unit: str, b: str) -> dict[str, Any]:
        w = word("band", b)
        return {"value": value, "unit": unit, "band": b, "word": w["word"], "tone": w["tone"],
                "tip": tip}

    if "memory" in raw:
        out["memory"] = band(raw["memory"].get("used_pct"), "%", raw["memory"]["band"])
    if "cpu" in raw:
        out["cpu"] = band(raw["cpu"].get("load_per_core"), "/core", raw["cpu"]["band"])
    if "disk" in raw:
        out["disk"] = band(raw["disk"].get("used_pct"), "%", raw["disk"]["band"])
    if "swap" in raw:
        out["swap"] = band(raw["swap"].get("used_mb"), "MB", raw["swap"]["band"])
    elif p.get("swap_measured"):
        out["swap"] = band(round(float(cap.swap_used_mb), 1), "MB", "ok")
    return out


def stalls(connection: Any, project_id: str, *, since: Any, cells: int = 96) -> dict[str, Any]:
    """Stalls per cell since `since`: host.stalled events (the machine; recorded on the install's
    default project) plus this project's shepherd_watch_observations of a stall class."""
    from ..config import get_settings

    since = _aware(since)
    now = _now()
    cell_s = max(60, int(math.ceil((now - since).total_seconds() / max(1, cells) / 60.0)) * 60)
    end = since + timedelta(seconds=cells * cell_s)
    projects = sorted({project_id, str(get_settings().default_project_id or project_id)})
    params = {"ps": projects, "p": project_id, "a": since, "z": end,
              "s0": int(since.timestamp()), "cs": cell_s, "classes": sorted(STALL_WORDS)}
    union = (
        "SELECT 'host' AS src, e.created_at AS at, e.entity_id AS subject, "
        "       (e.data->>'stalled_seconds') AS secs, e.data->>'source' AS source, "
        "       NULL::int AS watch_class, NULL AS action, NULL AS mode, NULL::boolean AS acted, "
        "       NULL AS run_id, NULL AS title "
        "FROM events e WHERE e.project_id = ANY(:ps) AND e.event_type = 'host.stalled' "
        "  AND (e.data->>'source') IS NOT NULL AND e.created_at >= :a AND e.created_at < :z "
        "UNION ALL "
        "SELECT 'watch', o.created_at, NULL, NULL, NULL, o.watch_class, o.action, o.mode, "
        "       o.acted, o.run_id, NULL "
        "FROM shepherd_watch_observations o WHERE o.project_id = :p "
        "  AND o.watch_class = ANY(:classes) AND o.created_at >= :a AND o.created_at < :z")
    strip = [0] * cells
    for r in connection.execute(text(
            f"SELECT {_bucket_sql('at')} AS b, count(*) AS n FROM ({union}) u GROUP BY 1"),
            params).mappings():
        if 0 <= int(r["b"]) < cells:
            strip[int(r["b"])] = int(r["n"])
    rows = []
    newest = connection.execute(text(
        f"SELECT u.*, t.title AS task_title FROM ({union}) u "
        "LEFT JOIN code_task_runs r ON r.id = u.run_id LEFT JOIN tasks t ON t.id = r.task_id "
        "ORDER BY u.at DESC LIMIT 8"), params).mappings().all()
    for r in newest:
        if r["src"] == "host":
            secs = r["secs"]
            who = _STALL_SUBJECTS.get(str(r["source"] or ""), str(r["subject"] or "host"))
            subject = f"{int(round(float(secs)))}s · {who}" if secs else who
            rows.append({"at": _iso(_aware(r["at"])), "word": "Host stalled",
                         "subject": subject, "did": None, "shadow": False, "run_id": None})
            continue
        action = str(r["action"] or "none")
        shadow = str(r["mode"] or "") == "shadow"
        did = None
        if action != "none":
            did = (f"would {action} (shadow)" if shadow
                   else f"{action} done" if r["acted"] else None)
        title = (str(r["task_title"] or "").splitlines() or [""])[0][:60] or r["run_id"]
        rows.append({"at": _iso(_aware(r["at"])),
                     "word": STALL_WORDS.get(int(r["watch_class"]), "Stalled"),
                     "subject": title, "did": did, "shadow": shadow, "run_id": r["run_id"]})
    return {"strip": strip, "rows": rows, "total": sum(strip), "cell_s": cell_s,
            "from": _iso(since), "to": _iso(end)}


def _queue(connection: Any, project_id: str) -> dict[str, int]:
    """worker_autoscaling.queue_snapshot's definition (the autoscaler's queue pressure)."""
    row = connection.execute(text(
        "SELECT count(*) FILTER (WHERE status IN ('queued', 'failed_retryable') "
        "                        AND run_after <= now()) AS queue, "
        "       count(*) FILTER (WHERE status = 'running') AS running, "
        "       count(*) FILTER (WHERE status = 'leased') AS leased "
        "FROM jobs WHERE project_id = :p"), {"p": project_id}).mappings().first()
    return {"queue": int(row["queue"] or 0), "running": int(row["running"] or 0),
            "leased": int(row["leased"] or 0)}


def _findings(connection: Any, project_id: str) -> list[dict[str, Any]]:
    """Every open system_doctor finding of this install (or this project), shaped as the
    `system_health` AttentionItem `system_doctor.card_items` builds — needs a choice or not."""
    now = _now()
    rows = connection.execute(text(
        "SELECT id, kind, severity, status, title, detail, options, needs_choice, first_seen, "
        "       snoozed_until "
        "FROM system_findings WHERE status IN ('open', 'snoozed') "
        "AND (project_id IS NULL OR project_id = :p) ORDER BY first_seen, id"),
        {"p": project_id}).mappings().all()
    rank = {"critical": 0, "warn": 1, "warning": 1, "info": 2}
    out = []
    for f in rows:
        snoozed = _aware(f["snoozed_until"])
        if f["status"] == "snoozed" and snoozed is not None and snoozed > now:
            continue
        options = f["options"] or []
        if isinstance(options, str):
            options = json.loads(options)
        fid = str(f["id"])
        out.append({
            "id": f"system:{fid}", "kind": "system_health", "finding_id": fid,
            "finding_kind": f["kind"], "severity": f["severity"] or "warn",
            "title": f["title"], "detail": f["detail"] or "", "what_to_do": None,
            "answers": [{"key": o["key"], "label": o["label"], "enabled": True,
                         "reason": o.get("tip"), "confirm": bool(o.get("confirm"))}
                        for o in options],
            "actions": [o["key"] for o in options],
            "needs_choice": bool(f["needs_choice"]),
            "run_id": None, "at": _iso(_aware(f["first_seen"])),
        })
    out.sort(key=lambda i: (rank.get(i["severity"], 3), i["at"] or ""))
    return out


#: run_retrospective.host_advice's cause → the System line (title ≤ 6 words, tip ≤ 14).
_ADVICE = {
    "memory": ("Low on memory", "Close what you are not using, or give it more memory"),
    "disk": ("Disk nearly full", "Free disk space before the next run"),
    "cpu": ("Machine is overloaded", "Run fewer builds at once, or close other work"),
    "boss_process": ("BOSS was slow", "The machine has room; the API logs show what held it"),
}
_ADVICE_DEFAULT = ("Machine is struggling", "Close what you are not using on this machine")
_ADVICE_CACHE: dict[str, tuple[float, Optional[dict[str, Any]]]] = {}
ADVICE_TTL_S = 60.0


def _advice(connection: Any, project_id: str) -> Optional[dict[str, Any]]:
    """The HostAdviceBanner line. host_advice walks the preview trees for disk, so its answer
    is reused for a minute rather than recomputed on every 10 s poll."""
    from .run_retrospective import host_advice

    hit = _ADVICE_CACHE.get(project_id)
    if hit and time.monotonic() - hit[0] < ADVICE_TTL_S:
        return hit[1]
    got = host_advice(connection, project_id)
    line = None
    if got.get("show"):
        title, tip = _ADVICE.get(str(got.get("cause") or ""), _ADVICE_DEFAULT)
        if got.get("severity") == "critical" and got.get("cause") == "memory":
            title = "Out of memory"
        line = {"title": title, "tip": tip, "href": None,
                "severity": got.get("severity") or "warning"}
    _ADVICE_CACHE[project_id] = (time.monotonic(), line)
    return line


def system(connection: Any, settings: Any, project_id: str) -> System:
    """BOSS's own machine, its queue, its stalls and the doctor's findings. Polled every 10 s:
    /proc reads, statvfs and small indexed queries only. A reader that fails is `partial`."""
    from . import admission, system_doctor
    from .self_observability import _build_stamp, in_flight

    partial: list[str] = []

    def guarded(name: str, read: Any, default: Any) -> Any:
        try:
            with connection.begin_nested():
                return read()
        except Exception:  # noqa: BLE001
            log.exception("obs_view.system: %s failed", name)
            partial.append(name)
            return default

    machine: dict[str, Any] = {}
    state, reason, oom_new = "ok", None, None
    adm: dict[str, Any] = {}
    try:
        cap = admission.sample()
        adm = guarded("admission", lambda: admission.snapshot(connection, cap=cap), {})
        machine = health_body(cap, adm)
        p = _pressure(cap)
        machine["bands"] = _bands(p, cap)
        state = str(p.get("state") or "ok")
        reason = (p.get("reasons") or [None])[0]
        oom_new = p.get("oom_kills_new")
    except Exception:  # noqa: BLE001
        log.exception("obs_view.system: machine failed")
        partial.append("machine")

    stamp = _build_stamp()
    sha = stamp.get("source_sha")
    q = guarded("queue", lambda: _queue(connection, project_id),
                {"queue": 0, "running": 0, "leased": 0})
    flying = guarded("in_flight", lambda: sum(
        1 for i in in_flight(connection, project_id, limit=25) if i.get("active")), 0)
    w = window_bounds("24h", 96)
    body: System = {
        "state": state if state in ("ok", "warning", "critical") else "ok",
        "reason": reason, "machine": machine,
        "build": {"sha7": sha[:7] if sha else None, "built_at": stamp.get("built_at")},
        "oom_kills_new": oom_new,
        "work": {**q, "in_flight": flying,
                 "builds_at_once": {"label": adm.get("label"),
                                    "value": (adm.get("builds_at_once") or {}).get("value")}},
        "stalls": guarded("stalls", lambda: stalls(connection, project_id, since=w["start"],
                                                   cells=96),
                          {"strip": [0] * 96, "rows": [], "total": 0}),
        "findings": guarded("findings", lambda: _findings(connection, project_id), []),
        "actions": guarded("actions", lambda: [
            {"at": a.get("created_at"), "action": a.get("action"), "target": a.get("target"),
             "outcome": a.get("outcome"), "automatic": bool(a.get("automatic")),
             "improved": a.get("improved")}
            for a in system_doctor.recent_actions(connection, limit=8)], []),
        "advice": guarded("advice", lambda: _advice(connection, project_id), None),
        "partial": partial,
    }
    return _sealed(body)  # type: ignore[return-value]


# ── Sources (§4.13) ─────────────────────────────────────────────────────────────────────────

#: project → the last `Check now` probe of the stack {at, checked_at, status, dashboards}.
#: Only `fresh=True` writes it: a poll never reaches the host.
_STACK_PROBES: dict[str, dict[str, Any]] = {}
#: project → (monotonic at, composed body) — the non-fresh body, reused for SOURCES_TTL_S.
_SOURCES_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}

_GAP_LABELS = {"no_metrics_backend": "No metrics backend", "no_logs_backend": "No log backend",
               "empty_observability": "Nothing watched yet"}


def forget_sources(project_id: Optional[str] = None) -> None:
    """Drop the cached Sources body (after a stack deploy, so the next poll shows it)."""
    if project_id is None:
        _SOURCES_CACHE.clear()
    else:
        _SOURCES_CACHE.pop(project_id, None)


def _stack_host(connection: Any, project_id: str) -> Optional[dict[str, Any]]:
    """The target the stack borrows a host and credential from: the first active one (the
    console's rule since the stack panel existed)."""
    row = connection.execute(text(
        "SELECT t.id, t.target_ref, t.host_id, h.name AS host_name "
        "FROM deployment_targets t LEFT JOIN deployment_hosts h ON h.id = t.host_id "
        "WHERE t.project_id = :p AND t.status = 'active' ORDER BY t.created_at, t.id LIMIT 1"),
        {"p": project_id}).mappings().first()
    return dict(row) if row else None


def _probe_stack(connection: Any, settings: Any, project_id: str, host: dict[str, Any],
                 base: Optional[str]) -> dict[str, Any]:
    """`Check now`: ask the host (SSH, through observability_stack) and Grafana. Only ever
    reached with fresh=True."""
    from .observability_stack import provisioned_dashboards, stack_status

    status = stack_status(connection.engine, settings, host_target_id=host["id"])
    dashboards = provisioned_dashboards(base) if base else None
    probe = {"at": time.monotonic(), "checked_at": _iso(_now()), "status": status,
             "dashboards": dashboards}
    _STACK_PROBES[project_id] = probe
    return probe


def sources(connection: Any, settings: Any, project_id: str, *, fresh: bool = False) -> Sources:
    hit = _SOURCES_CACHE.get(project_id)
    if not fresh and hit and time.monotonic() - hit[0] < SOURCES_TTL_S:
        return hit[1]  # type: ignore[return-value]
    from .observability_stack import scrape_targets
    from .observability_suite import suite_status

    partial: list[str] = []
    suite: dict[str, Any] = {}
    try:
        with connection.begin_nested():
            suite = suite_status(connection, project_id)   # no host id: never probes
    except Exception:  # noqa: BLE001
        log.exception("obs_view.sources: suite status failed")
        partial.append("sources")
    base = _grafana_base(connection, project_id)
    host = _stack_host(connection, project_id)
    deployed = bool((suite.get("stack") or {}).get("deployed"))

    probe = _STACK_PROBES.get(project_id)
    if fresh and host:
        try:
            probe = _probe_stack(connection, settings, project_id, host, base)
        except Exception as exc:  # noqa: BLE001
            probe = {"at": time.monotonic(), "checked_at": _iso(_now()),
                     "status": {"running": False, "error": f"{type(exc).__name__}: {exc}"[:200]},
                     "dashboards": None}
            _STACK_PROBES[project_id] = probe
    status = (probe or {}).get("status") or {}
    if probe and status.get("running"):
        state = "running"
    elif probe and status.get("running") is False and deployed:
        state = "not_running"
    elif deployed:
        state = "unknown"
    else:
        state = "not_deployed"
    from .infra_view import word
    sw = word("stack", state)
    if host and host.get("host_id"):
        host_ref = {"id": host["host_id"], "name": host["host_name"] or host["host_id"]}
    elif host:
        host_ref = {"id": host["id"], "name": str(host["target_ref"] or "").split("@")[-1]
                    .split(":")[0] or host["id"]}
    else:
        host_ref = None

    health_keys = {(t["app_name"], t["environment"]) for t in _targets(connection, project_id)}
    scraped = []
    try:
        with connection.begin_nested():
            for s in scrape_targets(connection, project_id):
                scraped.append({"app": s["app"], "env": s["environment"],
                                "scrape_url": f"{s['scrape_url']}/metrics",
                                "dashboard_url": _dashboard_url(base, s["app"], project_id),
                                "watched": (s["app"], s["environment"]) in health_keys})
    except Exception:  # noqa: BLE001
        partial.append("scraped")
    srcs = [{"id": s.get("id"), "kind": s.get("kind"), "name": s.get("name"),
             "url": s.get("base_url"), "enabled": bool(s.get("enabled"))}
            for s in ((suite.get("connectors") or {}).get("registered") or [])]
    gaps = [{"id": g["id"], "label": _GAP_LABELS.get(g["id"], g["id"]),
             "tip": str(g.get("message") or "")} for g in (suite.get("gaps") or [])]
    body: Sources = {
        "stack": {"state": state, "word": sw["word"], "tone": sw["tone"], "host": host_ref,
                  "host_target_id": host["id"] if host else None,
                  "grafana_url": base,
                  "containers": [{"name": str(c.get("name") or ""),
                                  "status": str(c.get("status") or "")}
                                 for c in (status.get("containers") or [])],
                  "missing": list(status.get("missing") or []),
                  "checked_at": (probe or {}).get("checked_at"), "error": status.get("error"),
                  "can_deploy": host is not None, "needs_machine": host is None},
        "scraped": scraped, "sources": srcs, "gaps": gaps,
        "counts": {"sources": len(srcs), "enabled": sum(1 for s in srcs if s["enabled"]),
                   "scraped": len(scraped),
                   "not_watched": sum(1 for s in scraped if not s["watched"]),
                   "gaps": len(gaps)},
        "partial": partial,
    }
    sealed = _sealed(body)  # type: ignore[arg-type]
    _SOURCES_CACHE[project_id] = (time.monotonic(), sealed)
    return sealed  # type: ignore[return-value]
