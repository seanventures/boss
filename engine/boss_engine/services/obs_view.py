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
from typing import Any, Literal, Optional, TypedDict

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


# ── the builders (stream S2 implements; signatures frozen) ──────────────────────────────────


def pulse(connection: Any, settings: Any, project_id: str, *, win: str = "24h",
          cells: Optional[int] = None) -> Pulse:
    raise NotImplementedError("S2")


def app_detail(connection: Any, settings: Any, project_id: str, target_id: str, *,
               win: str = "24h") -> AppDetail:
    raise NotImplementedError("S2")


def models(connection: Any, settings: Any, project_id: str, *, win: str = "24h",
           group: str = "model", metric: str = "tokens") -> Models:
    raise NotImplementedError("S2")


def stalls(connection: Any, project_id: str, *, since: Any, cells: int = 96) -> dict[str, Any]:
    raise NotImplementedError("S2")


def system(connection: Any, settings: Any, project_id: str) -> System:
    raise NotImplementedError("S2")


def sources(connection: Any, settings: Any, project_id: str, *, fresh: bool = False) -> Sources:
    raise NotImplementedError("S2")
