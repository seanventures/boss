"""Infra's one view of where things run (infra-observability spec §4.1-§4.8, §6.1-§6.5).

THE CONTRACT, frozen in Phase 0. apps/web/lib/infra-view.ts parses exactly these shapes; a field
changed here is changed under Fleet, Deploys, Previews, Cloud and the MCP host/deploy readers at
once. Change it with the lead, never inside a stream.

Pure derivations live here so every reader says the same thing: one word per raw state
(`word`), one setup stage per host (`host_stage`), one failure line per deploy
(`deploy_failure_line`), one attention list (`attention`). The web maps none of these.

NEVER A GUESS: no CPU or memory for a deploy host (BOSS does not measure them;
test_e2e_infra_ui_v1 guards it), no percent of progress, no ETA. Every number is a count, sum,
ratio or percentile of stored rows. No view function ever imports the SSH adapter: host checks
run only on an explicit `Re-verify`, never on a poll (spec §6.8).
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Literal, Optional, TypedDict

log = logging.getLogger(__name__)

# ── one state language (§4.2) ───────────────────────────────────────────────────────────────

Tone = Literal["green", "amber", "red", "accent", "grey", "hollow", "none"]
WordKind = Literal["health", "host", "deploy", "preview", "failure_kind", "band", "stack"]


class Word(TypedDict):
    word: str
    tone: Tone


#: (kind, raw) → word + dot tone. `host` setting-up is rendered by `host_word` (it carries n/5).
WORDS: dict[str, dict[str, Word]] = {
    "health": {
        "up": {"word": "Up", "tone": "green"},
        "degraded": {"word": "Slow", "tone": "amber"},
        "down": {"word": "Down", "tone": "red"},
        "unknown": {"word": "No signal", "tone": "hollow"},
        "not_deployed": {"word": "Not deployed", "tone": "grey"},
    },
    "host": {
        "registered": {"word": "Setting up", "tone": "accent"},
        "verified": {"word": "Ready", "tone": "green"},
        "degraded": {"word": "Unreachable", "tone": "red"},
        "unreachable": {"word": "Unreachable", "tone": "red"},
        "retired": {"word": "Retired", "tone": "none"},
    },
    "deploy": {
        "planned": {"word": "Planned", "tone": "hollow"},
        "created": {"word": "Planned", "tone": "hollow"},
        "pending": {"word": "Planned", "tone": "hollow"},
        "awaiting_approval": {"word": "Needs you", "tone": "amber"},
        # A governed host deploy approved and queued for its worker (governed_host_deploy.py).
        "approved": {"word": "Starting", "tone": "accent"},
        "running": {"word": "Deploying", "tone": "accent"},
        "applying": {"word": "Deploying", "tone": "accent"},
        "deploying": {"word": "Deploying", "tone": "accent"},
        "building": {"word": "Deploying", "tone": "accent"},
        # `succeeded`/`healthy` say Live for the newest of a target and Done for older ones;
        # `deploy_word(raw, newest=...)` decides. This entry is the newest case.
        "succeeded": {"word": "Live", "tone": "green"},
        "healthy": {"word": "Live", "tone": "green"},
        "completed": {"word": "Live", "tone": "green"},
        # A registry run whose provider effect completed through its ChangeSet.
        "deployed": {"word": "Live", "tone": "green"},
        "failed": {"word": "Failed", "tone": "red"},
        "rolled_back": {"word": "Rolled back", "tone": "grey"},
        "rejected": {"word": "Rejected", "tone": "grey"},
        "cancelled": {"word": "Cancelled", "tone": "grey"},
        # Refused by an environment policy before anything ran (delivery_pipeline).
        "blocked": {"word": "Blocked", "tone": "red"},
        # A request a newer one for the same target replaced, or one whose decision another
        # run took (deploy_status): nothing ran, and nothing will.
        "superseded": {"word": "Superseded", "tone": "grey"},
    },
    "preview": {
        "starting": {"word": "Starting", "tone": "accent"},
        "running": {"word": "Live", "tone": "green"},
        "failed": {"word": "Failed", "tone": "red"},
        "stopped": {"word": "Stopped", "tone": "grey"},
        "expired": {"word": "Expired", "tone": "grey"},
    },
    "failure_kind": {
        "clone_failed": {"word": "Clone failed", "tone": "red"},
        "command_missing": {"word": "No start command", "tone": "red"},
        "boot_crash": {"word": "Crashed on boot", "tone": "red"},
        "port_timeout": {"word": "Port never opened", "tone": "red"},
        "no_page_at_root": {"word": "No page at /", "tone": "red"},
    },
    "band": {
        "ok": {"word": "OK", "tone": "green"},
        "warn": {"word": "High", "tone": "amber"},
        "warning": {"word": "High", "tone": "amber"},
        "critical": {"word": "Critical", "tone": "red"},
    },
    "stack": {
        "running": {"word": "Running", "tone": "green"},
        "stopped": {"word": "Not running", "tone": "grey"},
        "not_running": {"word": "Not running", "tone": "grey"},
        "not_deployed": {"word": "Not deployed", "tone": "grey"},
        "unknown": {"word": "Status unknown", "tone": "hollow"},
    },
}

UNKNOWN_WORD: Word = {"word": "Unknown", "tone": "hollow"}


def word(kind: str, raw: Optional[str]) -> Word:
    """The one word (and dot tone) for a raw state. An unknown raw is `Unknown`, logged once per
    call so a new engine state is noticed rather than silently drawn as something else."""
    table = WORDS.get(kind) or {}
    hit = table.get(str(raw or "").strip().lower())
    if hit is None:
        log.warning("infra_view.word: no word for %s=%r", kind, raw)
        return dict(UNKNOWN_WORD)  # type: ignore[return-value]
    return dict(hit)  # type: ignore[return-value]


def health_checks(raw: Any) -> list[dict[str, Any]]:
    """The recorded health checks as a list. Deploys record either a list of checks or
    `{"healthy": bool, "checks": [...]}`; read as a list, the second form was its keys, so the
    drawer said "Not checked yet" over a check that had passed."""
    value = _json(raw) if isinstance(raw, str) else raw
    if isinstance(value, dict):
        value = value.get("checks") or []
    return [c for c in (value or []) if isinstance(c, dict)]


def _check_ok(check: dict[str, Any]) -> bool:
    return check.get("ok") is True or check.get("passed") is True or check.get("status") == "passed"


def health_line(raw: Any) -> Optional[str]:
    """`GET /readyz → 200` for each recorded check: what was actually asked, so a check that
    only fetched `/` reads as exactly that."""
    from urllib.parse import urlsplit

    parts = []
    for c in health_checks(raw)[:3]:
        url = str(c.get("url") or c.get("path") or "")
        path = (urlsplit(url).path or "/") if "://" in url else (url or "/")
        got = c.get("status")
        parts.append(f"GET {path} → {got if got is not None else '?'}"
                     + ("" if _check_ok(c) else f" (expected {c.get('expected', 200)})"))
    return " · ".join(parts) or None


_PHASE_OF = {"awaiting_approval": "needs_approval", "planned": "needs_approval",
             "created": "needs_approval", "pending": "needs_approval",
             "approved": "queued", "running": "deploying", "applying": "deploying",
             "deploying": "deploying", "building": "deploying"}


def deploy_status(raw_status: Optional[str], *, decision_status: Optional[str] = None,
                  steps: Any = None, health: Any = None, url: Optional[str] = None,
                  failure: Optional[str] = None, newest_for_target: bool = True,
                  success_means_healthy: bool = False) -> dict[str, Any]:
    """THE ONE READING OF A DEPLOY'S STATE: phase, word, tone, a sentence, the step running now,
    and the app's address when (and only when) the deploy went live and passed its health check.

    Every surface that shows a deploy (Infra rows and drawer, the workflow run's Deploy step,
    the fleet tile) draws this, so they cannot disagree. They did: a run that had shipped read
    "Nothing has deployed. ChangeSet … is ready" on the workflow, and Infra said "Needs you" for
    a request whose decision another run had already taken.
    """
    status = str(raw_status or "").strip().lower()
    # A request still `awaiting_approval` whose decision is no longer open can never move:
    # another run took the decision, or it was answered elsewhere. It is not waiting on you.
    if status == "awaiting_approval" and decision_status and decision_status != "pending":
        status = "superseded"
    w = deploy_word(status, newest_for_target=newest_for_target)
    listed = steps if isinstance(steps, list) else (_json(steps) or [])
    now = next((_step_title(x) for x in listed
                if isinstance(x, dict) and step_state(x.get("status")) == "running"), None)
    checks = health_checks(health)
    checked = health_line(health)
    healthy = bool(checks) and all(_check_ok(c) for c in checks)
    address = _app_url(url)
    phase = _PHASE_OF.get(status) or ("live" if status in SUCCESS_STATES else
                                      "failed" if status in FAILED_STATES else status or "unknown")
    sentence = {
        "needs_approval": "Waiting for approval. Nothing has run on the host yet.",
        "queued": "Approved. Starting the deploy.",
        "deploying": f"Deploying{f': {now}' if now else ''}.",
        "superseded": "Replaced by another deploy request. Nothing ran.",
        "rejected": "Not approved. Nothing was deployed.",
        "cancelled": "Cancelled. Nothing was deployed.",
        "rolled_back": ("The health check failed after release, so the previous release was "
                        "restored." + (f" Checked: {checked}." if checked else "")),
    }.get(phase)
    if phase == "live" and success_means_healthy and not checks:
        # A stack deployment reaches `succeeded` only after its manifest's health checks pass
        # (stack_deploy.py), so its status IS the health verdict.
        healthy = True
    if phase == "live":
        where = address or "its host"
        sentence = (f"Live at {where}. Health check passed"
                    + (f": {checked}." if checked else ".") if healthy else
                    f"Deployed to {where}, but its health check did not pass"
                    + (f": {checked}." if checked else " (no result was recorded)."))
    elif phase == "failed":
        first = (str(failure or "").strip().splitlines() or [""])[0][:200]
        sentence = first or ("Blocked before it ran." if status == "blocked" else "Failed.")
    return {"phase": phase, "word": w["word"], "tone": w["tone"],
            "sentence": sentence or f"{w['word']}.", "now": now,
            # Offered only for a deploy that went live AND passed the health check it recorded:
            # a link to a page that does not work is the same lie in a new place.
            "open_url": address if phase == "live" and healthy else None,
            "health": checked, "healthy": healthy if (checks or phase == "live") else None}


def deploy_word(raw: Optional[str], *, newest_for_target: bool) -> Word:
    """`succeeded` reads Live for the newest deploy of a target and Done for older ones."""
    w = word("deploy", raw)
    if w["word"] == "Live" and not newest_for_target:
        return {"word": "Done", "tone": "green"}
    return w


# ── host setup stages (§4.3, ported from delivery-manage.tsx hostStage) ─────────────────────

StageKey = Literal["credential", "install", "verify", "provision", "ready"]
STAGE_ORDER: tuple[str, ...] = ("credential", "install", "verify", "provision", "ready")
STAGE_LABELS: dict[str, str] = {
    "credential": "Credential", "install": "Authorise", "verify": "Verify",
    "provision": "Provision", "ready": "Ready",
}


class HostStage(TypedDict):
    stage: StageKey
    index: int          # 1-based position in STAGE_ORDER (Ready = 5)
    label: str


def host_stage(host: dict[str, Any]) -> HostStage:
    """Where a host is in its setup. Same rule as the console's old `hostStage()` so the web,
    the MCP readers and the rack agree: verified is Ready; a lost credential is back to step
    one; a minted key not yet on the box is Authorise; unreachable is Verify; docker problems
    are Provision."""
    import re

    pf = host.get("last_preflight") or {}
    problems = [str(p) for p in (pf.get("problems") or [])]
    if str(host.get("status") or "") == "verified":
        key = "ready"
    elif (not host.get("key_secret_ref")
          or any(re.search(r"bound but unusable|does not exist", p, re.I) for p in problems)):
        key = "credential"
    elif host.get("install_command") and not pf.get("reachable"):
        key = "install"
    elif not pf.get("reachable"):
        key = "verify"
    elif any(re.search(r"docker", p, re.I) for p in problems):
        key = "provision"
    else:
        key = "verify"
    return {"stage": key, "index": STAGE_ORDER.index(key) + 1, "label": STAGE_LABELS[key]}  # type: ignore[typeddict-item]


def host_word(host: dict[str, Any]) -> Word:
    """`Setting up · n/5` while registered, `Unreachable` when degraded or the last preflight
    could not reach it, else the plain host word."""
    status = str(host.get("status") or "registered")
    pf = host.get("last_preflight") or {}
    if status == "retired":
        return word("host", "retired")
    if status == "degraded" or (status == "verified" and pf and pf.get("reachable") is False):
        return word("host", "degraded")
    if status == "verified":
        return word("host", "verified")
    st = host_stage(host)
    return {"word": f"Setting up · {st['index']}/5", "tone": "accent"}


# ── Fleet (§4.3, §6.2 GET /v1/infra/fleet) ──────────────────────────────────────────────────

ResidentKind = Literal["target", "preview", "monitoring"]
#: One mini strip cell: [ok, n] — ok of n probes in a 45-minute bucket; n == 0 is a gap.
StripCell = list  # [int, int]


class FailedAfter(TypedDict):
    at: str               # when the newer attempt failed
    ref: str              # "<kind>:<id>" → its deploy drawer


class LastDeploy(TypedDict, total=False):
    sha7: str
    at: str
    word: str
    ref: str              # "<kind>:<id>" → deploy drawer
    # LIVE IS WHAT IS RUNNING; A NEWER FAILURE IS SAID BESIDE IT (operator decision 2026-10-04).
    # The newest attempt on the target after this live deploy, when it failed; None otherwise.
    failed_after: Optional[FailedAfter]
    # It passed its health check (deploy_status's rule) — what earns the tile its Open app.
    healthy: bool


class Resident(TypedDict, total=False):
    kind: ResidentKind
    id: str               # target id | preview id | "monitoring"
    name: str             # app name | "Preview <app>" | "Monitoring"
    env: Optional[str]
    health: Word          # target: health word; preview: preview word; monitoring: stack word
    strip: list[StripCell]  # target only: 32 cells × 45 min = 24h, oldest first
    last_deploy: Optional[LastDeploy]   # target only; None → "Never deployed"
    expires_at: Optional[str]           # preview only
    url: Optional[str]                  # preview open url | Grafana url
    open_url: Optional[str]             # target: the app, once its newest deploy is live healthy
    port: Optional[int]


class Rack(TypedDict, total=False):
    id: str               # host id, or "this" when drawn from hardware_places alone
    this_computer: bool
    name: str
    address: Optional[str]
    arch: Optional[str]
    state: Word           # host_word()
    stage: HostStage      # host_stage(); index 5 when Ready
    checked_at: Optional[str]      # last_preflight_at
    max_apps: Optional[int]
    apps: int             # targets placed here
    ports_used: int       # ports held by its targets and live previews
    ports_total: Optional[int]     # port_max - port_min + 1
    residents: list[Resident]
    # Inline action for a setting-up rack's current stage (same buttons as the host drawer).
    stage_action: Optional[str]    # "generate_key" | "verify" | "install_docker" | "copy_command" | None
    retired: bool         # a retired machine: drawn only when the console shows retired


class Preconditions(TypedDict):
    outward_ssh_allowed: bool
    host_writes_allowed: bool
    blocker: Optional[str]
    sandbox_deploys_enabled: bool
    community: bool
    can_write: bool       # the viewer may change infra (else every write control is hidden)


class ThisComputer(TypedDict, total=False):
    hostname: Optional[str]
    arch: Optional[str]
    os: Optional[str]
    agent_state: Optional[str]
    host_id: Optional[str]          # the deployment_hosts row it matches, when one does
    place_id: Optional[str]


class FleetCounts(TypedDict):
    ready: int
    setting_up: int
    unreachable: int
    apps: int
    previews_live: int
    needs_you: int


class Fleet(TypedDict, total=False):
    counts: FleetCounts
    preconditions: Preconditions
    attention: list[dict[str, Any]]   # AttentionItem wire (agents_live_board shape), area=infra
    this_computer: Optional[ThisComputer]
    racks: list[Rack]                 # ordered: this computer, Unreachable, Setting up, Ready; name
    unplaced: list[Resident]          # targets with no host_id (registry/cloud)
    retired_count: int
    partial: list[str]                # names of feeds that failed ("hosts", "health", ...)
    generated_at: str
    etag: str


# ── Host and target drawers (§4.4, §4.5) ────────────────────────────────────────────────────


class Finding(TypedDict, total=False):
    kind: str
    label: str            # findingLabel() text
    ok: bool
    detail: Optional[str]
    remedy: Optional[str]


class HostDetail(TypedDict, total=False):
    id: str
    this_computer: bool
    name: str
    state: Word
    stage: HostStage
    facts: dict[str, Any]   # address, user, port, arch, labels, port_min, port_max, max_apps,
                            # key_fingerprint; this computer: hostname, arch, os, agent_state
    key_public: Optional[str]
    authorise_line: Optional[str]   # the authorized_keys line
    install_command: Optional[str]
    route_options: list[dict[str, str]]   # [{key, label, command}] for `Reach it by`
    findings: list[Finding]
    checked_at: Optional[str]
    residents: list[Resident]
    can: dict[str, bool]    # adopt_key, generate_key, verify, install_key, install_docker, retire, correct
    etag: str


class TargetDetail(TypedDict, total=False):
    id: str
    app: str
    env: str
    kind: str               # target_kind: stack | app | registry kinds
    health: Word
    since: Optional[str]
    toggles: dict[str, bool]          # auto_deploy, paused
    where: dict[str, Any]             # host{id,name}, repo, ref, manifest, domain, public_url, account{id,name}
    strip: list[StripCell]            # 32 cells
    uptime_24h: Optional[float]       # ratio 0..1 of ok samples (a measurement, not progress)
    recent: list["DeployRow"]         # 5 newest
    secrets: list[dict[str, Any]]     # [{name, kind, status}]
    can: dict[str, bool]              # deploy, auto_deploy, move, retire, check
    disabled: dict[str, str]          # control → tooltip (e.g. "Paused in Community")
    etag: str


# ── Deploys (§4.6, §6.3) ────────────────────────────────────────────────────────────────────

DeployKind = Literal["stack", "registry", "delivery"]
StepState = Literal["done", "running", "failed", "pending", "skipped"]


class DeployRow(TypedDict, total=False):
    """≤ 2 KB. Never carries manifest, log_tail or preflight (the list budget)."""
    ref: str                          # "<kind>:<id>"
    kind: DeployKind
    target_id: Optional[str]
    app: str
    env: str
    host: Optional[dict[str, str]]    # {id, name} | None (an account name goes in `account`)
    account: Optional[str]
    sha7: Optional[str]
    ref_name: Optional[str]
    trigger: Literal["manual", "merge", "workflow", "rollback", "redeploy"]
    rollback_of_sha7: Optional[str]
    approver: Optional[dict[str, str]]   # {"role_key": k} | {"actor": "you"} | {"actor": name}
    state: str                        # raw status
    word: str
    tone: Tone
    steps: list[dict[str, str]]       # [{"s": StepState, "t": title ≤ 40 chars}] ≤ 24
    started_at: Optional[str]
    finished_at: Optional[str]
    duration_ms: Optional[int]
    can: dict[str, bool]              # approve, reject, rollback, redeploy
    # ADDED by stream S3 (additive, optional): the commit `Roll back` would redeploy — the last
    # one that went live on this target before this deploy — so the drawer can say
    # `Roll back to <sha7>` (§5.1). None when can.rollback is false.
    rollback_to_sha7: Optional[str]
    # On the Live row of a target: a newer attempt that failed (`Last attempt failed · 2h ago`).
    failed_after: Optional[FailedAfter]
    decision_id: Optional[str]
    run_id: Optional[str]             # the build that made it → Build ↗
    # deploy_status: needs_approval · queued · deploying · live · failed · superseded …, the
    # one sentence every surface shows, the step running now, and the app's address on the
    # target's Live row when its health check passed.
    phase: str
    sentence: str
    now: Optional[str]
    open_url: Optional[str]


class DeployList(TypedDict, total=False):
    counts: dict[str, int]            # needs_you, live, failed, all
    rows: list[DeployRow]
    next_cursor: Optional[str]
    filters: dict[str, list[str]]     # apps, envs present
    attention: list[dict[str, Any]]
    partial: list[str]
    etag: str


class DeployDetail(TypedDict, total=False):
    row: DeployRow
    steps: list[dict[str, Any]]       # [{title, state, duration_ms}]
    waiting_on: Optional[str]
    failure: Optional[dict[str, str]]  # {line ≤ 6 words, error raw}
    plan: Optional[dict[str, Any]]     # {steps[], env_keys[], health{path, expect}}
    preflight: list[Finding]
    log_tail: Optional[str]            # ≤ 64 KB, last lines
    health_check_results: list[dict[str, Any]]
    promotion: Optional[dict[str, Any]]  # {phases:[{key,label,state}], id}
    open_url: Optional[str]
    changeset_id: Optional[str]
    live: bool                         # poll every 3 s while true
    etag: str


# ── Previews (§4.7) ─────────────────────────────────────────────────────────────────────────


class PreviewRow(TypedDict, total=False):
    """≤ 1 KB."""
    id: str
    run_id: Optional[str]
    app: str
    branch: Optional[str]
    state: str
    word: str                          # `Failed · Port never opened` for a failed one
    tone: Tone
    substrate: Literal["Process", "Docker", "Rootfs", "microVM"]
    port: Optional[int]
    host: Optional[dict[str, str]]     # {id, name}; the BOSS machine unless host_ref says otherwise
    url: Optional[str]
    created_at: Optional[str]
    expires_at: Optional[str]
    last_seen_at: Optional[str]
    can: dict[str, bool]               # open, log, stop, restart
    restart_why: Optional[str]         # why Restart cannot run (its build is gone), in words


class PreviewList(TypedDict, total=False):
    counts: dict[str, int]             # live, failed_24h, ended_24h
    live: list[PreviewRow]
    failed_24h: list[PreviewRow]
    ended_24h: list[PreviewRow]
    partial: list[str]
    etag: str


# ── Cloud (§4.8) ────────────────────────────────────────────────────────────────────────────

CloudView = Literal["accounts", "resources", "capacity", "spend", "changes"]


class CloudBody(TypedDict, total=False):
    view: CloudView
    counts: dict[str, int]             # accounts, resources, pools, providers, changes, anomalies
    rows: list[dict[str, Any]]         # per view, each ≤ 2 KB; money in micros AND a `usd` string
    filters: dict[str, list[str]]
    header: dict[str, Any]             # capacity: queue{depth,leased,running,real_provision}; api_health[]
    notices: list[dict[str, str]]      # [{key: read_only|byok, tip}]
    partial: list[str]
    etag: str


# ── Attention (§2.2) ────────────────────────────────────────────────────────────────────────
# Items use the AttentionItem wire of agents_live_board (id, kind, title, detail, role_key,
# answers[{key,label,enabled,reason,confirm}], actions, at, severity, fix{area,label,focus}).
# deploy approvals are kind "approval" (the decision itself, decided through the decision
# routes as everywhere else). The rest are kind "infra": its answers are pressed through
# POST /v1/infra/attention/act {id, option, actor}; option keys: rollback | reverify | open.
#   deploy_failed   id "infra:deploy_failed:<target_id>"   title "<app> deploy failed"
#   app_down        id "infra:app_down:<target_id>"        title "<app> is down"
#   host_unreachable id "infra:host_unreachable:<host_id>" title "<host> unreachable"
#   app_errors      id "infra:app_errors:<target_id>"      title "<app> requests failing"
#                   (the app's own /metrics error-rate alert has an open incident; app_metrics)
# `area` on each item says where it lives: "infra" | "observability".

INFRA_ATTENTION_KINDS = ("deploy_approval", "deploy_failed", "app_down", "host_unreachable",
                         "app_errors")


def etag_of(material: Any) -> str:
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode())
    return f'"inf-{digest.hexdigest()[:24]}"'




# ── the builders (stream S1) ────────────────────────────────────────────────────────────────
# Every builder reads stored rows only. A feed that fails becomes a name in `partial[]` (its
# query runs inside a SAVEPOINT so one broken table cannot poison the rest of the view).

import base64
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

#: The Fleet tile's mini strip: 32 cells of 45 minutes = 24 hours, oldest first.
STRIP_CELLS = 32
STRIP_CELL_S = 45 * 60
#: Deploy rows (§6.3) and their budget.
DEPLOY_PAGE = 30
ROW_BUDGET_BYTES = 2048
MAX_STEPS = 24
LOG_TAIL_BYTES = 64 * 1024
#: Deploy rail kinds, and the raw states each chip means.
SUCCESS_STATES = ("healthy", "succeeded", "deployed", "completed")
FAILED_STATES = ("failed", "blocked")
#: Deploys that ended or wait without ever running: no duration, no steps to time.
NEVER_RAN_STATES = ("rejected", "awaiting_approval", "approved", "planned", "created", "pending")
WAITING_STATES = ("awaiting_approval", "planned", "created", "pending")
ACTIVE_STATES = ("planned", "awaiting_approval", "approved", "running", "applying", "deploying",
                 "building", "created", "pending")
TERMINAL_RUN_STATES = ("succeeded", "failed", "rolled_back", "deployed", "blocked")
#: Target kinds that deploy through the per-app delivery connectors rather than a cloud account.
LOCAL_KINDS = frozenset({"local", "local_process", "local_runner", "ssh", "container"})
_PAID_EDITIONS = frozenset({"cloud", "enterprise", "enterprise_cloud", "enterprise_self_hosted"})
PAUSED_IN_COMMUNITY = "Paused in Community"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _aware(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value if isinstance(value, datetime) else None


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


_HEX = re.compile(r"^[0-9a-f]{7,40}$")


def sha7(value: Any) -> Optional[str]:
    """The short sha of a commit, or None when the ref is a branch name (never a fake sha)."""
    v = str(value or "").strip().lower()
    return v[:7] if _HEX.match(v) else None


def usd(micros: Any) -> str:
    """Dollars, 2 decimals, `<$0.01` below a cent. Never micros on screen."""
    m = int(micros or 0)
    if m == 0:
        return "$0.00"
    if 0 < abs(m) < 10_000:
        return "<$0.01"
    return f"{'-' if m < 0 else ''}${abs(m) / 1_000_000:,.2f}"


def community(settings: Any) -> bool:
    edition = str(getattr(settings, "boss_edition", "community") or "community").strip().lower()
    return not (edition in _PAID_EDITIONS or edition.startswith("enterprise"))


def _guard(connection: Any, partial: list[str], name: str, fn, default):
    """Run one feed inside a SAVEPOINT; a failure names the feed instead of raising."""
    try:
        with connection.begin_nested():
            return fn()
    except Exception:  # noqa: BLE001 — a broken feed is a partial view, never a 500
        log.exception("infra_view: feed %s failed", name)
        if name not in partial:
            partial.append(name)
        return default


def _rows(connection: Any, sql_: str, params: Optional[dict] = None) -> list[dict[str, Any]]:
    return [dict(r) for r in connection.execute(text(sql_), params or {}).mappings().all()]


def _finish(body: dict[str, Any]) -> dict[str, Any]:
    """Etag the body (without generated_at), then stamp generated_at."""
    body.pop("etag", None)
    body.pop("generated_at", None)
    body["etag"] = etag_of(body)
    body["generated_at"] = _now().isoformat()
    return body


def viewer_can_write(connection: Any, settings: Any, project_id: str,
                     viewer: Optional[str], permission: str = "providerops_manage", *,
                     catalogue_permission: str = "deployment:create") -> bool:
    """Whether the viewer holds `permission`. No viewer (API auth off, nobody named) → True:
    every write still binds its actor and checks the permission server-side.

    Under enforce: the PDP's answer for `catalogue_permission` (what the page's write routes
    declare) held by the request's verified principal (identity.viewer.may, R2 #5)."""
    from ..identity import viewer as _viewer

    decided = _viewer.may(project_id, catalogue_permission)
    if decided is not None:
        return decided
    if not viewer:
        return True
    try:
        row = connection.execute(
            text("SELECT permissions, status FROM project_members "
                 "WHERE project_id = :p AND member_id = :m"),
            {"p": project_id, "m": viewer}).mappings().first()
    except Exception:  # noqa: BLE001
        return False
    if not row or str(row["status"] or "active") != "active":
        return False
    return permission in (_json(row["permissions"]) or [])


# ── hosts ─────────────────────────────────────────────────────────────────────────────────────

def _host_ready(host: dict[str, Any]) -> dict[str, Any]:
    """A deployment_hosts row with its derived install command, as host_stage expects."""
    from .deployment_fleet import install_command_for

    h = dict(host)
    h["last_preflight"] = _json(h.get("last_preflight")) or {}
    h["labels"] = _json(h.get("labels")) or {}
    if "install_command" not in h:
        h["install_command"] = install_command_for(h)
    return h


def _host_rank(w: Word) -> int:
    word_ = w["word"]
    if word_ == "Unreachable":
        return 0
    if word_.startswith("Setting up"):
        return 1
    return 2


def _host_unreachable(host: dict[str, Any]) -> bool:
    return host_word(host)["word"] == "Unreachable"


def key_fingerprint(public_key: Optional[str]) -> Optional[str]:
    parts = str(public_key or "").split()
    if len(parts) < 2:
        return None
    try:
        blob = base64.b64decode(parts[1].encode(), validate=True)
    except (ValueError, TypeError):
        return None
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def authorize_line(public_key: Optional[str]) -> Optional[str]:
    """The exact line that authorises BOSS on the VM (delivery-manage.tsx `authorizeLine`)."""
    key = str(public_key or "").strip()
    if not key:
        return None
    return ("mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            f"printf '%s\\n' '{key}' >> ~/.ssh/authorized_keys && "
            "chmod 600 ~/.ssh/authorized_keys")


#: delivery-manage.tsx `findingLabel`, kept word for word.
FINDING_LABELS = {
    "credential_lost": "Key no longer in the store",
    "key_not_installed": "Key not on the host yet",
    "expired_root_password": "Password expired on the host",
    "outward_ssh_disabled": "SSH is switched off",
    "docker_missing": "Docker missing",
    "other": "Problem",
}


def finding_label(kind: str) -> str:
    return FINDING_LABELS.get(kind) or str(kind or "").replace("_", " ")


def _findings(preflight: dict[str, Any]) -> list[Finding]:
    from .deployment_fleet import _classify

    pf = preflight or {}
    raw = pf.get("findings")
    if not isinstance(raw, list) or (not raw and pf.get("problems")):
        raw = _classify([str(p) for p in (pf.get("problems") or [])])
    out: list[Finding] = []
    for f in raw:
        if not isinstance(f, dict):
            continue
        kind = str(f.get("kind") or "other")
        out.append({"kind": kind, "label": finding_label(kind), "ok": False,
                    "detail": f.get("detail"), "remedy": f.get("remedy") or None})
    if pf and pf.get("reachable") and not out:
        out.append({"kind": "reachable", "label": "Reachable", "ok": True,
                    "detail": None, "remedy": None})
    return out


def _stage_action(host: dict[str, Any], stage: HostStage, *, can_write: bool,
                  host_writes: bool) -> Optional[str]:
    if not can_write:
        return None
    key = stage["stage"]
    if key == "credential":
        return "generate_key"
    if key == "install":
        return "copy_command"
    if key == "verify":
        return "verify"
    if key == "provision":
        return "install_docker" if host_writes else "verify"
    return None


def preconditions(settings: Any, *, can_write: bool = True) -> Preconditions:
    from .deployment_fleet import direct_host_writes_allowed, outward_ssh_allowed

    outward = outward_ssh_allowed(settings)
    return {
        "outward_ssh_allowed": outward,
        "host_writes_allowed": direct_host_writes_allowed(settings),
        "blocker": None if outward else (
            "BOSS is configured not to reach out over SSH, so no host can be verified or "
            "deployed to. Remove BOSS_SSH_OUTWARD_ALLOWED=false and restart."),
        "sandbox_deploys_enabled": bool(getattr(settings, "enable_sandbox_deploy", False)),
        "community": community(settings),
        "can_write": bool(can_write),
    }


# ── health, strips, last deploys (batched: one query each, whatever the size) ────────────────

def strip_window(now: Optional[datetime] = None, *, cells: int = STRIP_CELLS,
                 cell_s: int = STRIP_CELL_S) -> tuple[datetime, datetime]:
    """[from, to) aligned to the cell size, so an unchanged table gives an unchanged strip (and
    an unchanged ETag) until a new cell starts."""
    now = now or _now()
    epoch = int(now.timestamp())
    to_s = (epoch // cell_s + 1) * cell_s
    to = datetime.fromtimestamp(to_s, tz=timezone.utc)
    return to - timedelta(seconds=cells * cell_s), to


def strips(connection: Any, target_ids: list[str], *, now: Optional[datetime] = None,
           cells: int = STRIP_CELLS, cell_s: int = STRIP_CELL_S) -> dict[str, list[list[int]]]:
    """target_id → [[ok, n] × cells], oldest first, from ONE grouped query. n == 0 is a gap."""
    out = {t: [[0, 0] for _ in range(cells)] for t in target_ids}
    if not target_ids:
        return out
    start, end = strip_window(now, cells=cells, cell_s=cell_s)
    for r in connection.execute(text(
            "SELECT deployment_target_id AS t, "
            "       floor(extract(epoch FROM (checked_at - :f)) / :c)::int AS b, "
            "       count(*) AS n, sum(CASE WHEN ok THEN 1 ELSE 0 END) AS ok "
            "FROM deployment_health_samples "
            "WHERE deployment_target_id = ANY(:ids) AND checked_at >= :f AND checked_at < :e "
            "GROUP BY 1, 2"),
            {"ids": list(target_ids), "f": start, "e": end, "c": cell_s}).mappings():
        b = int(r["b"])
        if r["t"] in out and 0 <= b < cells:
            out[r["t"]][b] = [int(r["ok"] or 0), int(r["n"] or 0)]
    return out


def deployed_targets(connection: Any, target_ids: list[str]) -> set[str]:
    """Targets with something running — deployment_health's one DEPLOYED rule (a succeeded or
    applied run, or a healthy stack deployment), shared with Observability and the sweep."""
    from .deployment_health import newest_deploys

    return set(newest_deploys(connection, list(target_ids)))


def health_states(connection: Any, target_ids: list[str], *,
                  now: Optional[datetime] = None) -> dict[str, dict[str, Any]]:
    """target_id → {status, since, word, tone} by deployment_health.verdict over each target's
    newest probes (one LATERAL query on the (target, checked_at) index)."""
    from .deployment_health import status_since, verdict

    now = now or _now()
    out: dict[str, dict[str, Any]] = {}
    if not target_ids:
        return out
    samples: dict[str, list[dict[str, Any]]] = {t: [] for t in target_ids}
    for r in connection.execute(text(
            "SELECT t.id AS t, s.checked_at, s.ok, s.latency_ms "
            "FROM unnest(CAST(:ids AS text[])) AS t(id) "
            "CROSS JOIN LATERAL ("
            "  SELECT checked_at, ok, latency_ms FROM deployment_health_samples "
            "  WHERE deployment_target_id = t.id AND checked_at >= :cut "
            "  ORDER BY checked_at DESC LIMIT 20) s "
            "ORDER BY t.id, s.checked_at DESC"),
            {"ids": list(target_ids), "cut": now - timedelta(days=7)}).mappings():
        samples[r["t"]].append({"checked_at": r["checked_at"], "ok": bool(r["ok"]),
                                "latency_ms": r["latency_ms"]})
    deployed = deployed_targets(connection, target_ids)
    verdicts = {t: (verdict(samples.get(t) or []) if t in deployed else "not_deployed")
                for t in target_ids}
    # WHEN IT BEGAN. The newest 20 probes say what the state is; when every one of them agrees,
    # the state began before them, so ask the 7-day history: up since the last failed probe,
    # down since the last good one (one grouped query, only for those targets).
    since: dict[str, Any] = {}
    deeper = []
    for t in target_ids:
        s = samples.get(t) or []
        if verdicts[t] in ("not_deployed", "unknown") or not s:
            continue
        began = status_since(s)
        if verdicts[t] in ("up", "down") and began == s[-1]["checked_at"] and len(s) >= 20:
            deeper.append(t)
        since[t] = began
    if deeper:
        for r in connection.execute(text(
                "SELECT deployment_target_id AS t, "
                "       max(checked_at) FILTER (WHERE NOT ok) AS last_bad, "
                "       max(checked_at) FILTER (WHERE ok) AS last_good, "
                "       min(checked_at) AS first "
                "FROM deployment_health_samples "
                "WHERE deployment_target_id = ANY(:ids) AND checked_at >= :cut GROUP BY 1"),
                {"ids": deeper, "cut": now - timedelta(days=7)}).mappings():
            edge = r["last_bad"] if verdicts[r["t"]] == "up" else r["last_good"]
            since[r["t"]] = edge or r["first"]
    for t in target_ids:
        status = verdicts[t]
        out[t] = {"status": status, "since": _iso(since.get(t)), **word("health", status)}
    return out


def failed_after(connection: Any, live: dict[str, tuple[str, Any]]) -> dict[str, FailedAfter]:
    """target_id → the newest attempt that FAILED after the target's live deploy (`live` maps
    target → (live ref, live at)). Only when that failure is also the target's newest attempt
    of any outcome: a failure followed by a newer success is history, not news."""
    if not live:
        return {}
    ids = list(live)
    out: dict[str, FailedAfter] = {}
    for r in connection.execute(text(
            "SELECT DISTINCT ON (t) t, kind, id, status, at FROM ("
            "  SELECT target_id AS t, 'stack' AS kind, id, status, "
            "         COALESCE(finished_at, updated_at, created_at) AS at "
            "  FROM stack_deployments WHERE target_id = ANY(:ids) "
            "    AND status IN ('healthy', 'failed') "
            "  UNION ALL "
            "  SELECT deployment_target_id, CASE WHEN trigger IS NULL THEN 'registry' ELSE 'delivery' END, "
            "         id, status, COALESCE(finished_at, started_at, created_at) "
            "  FROM deployment_runs WHERE deployment_target_id = ANY(:ids) "
            "    AND status IN ('succeeded', 'deployed', 'failed')) x "
            "ORDER BY t, at DESC"), {"ids": ids}).mappings():
        live_ref, live_at = live.get(r["t"], (None, None))
        if (r["status"] == "failed" and f"{r['kind']}:{r['id']}" != live_ref
                and live_at is not None and r["at"] is not None and r["at"] > live_at):
            out[r["t"]] = {"at": _iso(r["at"]), "ref": f"{r['kind']}:{r['id']}"}
    return out


def last_deploys(connection: Any, target_ids: list[str]) -> dict[str, LastDeploy]:
    """target_id → the newest successful deploy (stack healthy, or run succeeded)."""
    if not target_ids:
        return {}
    out: dict[str, LastDeploy] = {}
    for r in connection.execute(text(
            "SELECT DISTINCT ON (target_id) target_id, kind, id, sha, at, health FROM ("
            "  SELECT target_id, 'stack' AS kind, id, commit_sha AS sha, "
            "         COALESCE(finished_at, created_at) AS at, NULL::jsonb AS health "
            "  FROM stack_deployments WHERE target_id = ANY(:ids) AND status = 'healthy' "
            "  UNION ALL "
            "  SELECT deployment_target_id, "
            "         CASE WHEN trigger IS NULL THEN 'registry' ELSE 'delivery' END, id, "
            "         commit_ref, COALESCE(finished_at, started_at, created_at), "
            "         health_check_results "
            "  FROM deployment_runs WHERE deployment_target_id = ANY(:ids) "
            "    AND status IN ('succeeded', 'deployed')) x "
            "ORDER BY target_id, at DESC"), {"ids": list(target_ids)}).mappings():
        # Healthy by the same rule as every deploy view (deploy_status): a stack deployment
        # is healthy by status; a deployment run by the health check it recorded.
        healthy = r["kind"] == "stack" or deploy_status("succeeded", health=r["health"])["healthy"]
        out[r["target_id"]] = {"sha7": sha7(r["sha"]) or (str(r["sha"] or "")[:12] or None),
                               "at": _iso(r["at"]), "word": "Live",
                               "ref": f"{r['kind']}:{r['id']}", "failed_after": None,
                               "healthy": bool(healthy),
                               "_at": r["at"]}  # type: ignore[typeddict-unknown-key]
    newer = failed_after(connection, {t: (d["ref"], d.pop("_at")) for t, d in out.items()})  # type: ignore[misc]
    for t, f in newer.items():
        out[t]["failed_after"] = f
    return out


def _ports_of(domains: Any) -> set[int]:
    ports: set[int] = set()
    for d in (_json(domains) or []):
        tail = str(d).rstrip("/").rpartition(":")[2]
        if tail.isdigit():
            ports.add(int(tail))
    return ports


# ── monitoring stack (stored only; never probed here) ───────────────────────────────────────

MONITORING_EVENTS = ("observability.stack_deployed", "observability.stack_checked")


def monitoring_stored(connection: Any, project_id: str) -> Optional[dict[str, Any]]:
    """The monitoring stack as BOSS last recorded it: where it was deployed (the host of the
    target it borrowed) and, when a check was recorded, whether it was running. Read from
    `events`; this function never opens a connection to the host."""
    rows = _rows(connection,
                 "SELECT event_type, entity_id, data, created_at FROM events "
                 "WHERE project_id = :p AND event_type = ANY(:k) "
                 "ORDER BY created_at DESC LIMIT 20",
                 {"p": project_id, "k": list(MONITORING_EVENTS)})
    deployed = next((r for r in rows if r["event_type"] == "observability.stack_deployed"), None)
    if not deployed:
        return None
    checked = next((r for r in rows if r["event_type"] == "observability.stack_checked"
                    and r["created_at"] >= deployed["created_at"]), None)
    data = _json(deployed["data"]) or {}
    target = connection.execute(
        text("SELECT host_id, target_ref FROM deployment_targets WHERE id = :i"),
        {"i": deployed["entity_id"]}).mappings().first()
    raw = "unknown"
    if checked is not None:
        cdata = _json(checked["data"]) or {}
        if "running" in cdata:
            raw = "running" if cdata.get("running") else "stopped"
    return {"host_id": (target or {}).get("host_id"), "address": data.get("host"),
            "grafana_url": data.get("grafana"), "raw": raw,
            "checked_at": _iso(checked["created_at"]) if checked is not None else None}


# ── Fleet ─────────────────────────────────────────────────────────────────────────────────────

def _target_resident(t: dict[str, Any], health: dict[str, dict[str, Any]],
                     strip_: dict[str, list[list[int]]], last: dict[str, LastDeploy]) -> Resident:
    h = health.get(t["id"]) or {"word": "No signal", "tone": "hollow"}
    url = t.get("public_url") or ((_json(t.get("domains")) or [None])[0])
    shipped = last.get(t["id"])
    return {"kind": "target", "id": t["id"], "name": t["app_name"], "env": t["environment"],
            "health": {"word": h["word"], "tone": h["tone"]},  # type: ignore[typeddict-item]
            "strip": strip_.get(t["id"]) or [[0, 0] for _ in range(STRIP_CELLS)],
            "last_deploy": shipped, "expires_at": None,
            "url": url,
            # The app, opened from its tile: only once its newest deploy went live healthy.
            "open_url": _app_url(url) if shipped and shipped.get("healthy") else None,
            "port": None}


def _preview_resident(p: dict[str, Any]) -> Resident:
    raw = str(p.get("status") or "")
    w = word("preview", raw)
    return {"kind": "preview", "id": p["id"], "name": f"Preview {preview_app(p)}",
            "env": p.get("branch"), "health": w, "strip": [], "last_deploy": None,
            "expires_at": _iso(p.get("expires_at")), "url": p.get("url"),
            "port": p.get("port")}


def preview_app(p: dict[str, Any]) -> str:
    repo = str(p.get("repo") or "").strip().rstrip("/")
    if repo:
        return repo.rpartition("/")[2].removesuffix(".git") or repo
    return str(p.get("run_id") or p.get("id") or "app")[:24]


def _this_computer(connection: Any, project_id: str, hosts: list[dict[str, Any]]
                   ) -> tuple[Optional[ThisComputer], Optional[dict[str, Any]]]:
    place = connection.execute(
        text("SELECT id, name, hostname, arch, os_name, agent_state, status, endpoint "
             "FROM hardware_places WHERE project_id = :p AND is_default "
             "ORDER BY created_at LIMIT 1"), {"p": project_id}).mappings().first()
    if not place:
        return None, None
    place = dict(place)
    names = {str(v).strip().lower() for v in (place.get("hostname"), place.get("name"))
             if v and str(v).strip()}
    endpoint = str(place.get("endpoint") or "").lower()
    match = None
    for h in hosts:
        if h.get("status") == "retired":
            continue
        if (str(h.get("name") or "").lower() in names
                or str(h.get("address") or "").lower() in names
                or (endpoint and str(h.get("address") or "").lower() in endpoint)):
            match = h
            break
    return ({"hostname": place.get("hostname"), "arch": place.get("arch"),
             "os": place.get("os_name"), "agent_state": place.get("agent_state"),
             "host_id": match["id"] if match else None, "place_id": place["id"]}, place)


def _place_rack(this: ThisComputer, place: Optional[dict[str, Any]]) -> Rack:
    ready = bool(place) and place.get("status") == "ready"
    return {"id": "this", "this_computer": True,
            "name": (this.get("hostname") or (place or {}).get("name") or "This computer"),
            "address": None, "arch": this.get("arch"),
            "state": word("host", "verified") if ready
            else {"word": "Setting up", "tone": "accent"},  # type: ignore[typeddict-item]
            "stage": ({"stage": "ready", "index": 5, "label": "Ready"} if ready
                      else {"stage": "verify", "index": 3, "label": "Verify"}),  # type: ignore
            "checked_at": None, "max_apps": None, "apps": 0, "ports_used": 0,
            "ports_total": None, "residents": [], "stage_action": None}


def _fleet_parts(connection: Any, settings: Any, project_id: str, partial: list[str], *,
                 can_write: bool = True) -> dict[str, Any]:
    """Everything the racks are drawn from, read once (shared by fleet and host_detail)."""
    now = _now()
    hosts = _guard(connection, partial, "hosts", lambda: [
        _host_ready(h) for h in _rows(connection,
                                      "SELECT * FROM deployment_hosts WHERE project_id = :p",
                                      {"p": project_id})], [])
    targets = _guard(connection, partial, "targets", lambda: _rows(
        connection,
        "SELECT id, app_name, environment, repo, target_kind, target_ref, host_id, domains, "
        "       status, public_url, provider_account_id, manifest_path "
        "FROM deployment_targets WHERE project_id = :p AND status <> 'retired' "
        "ORDER BY app_name, environment", {"p": project_id}), [])
    previews = _guard(connection, partial, "previews", lambda: _rows(
        connection,
        "SELECT id, run_id, repo, branch, port, url, status, expires_at, created_at "
        "FROM app_previews WHERE project_id = :p AND status IN ('running', 'starting') "
        "ORDER BY created_at DESC", {"p": project_id}), [])
    ids = [t["id"] for t in targets]
    health = _guard(connection, partial, "health", lambda: health_states(connection, ids, now=now), {})
    strip_ = _guard(connection, partial, "health", lambda: strips(connection, ids, now=now), {})
    last = _guard(connection, partial, "deploys", lambda: last_deploys(connection, ids), {})
    this, place = _guard(connection, partial, "this computer",
                         lambda: _this_computer(connection, project_id, hosts), (None, None))
    monitoring = _guard(connection, partial, "monitoring",
                        lambda: monitoring_stored(connection, project_id), None)
    host_writes = preconditions(settings)["host_writes_allowed"]

    racks: dict[str, Rack] = {}
    retired = 0
    for h in hosts:
        if h.get("status") == "retired":
            # DRAWN, MARKED RETIRED (P-I-45). "Show retired" toggled a client filter over racks
            # the server had already dropped, so it never showed anything. A retired machine is
            # a rack with the Retired word, no residents and nothing to press; the console hides
            # it until the operator asks.
            retired += 1
            racks[h["id"]] = {
                "id": h["id"], "this_computer": False, "name": h["name"],
                "address": h.get("address"), "arch": h.get("arch"),
                "state": word("host", "retired"),
                "stage": {"stage": "ready", "index": 5, "label": "Retired"},  # type: ignore
                "checked_at": _iso(h.get("last_preflight_at")), "max_apps": h.get("max_apps"),
                "apps": 0, "ports_used": 0, "ports_total": None, "residents": [],
                "stage_action": None, "retired": True,
            }
            continue
        st = host_stage(h)
        racks[h["id"]] = {
            "id": h["id"], "this_computer": bool(this and this.get("host_id") == h["id"]),
            "name": h["name"], "address": h.get("address"), "arch": h.get("arch"),
            "state": host_word(h), "stage": st, "checked_at": _iso(h.get("last_preflight_at")),
            "max_apps": h.get("max_apps"), "apps": 0, "ports_used": 0,
            "ports_total": (int(h["port_max"]) - int(h["port_min"]) + 1
                            if h.get("port_max") is not None and h.get("port_min") is not None
                            else None),
            "residents": [],
            "stage_action": (None if st["stage"] == "ready" else
                             _stage_action(h, st, can_write=can_write, host_writes=host_writes)),
        }
    this_rack_id = (this or {}).get("host_id")
    if this is not None and not this_rack_id:
        racks["this"] = _place_rack(this, place)
        this_rack_id = "this"
    if previews and not this_rack_id:
        # Previews run on the BOSS machine even when no hardware place was recorded for it.
        racks["this"] = _place_rack({"hostname": None, "arch": None}, None)
        racks["this"]["name"] = "This computer"
        this_rack_id = "this"

    ports: dict[str, set[int]] = {k: set() for k in racks}
    unplaced: list[Resident] = []
    for t in targets:
        res = _target_resident(t, health, strip_, last)
        rack = racks.get(str(t.get("host_id") or ""))
        if rack is not None and rack.get("retired"):
            rack = None  # an app still pointed at a retired machine is not placed anywhere
        if rack is None:
            unplaced.append(res)
            continue
        rack["residents"].append(res)
        rack["apps"] += 1
        ports[rack["id"]] |= _ports_of(t.get("domains"))
    for p in previews:
        if this_rack_id and this_rack_id in racks:
            racks[this_rack_id]["residents"].append(_preview_resident(p))
            if p.get("port"):
                ports[this_rack_id].add(int(p["port"]))
    if monitoring:
        rack_id = monitoring.get("host_id")
        if not rack_id or rack_id not in racks:
            rack_id = next((h["id"] for h in hosts if monitoring.get("address")
                            and h.get("address") == monitoring.get("address")
                            and h["id"] in racks), None)
        if rack_id:
            racks[rack_id]["residents"].append({
                "kind": "monitoring", "id": "monitoring", "name": "Monitoring", "env": None,
                "health": word("stack", monitoring["raw"]), "strip": [], "last_deploy": None,
                "expires_at": None, "url": monitoring.get("grafana_url"), "port": None})
    for rid, rack in racks.items():
        rack["ports_used"] = len(ports.get(rid) or ())
    ordered = sorted(racks.values(), key=lambda r: (
        2 if r.get("retired") else 0 if r.get("this_computer") else 1, _host_rank(r["state"]),
        str(r["name"]).lower()))
    return {"hosts": hosts, "targets": targets, "previews": previews, "health": health,
            "strips": strip_, "last": last, "this": this, "place": place, "racks": ordered,
            "unplaced": unplaced, "retired": retired}


def fleet(connection: Any, settings: Any, project_id: str, *, viewer: Optional[str] = None) -> Fleet:
    partial: list[str] = []
    can_write = viewer_can_write(connection, settings, project_id, viewer,
                                 catalogue_permission="host:manage")
    parts = _fleet_parts(connection, settings, project_id, partial, can_write=can_write)
    items = _guard(connection, partial, "attention",
                   lambda: attention(connection, settings, project_id), [])
    mine = [i for i in items if i.get("area") == "infra"]
    # COUNTED FROM THE RACKS THE PAGE DRAWS — "This computer" is a rack too. Counting hosts
    # said "2 ready" above three Ready racks.
    words = [str((r.get("state") or {}).get("word") or "") for r in parts["racks"]
             if str((r.get("state") or {}).get("word") or "") != "Retired"]
    body: dict[str, Any] = {
        "counts": {
            "ready": sum(1 for w in words if w == "Ready"),
            "setting_up": sum(1 for w in words if w.startswith("Setting up")),
            "unreachable": sum(1 for w in words if w == "Unreachable"),
            "apps": len(parts["targets"]),
            "previews_live": len(parts["previews"]),
            # THE ONE NEEDS-YOU RULE (command_center.attention_counts): decisions only. A
            # failed deploy or an unreachable host is listed, and is not counted as needing you.
            "needs_you": _needs_you_count(mine),
        },
        "preconditions": preconditions(settings, can_write=can_write),
        "attention": mine,
        "this_computer": parts["this"],
        "racks": parts["racks"],
        "unplaced": parts["unplaced"],
        "retired_count": parts["retired"],
        "partial": partial,
    }
    return _finish(body)  # type: ignore[return-value]


def _host_key_words(host: dict[str, Any]) -> dict[str, Any]:
    from .host_key_pin import describe

    return describe(host)


def host_detail(connection: Any, settings: Any, project_id: str, host_id: str) -> HostDetail:
    from .deployment_fleet import install_variants_for

    partial: list[str] = []
    parts = _fleet_parts(connection, settings, project_id, partial)
    pre = preconditions(settings)
    rack = next((r for r in parts["racks"] if r["id"] == host_id
                 or (host_id == "this" and r.get("this_computer"))), None)
    this = parts["this"] or {}
    if host_id == "this" and rack is None:
        raise LookupError("This computer is not recorded.")
    host = next((h for h in parts["hosts"] if h["id"] == (rack or {}).get("id", host_id)), None)
    if host is None and (rack is None or rack["id"] != "this"):
        raise LookupError("No such machine in this project.")
    is_this = bool(rack and rack.get("this_computer"))
    if host is None:
        place = parts["place"] or {}
        body: dict[str, Any] = {
            "id": "this", "this_computer": True, "name": rack["name"], "state": rack["state"],
            "stage": rack["stage"],
            "facts": {"hostname": this.get("hostname"), "arch": this.get("arch"),
                      "os": this.get("os"), "agent_state": this.get("agent_state")},
            "key_public": None, "authorise_line": None, "install_command": None,
            "route_options": [], "findings": [], "checked_at": None,
            "residents": rack["residents"],
            "can": {"adopt_key": False, "generate_key": False, "verify": False,
                    "install_key": False, "install_docker": False, "retire": False,
                    "correct": bool(place)},
            "partial": partial,
        }
        return _finish(body)  # type: ignore[return-value]
    variants = install_variants_for(host)
    key_public = (host.get("key_public") or "").strip() or None
    route_options = [
        {"key": k, "label": label, "command": str(variants.get(k) or "")}
        for k, label in (("from_laptop", "SSH from here"), ("on_host", "Provider console"),
                         ("cloud_init", "Not created yet"))
        if variants.get(k)]
    facts = {"address": host.get("address"), "user": host.get("ssh_user"),
             "port": host.get("ssh_port"), "arch": host.get("arch"),
             "labels": host.get("labels") or {}, "port_min": host.get("port_min"),
             "port_max": host.get("port_max"), "max_apps": host.get("max_apps"),
             "key_fingerprint": key_fingerprint(key_public)}
    if is_this:
        facts.update({"hostname": this.get("hostname"), "os": this.get("os"),
                      "agent_state": this.get("agent_state")})
    retired = host.get("status") == "retired"
    body = {
        "id": host["id"], "this_computer": is_this, "name": host["name"],
        "state": host_word(host), "stage": host_stage(host),
        "facts": facts, "key_public": key_public, "authorise_line": authorize_line(key_public),
        "install_command": host.get("install_command"), "route_options": route_options,
        "findings": _findings(host.get("last_preflight") or {}),
        "checked_at": _iso(host.get("last_preflight_at")),
        "residents": (rack or {}).get("residents") or [],
        # WHICH MACHINE BOSS WILL TALK TO: the host's own key, pinned at Verify (host_key_pin).
        "host_key": (None if is_this else _host_key_words(host)),
        "can": {
            "adopt_key": not retired,
            "generate_key": not retired,
            "verify": pre["outward_ssh_allowed"] and not retired and bool(host.get("key_secret_ref")),
            "install_key": pre["host_writes_allowed"] and bool(host.get("key_secret_ref")),
            "install_docker": pre["host_writes_allowed"] and not retired,
            "retire": pre["host_writes_allowed"] and not retired and not is_this,
            "correct": is_this,
        },
        "partial": partial,
    }
    return _finish(body)  # type: ignore[return-value]


# ── Deploys (§4.6, §6.3) ──────────────────────────────────────────────────────────────────────
# Three kinds, two tables: `stack_deployments` (stack), and `deployment_runs` split by how they
# were made. The delivery routes (/v1/delivery/deploy, merge, redeploy, the pipeline) stamp a
# `trigger`; a run planned straight through the registry (POST /v1/deployment-runs, promotions)
# has none. GET /v1/delivery/deployments lists the same table.

_UNION = """
  SELECT 'stack'::text AS kind, s.id, s.target_id, s.host_id, s.status, s.commit_sha AS sha,
         s.ref AS ref_name, s.decision_id, s.rollback_of AS rollback_of,
         s.workflow_run_id, s.code_task_run_id AS run_id, NULL::text AS trig, s.steps,
         COALESCE(s.apply_started_at, s.created_at) AS started_at, s.finished_at, s.created_at,
         CAST(CASE WHEN s.finished_at IS NOT NULL AND s.apply_started_at IS NOT NULL
              THEN extract(epoch FROM (s.finished_at - s.apply_started_at)) * 1000 END AS bigint)
           AS duration_ms,
         s.apply_started_at, s.failure_reason,
         NULL::jsonb AS health, s.open_url AS open_url
  FROM stack_deployments s WHERE s.project_id = :p
  UNION ALL
  SELECT CASE WHEN d.trigger IS NULL THEN 'registry' ELSE 'delivery' END, d.id,
         d.deployment_target_id, NULL::text, d.status, d.commit_ref, d.commit_ref, d.decision_id,
         CASE WHEN d.trigger = 'rollback' THEN d.release_packet->>'redeploy_of' END,
         NULL::text, d.release_packet->>'code_task_run_id',
         d.trigger, d.steps, COALESCE(d.started_at, d.created_at), d.finished_at, d.created_at,
         CAST(COALESCE(d.duration_ms, CASE WHEN d.finished_at IS NOT NULL
              AND d.started_at IS NOT NULL
              THEN extract(epoch FROM (d.finished_at - d.started_at)) * 1000 END) AS bigint),
         d.started_at, d.failure_reason,
         d.health_check_results, NULL::text
  FROM deployment_runs d WHERE d.project_id = :p
"""

_STEP_DONE = {"ok", "done", "succeeded", "success", "healthy", "passed", "completed"}
_STEP_FAILED = {"failed", "error", "errored", "blocked"}


def step_state(raw: Any) -> StepState:
    s = str(raw or "").strip().lower()
    if s in _STEP_DONE:
        return "done"
    if s in _STEP_FAILED:
        return "failed"
    if s in ("running", "started", "in_progress"):
        return "running"
    if s == "skipped":
        return "skipped"
    return "pending"


def _step_title(step: dict[str, Any]) -> str:
    return str(step.get("label") or step.get("name") or step.get("step") or step.get("title")
               or "")


def _encode_cursor(ts: Any, ref: str) -> str:
    raw = json.dumps([_iso(ts), ref]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: Optional[str]) -> Optional[tuple[datetime, str]]:
    if not cursor:
        return None
    try:
        pad = "=" * (-len(cursor) % 4)
        ts, ref = json.loads(base64.urlsafe_b64decode((cursor + pad).encode()))
        at = _aware(ts)
        return (at, str(ref)) if at else None
    except (ValueError, TypeError):
        return None


def _trigger(r: dict[str, Any]) -> str:
    if r["kind"] == "stack":
        if r.get("rollback_of"):
            return "rollback"
        return "workflow" if r.get("workflow_run_id") else "manual"
    if r["kind"] == "registry":
        return "manual"
    t = str(r.get("trig") or "").lower()
    return t if t in ("merge", "workflow", "rollback", "redeploy") else "manual"


def _fit(row: dict[str, Any]) -> dict[str, Any]:
    """Hold a row to the list budget: shorten step titles, then drop them, never the dots."""
    def size() -> int:
        return len(json.dumps(row, default=str).encode())
    if size() <= ROW_BUDGET_BYTES:
        return row
    for cap in (16, 8, 0):
        row["steps"] = [{"s": s["s"], "t": s.get("t", "")[:cap]} for s in row["steps"]]
        if size() <= ROW_BUDGET_BYTES:
            return row
    for key in ("ref_name", "account", "app", "env"):
        if isinstance(row.get(key), str):
            row[key] = row[key][:40]
    return row


def _approvers(connection: Any, project_id: str, names: set[str]) -> dict[str, str]:
    if not names:
        return {}
    return {r["member_id"]: str(r["member_type"] or "human") for r in connection.execute(text(
        "SELECT member_id, member_type FROM project_members "
        "WHERE project_id = :p AND member_id = ANY(:m)"),
        {"p": project_id, "m": sorted(names)}).mappings()}


def _approver(name: Optional[str], viewer: Optional[str], types: dict[str, str]
              ) -> Optional[dict[str, str]]:
    if not name:
        return None
    if viewer and name == viewer:
        return {"actor": "you"}
    if types.get(name) == "agent":
        return {"role_key": name}
    return {"actor": name}


def _stack_history(connection: Any, target_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    hist: dict[str, list[dict[str, Any]]] = {t: [] for t in target_ids}
    if target_ids:
        for h in connection.execute(text(
                "SELECT id, target_id, status, commit_sha, apply_started_at, created_at "
                "FROM stack_deployments WHERE target_id = ANY(:t)"),
                {"t": list(target_ids)}).mappings():
            hist[h["target_id"]].append(dict(h))
    return hist


def _run_history(connection: Any, target_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Delivery runs per target, newest first (for the delivery rollback rule)."""
    hist: dict[str, list[dict[str, Any]]] = {t: [] for t in target_ids}
    if target_ids:
        for h in connection.execute(text(
                "SELECT id, deployment_target_id AS target_id, status, commit_ref, created_at "
                "FROM deployment_runs WHERE deployment_target_id = ANY(:t) "
                "  AND trigger IS NOT NULL ORDER BY created_at DESC"),
                {"t": list(target_ids)}).mappings():
            hist[h["target_id"]].append(dict(h))
    return hist


def delivery_rollback_source(row_id: str, history: list[dict[str, Any]]) -> Optional[str]:
    """The run a delivery rollback redeploys: offered only on a target's NEWEST delivery run when
    it failed, and only when an earlier succeeded run exists (its id is returned)."""
    if not history or history[0]["id"] != row_id or history[0]["status"] != "failed":
        return None
    for h in history[1:]:
        if h["status"] in ("succeeded", "deployed"):
            return h["id"]
    return None


def governed_host(r: dict[str, Any]) -> bool:
    """A delivery deploy to an SSH host: it runs only through the governed plan → approve →
    apply path (governed_host_deploy.py), so Community may redeploy and roll it back."""
    from .governed_host_deploy import is_host_kind

    return is_host_kind(r.get("target_kind"))


def _deploy_can(r: dict[str, Any], *, can_write: bool, is_community: bool,
                stack_hist: dict[str, list[dict[str, Any]]],
                run_hist: dict[str, list[dict[str, Any]]]) -> dict[str, bool]:
    return _deploy_can_to(r, can_write=can_write, is_community=is_community,
                          stack_hist=stack_hist, run_hist=run_hist)[0]


def _deploy_can_to(r: dict[str, Any], *, can_write: bool, is_community: bool,
                   stack_hist: dict[str, list[dict[str, Any]]],
                   run_hist: dict[str, list[dict[str, Any]]]
                   ) -> tuple[dict[str, bool], Optional[str]]:
    """`can` and, when Roll back is offered, the sha7 it would go back to."""
    from .stack_deploy import rollback_option

    waiting = bool(r.get("decision_id")) and r.get("decision_status") == "pending" \
        and r["status"] in WAITING_STATES
    can = {"approve": waiting and can_write, "reject": waiting and can_write,
           "rollback": False, "redeploy": False}
    to: Optional[str] = None
    if not can_write:
        return can, None
    if r["kind"] == "stack":
        option = rollback_option(
            {"id": r["id"], "status": r["status"], "commit_sha": r["sha"],
             "apply_started_at": r.get("apply_started_at"), "created_at": r["created_at"]},
            stack_hist.get(r["target_id"]) or [])
        can["rollback"] = bool(option["rollback_available"])
        to = sha7(option.get("rollback_commit")) if can["rollback"] else None
    # NOT HIDDEN BY EDITION (operator decision 2026-10-04): Redeploy and Rollback will go through
    # the governed deploy path (plan → change → approval → apply) in every edition; until it
    # answers, the route's own guard refuses and the drawer says why.
    elif r["kind"] == "delivery":
        hist = run_hist.get(r["target_id"]) or []
        source = delivery_rollback_source(r["id"], hist)
        can["rollback"] = source is not None
        if source is not None:
            to = sha7(next((h["commit_ref"] for h in hist if h["id"] == source), None))
        can["redeploy"] = r["status"] in TERMINAL_RUN_STATES
    return can, to


def _shape_rows(connection: Any, settings: Any, project_id: str, raw: list[dict[str, Any]], *,
                live_refs: set[str], viewer: Optional[str], can_write: bool
                ) -> list[DeployRow]:
    is_community = community(settings)
    tids = sorted({r["target_id"] for r in raw if r.get("target_id")})
    stack_hist = _stack_history(connection, [t for t in tids
                                             if any(x["kind"] == "stack" and x["target_id"] == t
                                                    for x in raw)])
    run_hist = _run_history(connection, [t for t in tids
                                         if any(x["kind"] == "delivery" and x["target_id"] == t
                                                for x in raw)])
    types = _approvers(connection, project_id,
                       {r["approved_by"] for r in raw if r.get("approved_by")})
    # What a rollback rolled back from: the sha of the deploy it replaced.
    rb_ids = [r["rollback_of"] for r in raw if r.get("rollback_of")]
    rb_sha: dict[str, Any] = {}
    if rb_ids:
        for x in connection.execute(text(
                "SELECT id, commit_sha AS sha FROM stack_deployments WHERE id = ANY(:i) "
                "UNION ALL SELECT id, commit_ref FROM deployment_runs WHERE id = ANY(:i)"),
                {"i": rb_ids}).mappings():
            rb_sha[x["id"]] = x["sha"]
    # A Live row says when a newer attempt on its target failed (operator decision 2026-10-04).
    live_at = {r["target_id"]: (r["ref"], r.get("finished_at") or r.get("started_at"))
               for r in raw if r["ref"] in live_refs and r.get("target_id")}
    newer_failed = failed_after(connection, live_at) if live_at else {}
    out: list[DeployRow] = []
    for r in raw:
        status = str(r["status"] or "")
        state = status
        if (r["kind"] != "stack" and r.get("decision_status") == "pending"
                and status in WAITING_STATES):
            state = "awaiting_approval"
        # THE ONE READING (deploy_status): word, sentence, the step running now, the app link.
        seen = deploy_status(
            state, decision_status=r.get("decision_status"), steps=r.get("steps"),
            health=r.get("health"),
            url=r.get("open_url") or r.get("target_url")
            or ((_json(r.get("target_domains")) or [None])[0]),
            failure=r.get("failure_reason"), newest_for_target=r["ref"] in live_refs,
            success_means_healthy=r["kind"] == "stack")
        if seen["phase"] == "superseded":
            state = "superseded"
        w = {"word": seen["word"], "tone": seen["tone"]}
        steps = [{"s": step_state(s.get("status")), "t": _step_title(s)[:40]}
                 for s in (_json(r.get("steps")) or [])[:MAX_STEPS] if isinstance(s, dict)]
        decided = r.get("decision_status") in ("approved", "rejected", "executed")
        can, rollback_to = _deploy_can_to({**r, "status": state}, can_write=can_write,
                                          is_community=is_community, stack_hist=stack_hist,
                                          run_hist=run_hist)
        row: DeployRow = {
            "ref": r["ref"], "kind": r["kind"], "target_id": r.get("target_id"),
            "app": r.get("app_name") or "", "env": r.get("environment") or "",
            "host": ({"id": r["hid"], "name": r["host_name"]} if r.get("hid") and r.get("host_name")
                     else None),
            "account": r.get("account_label"),
            "sha7": sha7(r.get("sha")), "ref_name": (str(r.get("ref_name") or "")[:60] or None),
            "trigger": _trigger(r),  # type: ignore[typeddict-item]
            "rollback_of_sha7": sha7(rb_sha.get(r.get("rollback_of"))),
            "approver": _approver(r.get("approved_by") if decided else None, viewer, types),
            "state": state, "word": w["word"], "tone": w["tone"], "steps": steps,
            "started_at": _iso(r.get("started_at")), "finished_at": _iso(r.get("finished_at")),
            # A deploy that never ran has no duration: rejected, refused or still waiting.
            "duration_ms": (int(r["duration_ms"]) if r.get("duration_ms") is not None
                            and state not in NEVER_RAN_STATES else None),
            "can": can,
            "rollback_to_sha7": rollback_to,
            "failed_after": (newer_failed.get(r["target_id"]) if r["ref"] in live_refs else None),
            "decision_id": r.get("decision_id"), "run_id": r.get("run_id"),
            "phase": seen["phase"], "sentence": seen["sentence"], "now": seen["now"],
            "open_url": seen["open_url"] if r["ref"] in live_refs else None,
        }
        out.append(_fit(row))  # type: ignore[arg-type]
    return out


_STATE_FILTERS = {
    "needs_you": ("u.decision_id IS NOT NULL AND dd.status = 'pending' "
                  "AND u.status = ANY(:waiting)"),
    "live": "(u.kind || ':' || u.id) IN (SELECT ref FROM live)",
    "failed": "u.status = ANY(:failed)",
}


def _union_query(where: str, tail: str) -> str:
    return (
        "WITH u AS (" + _UNION + "), "
        "live AS (SELECT DISTINCT ON (target_id) kind || ':' || id AS ref FROM u "
        "         WHERE status = ANY(:success) "
        "         ORDER BY target_id, COALESCE(finished_at, started_at, created_at) DESC) "
        "SELECT u.*, COALESCE(u.finished_at, u.started_at, u.created_at) AS ts, "
        "       u.kind || ':' || u.id AS ref, t.app_name, t.environment, t.target_kind, "
        "       t.public_url AS target_url, t.domains AS target_domains, "
        "       COALESCE(u.host_id, t.host_id) AS hid, h.name AS host_name, "
        "       pa.account_label, dd.status AS decision_status, dd.approved_by "
        "FROM u LEFT JOIN deployment_targets t ON t.id = u.target_id "
        "LEFT JOIN deployment_hosts h ON h.id = COALESCE(u.host_id, t.host_id) "
        "LEFT JOIN provider_accounts pa ON pa.id = t.provider_account_id "
        "LEFT JOIN decisions dd ON dd.id = u.decision_id "
        f"WHERE {where} {tail}")


def _union_params(project_id: str) -> dict[str, Any]:
    return {"p": project_id, "success": list(SUCCESS_STATES), "failed": list(FAILED_STATES),
            "waiting": list(WAITING_STATES)}


def deploy_rows(connection: Any, settings: Any, project_id: str, *, state: Optional[str] = None,
                app: Optional[str] = None, env: Optional[str] = None, cursor: Optional[str] = None,
                limit: int = 30, viewer: Optional[str] = None) -> DeployList:
    partial: list[str] = []
    limit = max(1, min(int(limit or DEPLOY_PAGE), 100))
    can_write = viewer_can_write(connection, settings, project_id, viewer)
    params = _union_params(project_id)
    scope = ["TRUE"]
    if app:
        scope.append("t.app_name = :app")
        params["app"] = app
    if env:
        scope.append("t.environment = :env")
        params["env"] = env
    where = list(scope)
    if state in _STATE_FILTERS:
        where.append(_STATE_FILTERS[state])
    after = _decode_cursor(cursor)
    if after:
        where.append("(COALESCE(u.finished_at, u.started_at, u.created_at), "
                     "u.kind || ':' || u.id) < (:cts, :cref)")
        params.update({"cts": after[0], "cref": after[1]})

    def page() -> tuple[list[dict[str, Any]], set[str]]:
        raw = _rows(connection, _union_query(" AND ".join(where),
                                             "ORDER BY ts DESC, ref DESC LIMIT :lim"),
                    {**params, "lim": limit + 1})
        live = {r[0] for r in connection.execute(text(
            "WITH u AS (" + _UNION + ") SELECT DISTINCT ON (target_id) kind || ':' || id "
            "FROM u WHERE status = ANY(:success) "
            "ORDER BY target_id, COALESCE(finished_at, started_at, created_at) DESC"),
            {"p": project_id, "success": list(SUCCESS_STATES)})}
        return raw, live

    raw, live = _guard(connection, partial, "deploys", page, ([], set()))
    more = len(raw) > limit
    raw = raw[:limit]
    rows = _guard(connection, partial, "deploys", lambda: _shape_rows(
        connection, settings, project_id, raw, live_refs=live, viewer=viewer,
        can_write=can_write), [])

    def counts() -> dict[str, int]:
        sql_ = ("WITH u AS (" + _UNION + "), "
                "live AS (SELECT DISTINCT ON (target_id) kind || ':' || id AS ref FROM u "
                "         WHERE status = ANY(:success) "
                "         ORDER BY target_id, COALESCE(finished_at, started_at, created_at) DESC) "
                "SELECT count(*) AS all_, "
                f"  count(*) FILTER (WHERE {_STATE_FILTERS['needs_you']}) AS needs_you, "
                f"  count(*) FILTER (WHERE {_STATE_FILTERS['live']}) AS live, "
                f"  count(*) FILTER (WHERE {_STATE_FILTERS['failed']}) AS failed "
                "FROM u LEFT JOIN deployment_targets t ON t.id = u.target_id "
                "LEFT JOIN decisions dd ON dd.id = u.decision_id "
                f"WHERE {' AND '.join(scope)}")
        r = connection.execute(text(sql_), params).mappings().one()
        return {"needs_you": int(r["needs_you"]), "live": int(r["live"]),
                "failed": int(r["failed"]), "all": int(r["all_"])}

    def filters() -> dict[str, list[str]]:
        fr = _rows(connection,
                   "SELECT DISTINCT t.app_name, t.environment FROM deployment_targets t "
                   "WHERE t.project_id = :p AND (EXISTS (SELECT 1 FROM stack_deployments s "
                   "  WHERE s.target_id = t.id) OR EXISTS (SELECT 1 FROM deployment_runs d "
                   "  WHERE d.deployment_target_id = t.id))", {"p": project_id})
        return {"apps": sorted({r["app_name"] for r in fr if r["app_name"]}),
                "envs": sorted({r["environment"] for r in fr if r["environment"]})}

    items = _guard(connection, partial, "attention",
                   lambda: attention(connection, settings, project_id), [])
    body: dict[str, Any] = {
        "counts": _guard(connection, partial, "counts", counts,
                         {"needs_you": 0, "live": 0, "failed": 0, "all": 0}),
        "rows": rows,
        "next_cursor": (_encode_cursor(raw[-1]["ts"], raw[-1]["ref"]) if more and raw else None),
        "filters": _guard(connection, partial, "filters", filters, {"apps": [], "envs": []}),
        "attention": [i for i in items if i.get("area") == "infra"],
        "partial": partial,
    }
    return _finish(body)  # type: ignore[return-value]


def _parse_ref(ref: str) -> tuple[str, str]:
    kind, _, ident = str(ref or "").partition(":")
    if kind not in ("stack", "registry", "delivery") or not ident:
        raise LookupError(f"Not a deploy: {ref}")
    return kind, ident


def _one_row(connection: Any, settings: Any, project_id: str, ref: str,
             viewer: Optional[str] = None) -> tuple[dict[str, Any], DeployRow]:
    kind, ident = _parse_ref(ref)
    params = _union_params(project_id)
    params.update({"k": kind, "i": ident})
    raw = _rows(connection, _union_query("u.kind = :k AND u.id = :i", "LIMIT 1"), params)
    if not raw:
        raise LookupError(f"No such deploy in this project: {ref}")
    live = {r[0] for r in connection.execute(text(
        "WITH u AS (" + _UNION + ") SELECT DISTINCT ON (target_id) kind || ':' || id "
        "FROM u WHERE status = ANY(:success) AND target_id = :t "
        "ORDER BY target_id, COALESCE(finished_at, started_at, created_at) DESC"),
        {"p": project_id, "success": list(SUCCESS_STATES), "t": raw[0]["target_id"]})}
    row = _shape_rows(connection, settings, project_id, raw, live_refs=live, viewer=viewer,
                      can_write=viewer_can_write(connection, settings, project_id, viewer))[0]
    return raw[0], row


def _duration(step: dict[str, Any]) -> Optional[int]:
    if step.get("duration_ms") is not None:
        try:
            return int(step["duration_ms"])
        except (TypeError, ValueError):
            return None
    a, b = _aware(step.get("started_at")), _aware(step.get("ended_at"))
    return int((b - a).total_seconds() * 1000) if a and b else None


def _tail(log_text: Optional[str]) -> Optional[str]:
    if not log_text:
        return None
    data = log_text.encode("utf-8", "replace")
    if len(data) <= LOG_TAIL_BYTES:
        return log_text
    cut = data[-LOG_TAIL_BYTES:].decode("utf-8", "ignore")
    nl = cut.find("\n")
    return cut[nl + 1:] if 0 <= nl < len(cut) - 1 else cut


def _preflight_findings(pf: Any) -> list[Finding]:
    pf = _json(pf) or {}
    if not isinstance(pf, dict):
        return []
    out: list[Finding] = []
    for p in pf.get("problems") or []:
        if isinstance(p, dict):
            out.append({"kind": str(p.get("kind") or "other"),
                        "label": str(p.get("headline") or p.get("label")
                                     or finding_label(str(p.get("kind") or "other"))),
                        "ok": False, "detail": p.get("detail") or p.get("action"),
                        "remedy": p.get("action") or p.get("remedy")})
        else:
            out.append({"kind": "other", "label": str(p)[:120], "ok": False, "detail": None,
                        "remedy": None})
    for c in pf.get("checks") or []:
        if isinstance(c, dict):
            out.append({"kind": str(c.get("kind") or c.get("name") or "check"),
                        "label": str(c.get("label") or c.get("name") or c.get("kind") or ""),
                        "ok": bool(c.get("ok")), "detail": c.get("detail"), "remedy": None})
    if not out and pf.get("ok") is True:
        out.append({"kind": "ok", "label": "Host ready", "ok": True, "detail": pf.get("host"),
                    "remedy": None})
    return out


_PHASES = (("pipeline", "Pipeline verdict"), ("staging", "Staging"), ("smoke", "Smoke"),
           ("production", "Production"), ("rollout", "Rollout"))


def _promotion(connection: Any, run_id: str) -> Optional[dict[str, Any]]:
    p = connection.execute(text(
        "SELECT p.id, p.pipeline_verdict, p.gate_result, p.status, "
        "       s.status AS staging, d.status AS production, r.status AS rollout "
        "FROM deployment_promotions p "
        "LEFT JOIN deployment_runs s ON s.id = p.staging_deployment_run_id "
        "LEFT JOIN deployment_runs d ON d.id = p.production_deployment_run_id "
        "LEFT JOIN deployment_rollouts r ON r.id = p.production_rollout_id "
        "WHERE p.staging_deployment_run_id = :i OR p.production_deployment_run_id = :i "
        "ORDER BY p.created_at DESC LIMIT 1"), {"i": run_id}).mappings().first()
    if not p:
        return None
    states = {"pipeline": str((_json(p["pipeline_verdict"]) or {}).get("status") or "pending"),
              "staging": str(p["staging"] or "waiting"),
              "smoke": str((_json(p["gate_result"]) or {}).get("status") or "waiting"),
              "production": str(p["production"] or "waiting"),
              "rollout": str(p["rollout"] or "waiting")}
    return {"id": p["id"], "status": p["status"],
            "phases": [{"key": k, "label": label, "state": states[k]} for k, label in _PHASES]}


def _app_url(value: Any) -> Optional[str]:
    """A link the browser opens as the app: a bare `host:port` domain would be read as a path
    under the console, so it gets its scheme."""
    text_ = str(value or "").strip()
    if not text_:
        return None
    return text_ if "://" in text_ else f"http://{text_}/"


def deploy_detail(connection: Any, settings: Any, project_id: str, ref: str) -> DeployDetail:
    raw, row = _one_row(connection, settings, project_id, ref)
    kind, ident = _parse_ref(ref)
    if kind == "stack":
        full = connection.execute(text(
            "SELECT manifest, log_tail, preflight, error, failure_reason, waiting_on, open_url, "
            "       changeset_id, steps FROM stack_deployments WHERE id = :i"),
            {"i": ident}).mappings().one()
        manifest = _json(full["manifest"]) or {}
        steps_raw = _json(full["steps"]) or []
        health = (manifest.get("health") or [None])[0] if isinstance(manifest, dict) else None
        plan = ({"steps": list((manifest.get("steps") or {}).get("install") or []),
                 "env_keys": list(manifest.get("env_keys") or []),
                 "health": ({"path": health.get("url"), "expect": health.get("expect")}
                            if isinstance(health, dict) else None)}
                if manifest else None)
        error = _json(full["error"]) or {}
        err_text = (error.get("headline") if isinstance(error, dict) else str(error)) \
            or full["failure_reason"]
        body: dict[str, Any] = {
            "row": row,
            "steps": [{"title": _step_title(s), "state": step_state(s.get("status")),
                       "duration_ms": _duration(s)} for s in steps_raw if isinstance(s, dict)],
            "waiting_on": full["waiting_on"] if raw["status"] == "running" else None,
            "failure": ({"line": deploy_failure_line({**dict(full), "status": raw["status"],
                                                      "steps": steps_raw}),
                         "error": error_in_words(err_text)}
                        if raw["status"] in FAILED_STATES else None),
            "plan": plan,
            "preflight": _preflight_findings(full["preflight"]),
            "log_tail": _tail(full["log_tail"]),
            "health_check_results": [],
            "promotion": None,
            "open_url": full["open_url"],
            "changeset_id": full["changeset_id"],
        }
    else:
        full = connection.execute(text(
            "SELECT d.steps, d.failure_reason, d.health_check_results, d.changeset_id, "
            "       d.release_packet, t.public_url, t.domains FROM deployment_runs d "
            "LEFT JOIN deployment_targets t ON t.id = d.deployment_target_id WHERE d.id = :i"),
            {"i": ident}).mappings().one()
        # A governed host deploy carries the plan its approver saw (governed_host_deploy.py):
        # what will happen, the secrets by NAME, the rollback and the cost.
        review = (_json(full["release_packet"]) or {}).get("plan_review") or None
        governed = isinstance(review, dict)
        steps_raw = _json(full["steps"]) or []
        logs = "\n".join(f"[{_step_title(s)}]\n{s.get('log')}" for s in steps_raw
                         if isinstance(s, dict) and s.get("log"))
        body = {
            "row": row,
            "steps": [{"title": _step_title(s), "state": step_state(s.get("status")),
                       "duration_ms": _duration(s)} for s in steps_raw if isinstance(s, dict)],
            "waiting_on": None,
            "failure": ({"line": deploy_failure_line({**dict(full), "status": raw["status"],
                                                      "steps": steps_raw}),
                         "error": error_in_words(full["failure_reason"])}
                        if raw["status"] in FAILED_STATES
                        or (governed and raw["status"] == "rolled_back") else None),
            "plan": ({"steps": [*(review.get("steps") or []),
                                *[f"Rollback: {x}" for x in (review.get("rollback") or [])[:1]],
                                f"Cost: {usd((review.get('cost') or {}).get('expected_cost_usd_micros') or 0)}"
                                " (your own host)"],
                      "env_keys": [str(s.get("name")) for s in review.get("secret_refs") or []
                                   if isinstance(s, dict) and s.get("name")],
                      "health": None} if governed else None),
            "preflight": [],
            "log_tail": _tail(logs) if logs else None,
            "health_check_results": health_checks(full["health_check_results"]),
            "promotion": _promotion(connection, ident) if kind == "registry" else None,
            # A deploy links its app only once it went live AND its health check passed
            # (deploy_status) — the same rule the row and the workflow step use.
            "open_url": deploy_status(
                raw["status"], decision_status=raw.get("decision_status"),
                health=full["health_check_results"],
                url=full["public_url"] or ((_json(full["domains"]) or [None])[0]),
            )["open_url"],
            "changeset_id": full["changeset_id"],
        }
    body["live"] = row["state"] in ACTIVE_STATES
    return _finish(body)  # type: ignore[return-value]


def deploy_pins(connection: Any, target_ids: list[str], since: Any) -> dict[str, list[dict[str, Any]]]:
    """target_id → [{at, sha7, word, tone, ref}] for deploys that FINISHED at/after `since`,
    oldest first. A deploy still running has no pin: a pin marks when it finished."""
    out: dict[str, list[dict[str, Any]]] = {t: [] for t in target_ids}
    if not target_ids:
        return out
    rows = _rows(connection,
                 "SELECT target_id, kind, id, sha, status, finished_at FROM ("
                 "  SELECT target_id, 'stack' AS kind, id, commit_sha AS sha, status, finished_at "
                 "  FROM stack_deployments WHERE target_id = ANY(:ids) "
                 "  UNION ALL "
                 "  SELECT deployment_target_id, CASE WHEN trigger IS NULL THEN 'registry' "
                 "         ELSE 'delivery' END, id, commit_ref, status, finished_at "
                 "  FROM deployment_runs WHERE deployment_target_id = ANY(:ids)) x "
                 "WHERE finished_at IS NOT NULL AND finished_at >= :since "
                 "ORDER BY target_id, finished_at", {"ids": list(target_ids), "since": since})
    newest: dict[str, str] = {}
    for r in rows:
        if r["status"] in SUCCESS_STATES:
            newest[r["target_id"]] = f"{r['kind']}:{r['id']}"
    for r in rows:
        ref = f"{r['kind']}:{r['id']}"
        w = deploy_word(r["status"], newest_for_target=newest.get(r["target_id"]) == ref)
        out[r["target_id"]].append({"at": _iso(r["finished_at"]), "sha7": sha7(r["sha"]),
                                    "word": w["word"], "tone": w["tone"], "ref": ref})
    return out


_FAILURE_PATTERNS = (
    (re.compile(r"permission denied|publickey", re.I), "Host refused BOSS's key"),
    (re.compile(r"bound but unusable|does not exist in the secret|secret .*unusable", re.I),
     "Host key is missing"),
    (re.compile(r"port.*(never|did not).*open|port_timeout", re.I), "Port never opened"),
    (re.compile(r"health", re.I), "Health check failed"),
    (re.compile(r"docker", re.I), "Docker problem on host"),
    (re.compile(r"timed? ?out|timeout", re.I), "Timed out"),
    (re.compile(r"polic|blocked", re.I), "Blocked by policy"),
    (re.compile(r"worker stopped", re.I), "Deploy worker stopped"),
    (re.compile(r"manifest|boss\.deploy\.yaml", re.I), "Manifest problem"),
    (re.compile(r"budget|reservation", re.I), "Budget refused it"),
    (re.compile(r"unreachable|could not connect|connection refused|no route", re.I),
     "Host unreachable"),
)


_EXC_PREFIX = re.compile(r"^\s*(?:[A-Za-z_][\w.]*\.)?[A-Z]\w*(?:Error|Exception|Failed|Exit)\s*:\s*")


def error_in_words(value: Any) -> str:
    """A failure as a person reads it: the exception's sentence without its class name
    (`RuntimeError: transfer failed: …` → `Transfer failed: …`)."""
    text_ = _EXC_PREFIX.sub("", str(value or "")).strip()
    return text_[:1].upper() + text_[1:] if text_ else ""


def deploy_failure_line(row: dict[str, Any]) -> str:
    """≤ 6 words: why a deploy failed, from its error/failure_reason/failed step."""
    error = _json(row.get("error"))
    headline = error.get("headline") if isinstance(error, dict) else (error or None)
    reason = error_in_words(headline or row.get("failure_reason") or "")
    failed = next((s for s in (_json(row.get("steps")) or [])
                   if isinstance(s, dict) and step_state(s.get("status")) == "failed"), None)
    if failed and not reason:
        reason = error_in_words(failed.get("error"))
    first = re.split(r"(?<=[.!?])\s|\n", reason, maxsplit=1)[0].strip().rstrip(".")
    if first and len(first.split()) <= 6:
        return first
    for pattern, line in _FAILURE_PATTERNS:
        if pattern.search(reason):
            return line
    if failed:
        return " ".join(f"Failed at {_step_title(failed)}".split()[:6])
    if str(row.get("status") or "") == "blocked":
        return "Blocked by policy"
    return " ".join(first.split()[:6]) if first else "Deploy failed"


# ── Target drawer (§4.5) ──────────────────────────────────────────────────────────────────────

def target_kind_route(target: dict[str, Any]) -> Literal["stack", "app", "registry"]:
    """Which engine a target's one Deploy button goes to: a stack target plans a stack
    deployment; a target on a cloud account plans a registry run; anything else is a per-app
    delivery deploy."""
    kind = str(target.get("target_kind") or "").lower().replace("-", "_")
    if kind == "stack":
        return "stack"
    if target.get("provider_account_id") and kind not in LOCAL_KINDS:
        return "registry"
    return "app"


def target_detail(connection: Any, settings: Any, project_id: str, target_id: str) -> TargetDetail:
    partial: list[str] = []
    t = connection.execute(text(
        "SELECT t.*, h.name AS host_name, pa.account_label, pa.provider "
        "FROM deployment_targets t LEFT JOIN deployment_hosts h ON h.id = t.host_id "
        "LEFT JOIN provider_accounts pa ON pa.id = t.provider_account_id "
        "WHERE t.id = :i AND t.project_id = :p"), {"i": target_id, "p": project_id}
    ).mappings().first()
    if not t:
        raise LookupError("No such app target in this project.")
    t = dict(t)
    now = _now()
    health = _guard(connection, partial, "health",
                    lambda: health_states(connection, [target_id], now=now), {})
    strip_ = _guard(connection, partial, "health",
                    lambda: strips(connection, [target_id], now=now), {})
    cells = strip_.get(target_id) or [[0, 0] for _ in range(STRIP_CELLS)]
    n = sum(c[1] for c in cells)
    h = health.get(target_id) or {"word": "No signal", "tone": "hollow", "since": None}

    def recent() -> list[DeployRow]:
        params = _union_params(project_id)
        params["t"] = target_id
        raw = _rows(connection, _union_query("u.target_id = :t",
                                             "ORDER BY ts DESC, ref DESC LIMIT 5"), params)
        live = {r[0] for r in connection.execute(text(
            "WITH u AS (" + _UNION + ") SELECT DISTINCT ON (target_id) kind || ':' || id "
            "FROM u WHERE status = ANY(:success) AND target_id = :t "
            "ORDER BY target_id, COALESCE(finished_at, started_at, created_at) DESC"),
            {"p": project_id, "success": list(SUCCESS_STATES), "t": target_id})}
        return _shape_rows(connection, settings, project_id, raw, live_refs=live, viewer=None,
                           can_write=True)

    def secrets() -> list[dict[str, Any]]:
        refs = [r for r in (_json(t.get("secret_refs")) or []) if isinstance(r, dict)]
        ids = [str(r.get("secret_ref_id")) for r in refs if r.get("secret_ref_id")]
        known = {r["id"]: r for r in _rows(
            connection, "SELECT id, secret_kind, status FROM secret_refs WHERE id = ANY(:i)",
            {"i": ids})} if ids else {}
        return [{"name": str(r.get("name") or ""),
                 "kind": str((known.get(str(r.get("secret_ref_id"))) or {}).get("secret_kind")
                             or r.get("kind") or ""),
                 "status": str((known.get(str(r.get("secret_ref_id"))) or {}).get("status")
                               or "missing")} for r in refs]

    route = target_kind_route(t)
    is_community = community(settings)
    domains = _json(t.get("domains")) or []
    disabled: dict[str, str] = {}
    # A host target deploys in Community too: through a plan a person approves.
    if is_community and route != "stack" and not governed_host(dict(t)):
        disabled["deploy"] = PAUSED_IN_COMMUNITY
    if is_community:
        disabled["auto_deploy"] = PAUSED_IN_COMMUNITY
    retired = t.get("status") == "retired"
    body: dict[str, Any] = {
        "id": t["id"], "app": t["app_name"], "env": t["environment"],
        "kind": str(t.get("target_kind") or ""),
        "health": {"word": h["word"], "tone": h["tone"]}, "since": h.get("since"),
        "toggles": {"auto_deploy": bool(t.get("auto_deploy")), "paused": bool(t.get("paused"))},
        "where": {
            "host": ({"id": t["host_id"], "name": t["host_name"]} if t.get("host_id") else None),
            "repo": t.get("repo"), "ref": t.get("target_ref"),
            "manifest": t.get("manifest_path"),
            "domain": (domains[0] if domains else None), "public_url": t.get("public_url"),
            "account": ({"id": t["provider_account_id"], "name": t.get("account_label") or ""}
                        if t.get("provider_account_id") else None),
        },
        "strip": cells,
        "uptime_24h": (round(sum(c[0] for c in cells) / n, 4) if n else None),
        "recent": _guard(connection, partial, "deploys", recent, []),
        "secrets": _guard(connection, partial, "secrets", secrets, []),
        "can": {"deploy": not retired and "deploy" not in disabled,
                "auto_deploy": not retired and "auto_deploy" not in disabled,
                "move": not retired and route != "registry", "retire": not retired,
                "check": not retired},
        "disabled": disabled,
        "route": route,
        "partial": partial,
    }
    return _finish(body)  # type: ignore[return-value]


# ── Previews (§4.7) ───────────────────────────────────────────────────────────────────────────

def substrate(p: dict[str, Any]) -> str:
    if str(p.get("build_substrate") or "").lower() == "microvm":
        return "microVM"
    return {"docker": "Docker", "rootfs": "Rootfs"}.get(str(p.get("mode") or "").lower(),
                                                        "Process")


def preview_word(p: dict[str, Any]) -> Word:
    raw = str(p.get("status") or "")
    w = word("preview", raw)
    if raw == "failed" and p.get("failure_kind"):
        fk = WORDS["failure_kind"].get(str(p["failure_kind"]))
        label = fk["word"] if fk else str(p["failure_kind"]).replace("_", " ").capitalize()
        return {"word": f"Failed · {label}", "tone": "red"}
    return w


def preview_rows(connection: Any, settings: Any, project_id: str) -> PreviewList:
    partial: list[str] = []
    since = _now() - timedelta(hours=24)

    def read() -> list[dict[str, Any]]:
        return _rows(connection,
                     "SELECT id, run_id, repo, branch, port, url, mode, build_substrate, status, "
                     "       failure_kind, expires_at, created_at, last_seen_at "
                     "FROM app_previews WHERE project_id = :p "
                     "  AND (status IN ('running', 'starting') OR created_at >= :s) "
                     "ORDER BY created_at DESC LIMIT 200", {"p": project_id, "s": since})

    raw = _guard(connection, partial, "previews", read, [])
    # A preview restarts FROM ITS BUILD. When that run is gone (deleted, or a retired lineage),
    # Restart answered 404 "code task run not found"; it is offered only where it can work, and
    # says why where it cannot.
    run_ids = sorted({str(p["run_id"]) for p in raw if p.get("run_id")})
    builds = set(_guard(connection, partial, "builds", lambda: [r["id"] for r in _rows(
        connection, "SELECT id FROM code_task_runs WHERE id = ANY(:i)", {"i": run_ids})]
        if run_ids else [], [])) if run_ids else set()
    this = _guard(connection, partial, "this computer", lambda: _this_computer(
        connection, project_id, _rows(connection, "SELECT id, name, address, status FROM "
                                      "deployment_hosts WHERE project_id = :p",
                                      {"p": project_id}))[0], None)
    host = ({"id": this.get("host_id") or "this",
             "name": str(this.get("hostname") or "This computer")} if this
            else {"id": "this", "name": "This computer"})
    live, failed, ended = [], [], []
    for p in raw:
        status = str(p.get("status") or "")
        is_live = status in ("running", "starting")
        w = preview_word(p)
        row: PreviewRow = {
            "id": p["id"], "run_id": p.get("run_id"), "app": preview_app(p),
            "branch": p.get("branch"), "state": status, "word": w["word"], "tone": w["tone"],
            "substrate": substrate(p),  # type: ignore[typeddict-item]
            "port": p.get("port"), "host": host, "url": p.get("url"),
            "created_at": _iso(p.get("created_at")), "expires_at": _iso(p.get("expires_at")),
            "last_seen_at": _iso(p.get("last_seen_at")),
            "can": {"open": status == "running" and bool(p.get("url")), "log": True,
                    "stop": is_live, "restart": bool(p.get("run_id")) and p["run_id"] in builds},
            "restart_why": (None if not p.get("run_id") or p["run_id"] in builds
                            else "Its build no longer exists, so it cannot start again"),
        }
        if is_live:
            live.append(row)
        elif status == "failed":
            failed.append(row)
        else:
            ended.append(row)
    body: dict[str, Any] = {
        "counts": {"live": len(live), "failed_24h": len(failed), "ended_24h": len(ended)},
        "live": live, "failed_24h": failed, "ended_24h": ended, "partial": partial,
    }
    return _finish(body)  # type: ignore[return-value]


# ── Cloud (§4.8): one composition per sub-view ────────────────────────────────────────────────
# Row keys are the contract app/infra/cloud-rows.ts reads (apps/web/tests/_helpers/
# infra-cloud-fixtures.ts, stream S3). Money: `*_usd_micros` AND a ready `*_usd` string.

CLOUD_VIEWS = ("accounts", "resources", "capacity", "spend", "changes")
BYOK_TIP = "Community connects your own provider keys"
READ_ONLY_TIP = "You lack providerops_manage"

#: The resource plane's own states, in the one state language.
CLOUD_WORDS: dict[str, dict[str, Word]] = {
    "account": {"active": {"word": "Active", "tone": "green"},
                "disabled": {"word": "Disabled", "tone": "grey"},
                "revoked": {"word": "Revoked", "tone": "grey"},
                "error": {"word": "Failed", "tone": "red"},
                "pending": {"word": "Setting up", "tone": "accent"}},
    "test": {"ok": {"word": "Fresh", "tone": "green"}, "passed": {"word": "Fresh", "tone": "green"},
             "succeeded": {"word": "Fresh", "tone": "green"},
             "success": {"word": "Fresh", "tone": "green"},
             "healthy": {"word": "Fresh", "tone": "green"},
             "failed": {"word": "Failed", "tone": "red"}, "error": {"word": "Failed", "tone": "red"},
             "": {"word": "Untested", "tone": "hollow"}},
    "resource": {"healthy": {"word": "Up", "tone": "green"}, "up": {"word": "Up", "tone": "green"},
                 "ok": {"word": "Up", "tone": "green"}, "active": {"word": "Up", "tone": "green"},
                 "running": {"word": "Up", "tone": "green"},
                 "degraded": {"word": "Slow", "tone": "amber"},
                 "down": {"word": "Down", "tone": "red"}, "unhealthy": {"word": "Down", "tone": "red"},
                 "failed": {"word": "Down", "tone": "red"},
                 "unknown": {"word": "No signal", "tone": "hollow"},
                 "terminated": {"word": "Stopped", "tone": "grey"},
                 "expired": {"word": "Expired", "tone": "grey"}},
    "provider": {"ok": {"word": "OK", "tone": "green"}, "healthy": {"word": "OK", "tone": "green"},
                 "available": {"word": "OK", "tone": "green"},
                 "degraded": {"word": "High", "tone": "amber"},
                 "rate_limited": {"word": "High", "tone": "amber"},
                 "down": {"word": "Critical", "tone": "red"},
                 "unavailable": {"word": "Critical", "tone": "red"},
                 "error": {"word": "Critical", "tone": "red"},
                 "unknown": {"word": "No signal", "tone": "hollow"}},
    "recon": {"completed": {"word": "Done", "tone": "green"}, "succeeded": {"word": "Done", "tone": "green"},
              "running": {"word": "Running", "tone": "accent"},
              "failed": {"word": "Failed", "tone": "red"}},
    "severity": {"critical": {"word": "Critical", "tone": "red"},
                 "high": {"word": "High", "tone": "amber"},
                 "medium": {"word": "Medium", "tone": "amber"},
                 "low": {"word": "Low", "tone": "grey"}},
    "change": {"draft": {"word": "Draft", "tone": "hollow"},
               "policy_checked": {"word": "Planned", "tone": "hollow"},
               "budget_reserved": {"word": "Planned", "tone": "hollow"},
               "pending_approval": {"word": "Needs you", "tone": "amber"},
               "approved": {"word": "Approved", "tone": "accent"},
               "applying": {"word": "Applying", "tone": "accent"},
               "applied": {"word": "Applied", "tone": "green"},
               "failed": {"word": "Failed", "tone": "red"},
               "rejected": {"word": "Rejected", "tone": "grey"},
               "cancelled": {"word": "Cancelled", "tone": "grey"},
               "deadlettered": {"word": "Failed", "tone": "red"},
               "rolled_back": {"word": "Rolled back", "tone": "grey"}},
}
WORDS.update(CLOUD_WORDS)


def _w(kind: str, raw: Any) -> Word:
    if kind == "test" and not raw:
        return dict(CLOUD_WORDS["test"][""])  # type: ignore[return-value]
    return word(kind, raw)


def _money(prefix: str, micros: Any) -> dict[str, Any]:
    m = int(micros or 0)
    return {f"{prefix}_usd_micros": m, f"{prefix}_usd": usd(m)}


def _accounts_list(connection: Any, project_id: str) -> list[dict[str, str]]:
    return [{"id": r["id"], "name": r["account_label"]} for r in _rows(
        connection, "SELECT id, account_label FROM provider_accounts WHERE project_id = :p "
                    "ORDER BY provider, account_label", {"p": project_id})]


def _cloud_counts(connection: Any, project_id: str) -> dict[str, int]:
    r = connection.execute(text(
        "SELECT (SELECT count(*) FROM provider_accounts WHERE project_id = :p) AS accounts, "
        " (SELECT count(*) FROM external_resources WHERE project_id = :p) "
        " + (SELECT count(*) FROM infra_resources WHERE project_id = :p "
        "    AND status <> 'terminated') AS resources, "
        " (SELECT count(*) FROM worker_pools WHERE project_id = :p) AS pools, "
        " (SELECT count(DISTINCT provider) FROM cost_events WHERE project_id = :p "
        "    AND created_at >= now() - interval '30 days') AS providers, "
        " (SELECT count(*) FROM changesets WHERE project_id = :p) AS changes, "
        " (SELECT count(*) FROM cost_anomalies WHERE project_id = :p AND status = 'open') "
        "   AS anomalies"), {"p": project_id}).mappings().one()
    return {k: int(r[k] or 0) for k in ("accounts", "resources", "pools", "providers", "changes",
                                        "anomalies")}


def _catalog(profiles: Optional[dict[str, Any]], supported: Optional[dict[str, Any]]
             ) -> list[dict[str, Any]]:
    """Provider catalog for `+ Account`: the connection profiles (auth methods, slots) joined to
    the multi-cloud catalog (categories, skills). Static data; no secret."""
    from .multi_cloud_catalog import MULTI_CLOUD_PROVIDERS

    multi = {p["provider_key"]: p for p in MULTI_CLOUD_PROVIDERS}
    keys = sorted(set(profiles or {}) | set(multi))
    out = []
    for key in keys:
        prof = (profiles or {}).get(key) or {}
        mc = multi.get(key) or {}
        skills = list(mc.get("infra_skills") or []) + list(mc.get("model_skills") or [])
        if not skills:
            skills = list(((supported or {}).get(key) or {}).get("capabilities") or [])
        methods = []
        for mkey, method in (prof.get("auth_methods") or {}).items():
            methods.append({"key": mkey, "available": method.get("runtime_status") == "available",
                            "slots": [{"key": s["key"], "required": bool(s.get("required"))}
                                      for s in method.get("credential_slots") or []]})
        out.append({
            "provider": key,
            "name": prof.get("display_name") or mc.get("display_name") or key,
            "category": prof.get("category") or ((mc.get("categories") or ["infrastructure"])[0]),
            "adapter": str(mc.get("adapter") or prof.get("adapter") or key),
            "adapter_status": ("available" if (prof.get("adapter_status")
                                               or mc.get("adapter_status")) == "available"
                               else "planned"),
            "categories": list(mc.get("categories") or [prof.get("category") or "infrastructure"]),
            "skills": skills[:5], "skill_count": len(skills),
            "pasteable": any(m["slots"] for m in methods),
            "base_url": any(s["key"] in ("base_url", "endpoint") for m in methods
                            for s in m["slots"]),
            "auth_methods": methods,
        })
    return out


def _cloud_accounts(connection: Any, project_id: str, since: datetime, *, can_write: bool,
                    profiles: Optional[dict[str, Any]]) -> dict[str, Any]:
    rows = _rows(connection,
                 "SELECT a.*, "
                 "  (SELECT count(*) FROM external_resources r "
                 "   WHERE r.provider_account_id = a.id) AS n_resources, "
                 "  (SELECT COALESCE(sum(c.amount_usd_micros), 0) FROM cost_events c "
                 "   WHERE c.provider_account_id = a.id AND c.created_at >= :s) AS spend, "
                 "  (SELECT count(*) FROM changesets cs WHERE cs.provider_account_id = a.id "
                 "   AND cs.status IN ('draft', 'policy_checked', 'budget_reserved', "
                 "   'pending_approval', 'approved', 'applying')) AS open_changes, "
                 "  (SELECT count(*) FROM resource_receipts rr "
                 "   WHERE rr.provider_account_id = a.id) AS n_receipts "
                 "FROM provider_accounts a WHERE a.project_id = :p "
                 "ORDER BY a.provider, a.account_label", {"p": project_id, "s": since})
    ids = [r["id"] for r in rows]
    events: dict[str, list] = {i: [] for i in ids}
    receipts: dict[str, list] = {i: [] for i in ids}
    if ids:
        for e in _rows(connection,
                       "SELECT * FROM (SELECT id, entity_id, event_type, actor, created_at, "
                       "  row_number() OVER (PARTITION BY entity_id ORDER BY created_at DESC) n "
                       "  FROM events WHERE project_id = :p AND entity_id = ANY(:i)) x "
                       "WHERE n <= 4 ORDER BY created_at DESC", {"p": project_id, "i": ids}):
            events[e["entity_id"]].append({"id": e["id"], "kind": e["event_type"],
                                           "actor": e["actor"], "at": _iso(e["created_at"])})
        for r in _rows(connection,
                       "SELECT * FROM (SELECT id, provider_account_id, action, status, created_at, "
                       "  row_number() OVER (PARTITION BY provider_account_id "
                       "                     ORDER BY created_at DESC) n "
                       "  FROM resource_receipts WHERE provider_account_id = ANY(:i)) x "
                       "WHERE n <= 4 ORDER BY created_at DESC", {"i": ids}):
            receipts[r["provider_account_id"]].append({"id": r["id"], "action": r["action"],
                                                       "status": r["status"],
                                                       "at": _iso(r["created_at"])})
    catalog = {c["provider"]: c for c in _catalog(profiles, None)}
    out = []
    for r in rows:
        status = str(r["status"] or "")
        live = status == "active"
        refs = _json(r["credential_refs"]) or {}
        prof = catalog.get(r["provider"]) or {}
        method = next((m for m in prof.get("auth_methods") or []
                       if m["key"] == r["auth_method"]), None)
        err = _json(r["last_error"])
        if isinstance(err, dict):
            err = err.get("message") or err.get("error") or err.get("detail")
        authority = ("Read only" if r["read_only"] else
                     "Deploy capable" if r["deploy_capable"] else "Custom")
        out.append({
            "id": r["id"], "provider": r["provider"],
            "provider_name": prof.get("name") or r["provider"], "label": r["account_label"],
            "state": _w("account", status),
            "credential": {"slots": sorted(refs) if isinstance(refs, dict) else [],
                           "version": r["credential_version"]},
            "last_test": {**_w("test", r["last_test_status"]), "at": _iso(r["last_tested_at"])},
            "authority": authority,
            "destructive_blocked": bool(r["destructive_actions_disabled"]),
            "resources": int(r["n_resources"] or 0), **_money("spend_30d", r["spend"]),
            "owner": r["owner_actor"], "scopes": list(_json(r["scopes"]) or [])[:12],
            "flags": {"read_only": bool(r["read_only"]),
                      "billing_readable": bool(r["billing_readable"]),
                      "deploy_capable": bool(r["deploy_capable"]),
                      "destructive_actions_disabled": bool(r["destructive_actions_disabled"])},
            "impact": {"resources": int(r["n_resources"] or 0), **_money("spend", r["spend"]),
                       "open_changes": int(r["open_changes"] or 0),
                       "receipts": int(r["n_receipts"] or 0)},
            "last_error": (str(err)[:200] if err else None),
            "rotation_slots": (method or {}).get("slots") or [
                {"key": k, "required": True} for k in (sorted(refs) if isinstance(refs, dict)
                                                       else [])],
            "events": events.get(r["id"], []), "receipts": receipts.get(r["id"], []),
            "can": {"manage": can_write, "test": can_write and live,
                    "sync": can_write and live, "enable": can_write and status == "disabled",
                    "disable": can_write and live,
                    "rotate": can_write and status != "revoked",
                    "revoke": can_write and status != "revoked"},
        })
    return {"rows": out, "filters": {},
            "header": {"accounts": [{"id": r["id"], "name": r["account_label"]} for r in rows],
                       "catalog": list(catalog.values())}}


def _ttl(minutes: Any) -> Optional[str]:
    if not minutes:
        return None
    m = int(minutes)
    if m % 1440 == 0:
        return f"{m // 1440}d"
    if m % 60 == 0:
        return f"{m // 60}h"
    return f"{m}m"


def _cloud_resources(connection: Any, project_id: str, filters: dict[str, str]) -> dict[str, Any]:
    where, params = ["r.project_id = :p"], {"p": project_id}
    for key, col in (("provider", "r.provider"), ("account", "r.provider_account_id"),
                     ("env", "r.environment"), ("service", "r.resource_type"),
                     ("region", "COALESCE(r.metadata->>'region', '')")):
        if filters.get(key):
            where.append(f"{col} = :{key}")
            params[key] = filters[key]
    ext = _rows(connection,
                "SELECT r.*, a.account_label FROM external_resources r "
                "LEFT JOIN provider_accounts a ON a.id = r.provider_account_id "
                f"WHERE {' AND '.join(where)} ORDER BY r.last_seen_at DESC LIMIT 200", params)
    # AN APP ON ONE OF OUR HOSTS IS SAID LIKE ONE. A governed host deploy records its app as a
    # resource with provider `ssh_host` and type `ssh_host.app`, which the drawer printed as its
    # provider and "region", and its health was the word written at deploy time ("Up") while
    # Observability, reading the probes, said Down. It is placed on its host by name now, and its
    # health is the same verdict Observability shows (health_states over the target's probes).
    host_apps: dict[str, str] = {}
    for r in ext:
        ext_id = str(r.get("external_id") or "")
        if r["provider"] == "ssh_host" and ext_id.split(":", 1)[0] in ("app", "stack"):
            host_apps[r["id"]] = ext_id.split(":")[1]
    placed = {t["id"]: t for t in _rows(
        connection, "SELECT t.id, h.name AS host_name FROM deployment_targets t "
                    "LEFT JOIN deployment_hosts h ON h.id = t.host_id WHERE t.id = ANY(:i)",
        {"i": sorted(set(host_apps.values()))})} if host_apps else {}
    probed = health_states(connection, sorted(placed)) if placed else {}
    rows = []
    for r in ext:
        meta = _json(r["metadata"]) or {}
        target_id = host_apps.get(r["id"])
        on_host = target_id is not None
        rows.append({
            "id": r["id"], "kind": "Resource", "name": r["name"], "provider": r["provider"],
            "provider_name": "SSH host" if on_host else None,
            "account": ({"id": r["provider_account_id"], "name": r["account_label"]}
                        if r["provider_account_id"] else None),
            "region": ((placed.get(target_id) or {}).get("host_name") or "Unplaced") if on_host
            else meta.get("region"),
            "service": ("App on a host" if on_host and str(r["resource_type"]).endswith(".app")
                        else "Stack on a host" if on_host else r["resource_type"]),
            "env": r["environment"], "owner": r["owner_role"],
            "health": ({"word": probed[target_id]["word"], "tone": probed[target_id]["tone"]}
                       if on_host and target_id in probed
                       else _w("resource", r["health_status"])),
            **_money("cost_month", r["actual_monthly_usd_micros"]
                     if r["actual_monthly_usd_micros"] is not None
                     else r["estimated_monthly_usd_micros"]),
            "last_seen_at": _iso(r["last_seen_at"]), "ttl": None, "expires_at": None,
            "external_id": r["external_id"],
            "links": {"build": meta.get("run_id") or meta.get("code_task_run_id"),
                      "deploy": meta.get("deploy_ref"),
                      "budget": r["budget_allocation_id"], "receipt": r["receipt_id"]},
            "droplet": None,
        })
    if not any(filters.get(k) for k in ("account", "env", "service")) \
            or filters.get("service") == "droplets":
        dwhere, dparams = ["project_id = :p", "status <> 'terminated'"], {"p": project_id}
        if filters.get("provider"):
            dwhere.append("provider = :provider")
            dparams["provider"] = filters["provider"]
        if filters.get("region"):
            dwhere.append("region = :region")
            dparams["region"] = filters["region"]
        for d in _rows(connection, "SELECT * FROM infra_resources "
                                   f"WHERE {' AND '.join(dwhere)} ORDER BY created_at DESC "
                                   "LIMIT 100", dparams):
            meta = _json(d["metadata"]) or {}
            rows.append({
                "id": d["id"], "kind": "Droplet", "name": d["resource_name"],
                "provider": d["provider"], "account": None, "region": d["region"],
                "service": "droplets", "env": meta.get("environment"), "owner": None,
                "health": _w("resource", d["status"]),
                **_money("cost_month", meta.get("monthly_usd_micros") or 0),
                "last_seen_at": _iso(d["updated_at"]), "ttl": _ttl(d["ttl_minutes"]),
                "expires_at": _iso(d["expires_at"]), "external_id": d["external_resource_id"],
                "links": {"build": meta.get("run_id"), "deploy": None, "budget": None,
                          "receipt": d["provision_receipt_id"]},
                "droplet": {"status": d["status"], "size": d["size"],
                            "boundary": d["data_boundary"],
                            "provider_ref": f"{d['provider']}:{d['external_resource_id']}",
                            "termination": (d["termination_reason"] or
                                            ("on expiry" if d["expires_at"] else None))},
            })
    facets = _rows(connection,
                   "SELECT DISTINCT r.provider, r.provider_account_id, r.environment, "
                   "       r.resource_type, r.metadata->>'region' AS region "
                   "FROM external_resources r WHERE r.project_id = :p "
                   "UNION SELECT DISTINCT provider, NULL, NULL, 'droplets', region "
                   "FROM infra_resources WHERE project_id = :p AND status <> 'terminated'",
                   {"p": project_id})
    return {"rows": rows,
            "filters": {"provider": sorted({f["provider"] for f in facets if f["provider"]}),
                        "account": sorted({f["provider_account_id"] for f in facets
                                           if f["provider_account_id"]}),
                        "env": sorted({f["environment"] for f in facets if f["environment"]}),
                        "region": sorted({f["region"] for f in facets if f["region"]}),
                        "service": sorted({f["resource_type"] for f in facets
                                           if f["resource_type"]})},
            "header": {"accounts": _accounts_list(connection, project_id)}}


def _plan_word(p: dict[str, Any]) -> Word:
    a, b = int(p.get("desired_workers_before") or 0), int(p.get("desired_workers_after") or 0)
    if b > a:
        return {"word": "Scale up", "tone": "amber"}
    if b < a:
        return {"word": "Scale down", "tone": "accent"}
    return {"word": "Hold", "tone": "grey"}


def _cloud_capacity(connection: Any, settings: Any, project_id: str, partial: list[str]
                    ) -> dict[str, Any]:
    plans = _rows(connection,
                  "SELECT * FROM scale_plans WHERE project_id = :p "
                  "ORDER BY created_at DESC LIMIT 100", {"p": project_id})
    by_pool: dict[str, list] = {}
    for p in plans:
        by_pool.setdefault(str(p["worker_pool_id"]), []).append(p)
    pools = _rows(connection, "SELECT * FROM worker_pools WHERE project_id = :p "
                              "ORDER BY pool_key", {"p": project_id})
    history = _guard(connection, partial, "autoscaling history", lambda: _rows(
        connection,
        "SELECT pool_key, previous_desired, desired_workers, reason, action, created_at "
        "FROM worker_autoscaling_history WHERE project_id = :p "
        "ORDER BY created_at DESC LIMIT 200", {"p": project_id}), [])
    hist: dict[str, list] = {}
    for h in history:
        hist.setdefault(h["pool_key"], []).append(h)
    rows = []
    for p in pools:
        mine = by_pool.get(p["id"]) or []
        latest = mine[0] if mine else None
        rows.append({
            "id": p["id"], "pool_key": p["pool_key"], "kind": p["kind"],
            "desired": p["desired_workers"], "actual": p["current_workers"],
            "min": p["min_workers"], "max": p["max_workers"],
            "queue_per_worker": p["max_queue_depth_per_worker"],
            **_money("cost_hour", p["cost_per_worker_hour_usd_micros"]),
            "plan": ({**_plan_word(latest), "summary": str(latest["trigger_reason"] or "")[:120],
                      "at": _iso(latest["created_at"])} if latest else None),
            "history": [{"at": _iso(h["created_at"]), "from": h["previous_desired"],
                         "to": h["desired_workers"],
                         "reason": str(h["reason"] or h["action"] or "")[:120]}
                        for h in (hist.get(p["pool_key"]) or [])[:8]],
            "plans": [{"id": x["id"], "at": _iso(x["created_at"]),
                       "action": (_plan_word(x)["word"].lower().replace(" ", "_")),
                       "desired": x["desired_workers_after"],
                       "reason": str(x["trigger_reason"] or "")[:120]} for x in mine[:4]],
        })
    queue = _guard(connection, partial, "queue", lambda: dict(connection.execute(text(
        "SELECT count(*) FILTER (WHERE status IN ('queued', 'failed_retryable') "
        "                        AND run_after <= now()) AS depth, "
        "       count(*) FILTER (WHERE status = 'leased') AS leased, "
        "       count(*) FILTER (WHERE status = 'running') AS running "
        "FROM jobs WHERE project_id = :p"), {"p": project_id}).mappings().one()),
        {"depth": 0, "leased": 0, "running": 0})
    health = _guard(connection, partial, "provider health", lambda: _rows(
        connection,
        "SELECT DISTINCT ON (provider) provider, status, api_available, rate_limit_status, "
        "       usage_cap_status, last_checked_at FROM provider_health WHERE project_id = :p "
        "ORDER BY provider, last_checked_at DESC", {"p": project_id}), [])
    fabric = _guard(connection, partial, "fabric", lambda: _rows(
        connection,
        "SELECT kind, status, last_heartbeat_at, "
        "       COALESCE((metadata->>'simulated')::boolean, false) AS simulated "
        "FROM worker_instances WHERE project_id = :p", {"p": project_id}), [])
    active = [i for i in fabric if i["status"] not in ("terminated", "failed")]
    by_kind: dict[str, int] = {}
    for i in active:
        by_kind[i["kind"]] = by_kind.get(i["kind"], 0) + 1
    return {
        "rows": rows, "filters": {},
        "header": {
            "accounts": _accounts_list(connection, project_id),
            "queue": {"depth": int(queue["depth"] or 0), "leased": int(queue["leased"] or 0),
                      "running": int(queue["running"] or 0),
                      "real_provision": bool(getattr(settings, "infra_real_provision_allowed",
                                                     False))},
            "api_health": [{"provider": h["provider"], "state": _w("provider", h["status"]),
                            "api": "available" if h["api_available"] else "unavailable",
                            "rate_limit": h["rate_limit_status"],
                            "usage_cap": h["usage_cap_status"],
                            "checked_at": _iso(h["last_checked_at"])} for h in health],
            "fabric": {"pools": len(pools), "instances": len(active),
                       "real_host_effects": sum(1 for i in active
                                                if i["status"] == "running" and not i["simulated"]
                                                and i["last_heartbeat_at"]),
                       "by_kind": by_kind},
        },
    }


_ESTIMATED = ("estimated", "estimate", "forecast", "projected")


def _cloud_spend(connection: Any, project_id: str, since: datetime) -> dict[str, Any]:
    per = _rows(connection,
                "SELECT provider, COALESCE(sum(amount_usd_micros), 0) AS spend, count(*) AS events, "
                "  bool_or(confidence ILIKE ANY(:est)) AS estimated, "
                "  count(*) FILTER (WHERE receipt_id IS NOT NULL) AS ledger "
                "FROM cost_events WHERE project_id = :p AND created_at >= :s "
                "GROUP BY provider ORDER BY 2 DESC, 1",
                {"p": project_id, "s": since, "est": [f"%{e}%" for e in _ESTIMATED]})
    cats = _rows(connection,
                 "SELECT provider, category, COALESCE(sum(amount_usd_micros), 0) AS spend "
                 "FROM cost_events WHERE project_id = :p AND created_at >= :s "
                 "GROUP BY 1, 2 ORDER BY 3 DESC", {"p": project_id, "s": since})
    recent = _rows(connection,
                   "SELECT * FROM (SELECT id, provider, category, amount_usd_micros, confidence, "
                   "  created_at, row_number() OVER (PARTITION BY provider "
                   "                                 ORDER BY created_at DESC) n "
                   "  FROM cost_events WHERE project_id = :p AND created_at >= :s) x "
                   "WHERE n <= 5", {"p": project_id, "s": since})
    anomalies = _rows(connection,
                      "SELECT id, provider, severity, status, summary FROM cost_anomalies "
                      "WHERE project_id = :p ORDER BY (status = 'open') DESC, created_at DESC "
                      "LIMIT 25", {"p": project_id})
    recon = _rows(connection,
                  "SELECT * FROM (SELECT id, provider, status, started_at, resources_seen, "
                  "  costs_seen, row_number() OVER (PARTITION BY provider "
                  "                                 ORDER BY started_at DESC) n "
                  "  FROM reconciliation_runs WHERE project_id = :p) x WHERE n <= 3",
                  {"p": project_id})
    total = sum(int(r["spend"] or 0) for r in per)
    open_by: dict[str, int] = {}
    for a in anomalies:
        if a["status"] == "open":
            open_by[a["provider"]] = open_by.get(a["provider"], 0) + 1
    rows = []
    for r in per:
        prov = r["provider"]
        spend = int(r["spend"] or 0)
        rows.append({
            "id": prov, "provider": prov, **_money("spend_30d", spend),
            "share": (round(spend / total, 4) if total else 0.0),
            "events": int(r["events"]), "anomalies": open_by.get(prov, 0),
            "estimated": bool(r["estimated"]),
            # Spend that ran entirely on a plan: events recorded, nothing metered.
            "included": int(r["events"]) > 0 and spend == 0,
            "categories": [{"name": c["category"], "usd_micros": int(c["spend"] or 0),
                            "usd": usd(c["spend"])} for c in cats if c["provider"] == prov][:8],
            "cost_events": [{"id": e["id"], "category": e["category"],
                             "usd_micros": int(e["amount_usd_micros"] or 0),
                             "usd": usd(e["amount_usd_micros"]), "confidence": e["confidence"],
                             "at": _iso(e["created_at"])}
                            for e in recent if e["provider"] == prov],
            "reconciliation": [{"id": x["id"], "state": _w("recon", x["status"]),
                                "at": _iso(x["started_at"]),
                                "resources_seen": x["resources_seen"],
                                "costs_seen": x["costs_seen"]}
                               for x in recon if x["provider"] == prov],
            "usage": {"events": int(r["events"]), "ledger": int(r["ledger"] or 0),
                      "billing_mirror": None},
        })
    return {"rows": rows, "filters": {},
            "header": {**_money("total_30d", total),
                       "anomalies": [{"id": a["id"], "provider": a["provider"],
                                      "severity": _w("severity", a["severity"]),
                                      "summary": a["summary"], "status": a["status"]}
                                     for a in anomalies]}}


def _cloud_changes(connection: Any, project_id: str, *, can_write: bool) -> dict[str, Any]:
    from . import decision_actions

    rows = _rows(connection,
                 "SELECT c.*, a.account_label, x.status AS decision_status, "
                 "       x.recommendation->>'action' AS decision_action "
                 "FROM changesets c "
                 "LEFT JOIN provider_accounts a ON a.id = c.provider_account_id "
                 "LEFT JOIN decisions x ON x.id = c.approval_decision_id "
                 "WHERE c.project_id = :p ORDER BY c.created_at DESC LIMIT 50", {"p": project_id})
    ids = [r["id"] for r in rows]
    receipts: dict[str, list] = {}
    if ids:
        for x in _rows(connection,
                       "SELECT id, changeset_id, action, status FROM resource_receipts "
                       "WHERE changeset_id = ANY(:i) ORDER BY created_at DESC", {"i": ids}):
            receipts.setdefault(x["changeset_id"], []).append(
                {"id": x["id"], "action": x["action"], "status": x["status"]})
    drift: dict[Any, list] = {}
    for d in _rows(connection,
                   "SELECT id, provider_account_id, classification, summary, unmanaged_count, "
                   "  orphaned_count, security_impacting_count FROM drift_reports "
                   "WHERE project_id = :p ORDER BY created_at DESC LIMIT 50", {"p": project_id}):
        drift.setdefault(d["provider_account_id"], []).append(
            {"id": d["id"], "classification": d["classification"],
             "summary": str(d["summary"] or "")[:120], "unmanaged": d["unmanaged_count"],
             "orphaned": d["orphaned_count"], "security": d["security_impacting_count"]})
    out = []
    for r in rows:
        ops = [o for o in (_json(r["operations"]) or []) if isinstance(o, dict)]
        raw_json = json.dumps(ops)
        status = str(r["status"] or "")
        via = (r["approval_decision_id"]
               if r["decision_action"] in decision_actions.CARRIED_OUT_ON_APPROVAL
               and r["decision_action"] != "approve_changeset" else None)
        out.append({
            "id": r["id"], "title": r["title"], "summary": str(r["summary"] or "")[:200],
            "account": ({"id": r["provider_account_id"], "name": r["account_label"]}
                        if r["provider_account_id"] else None),
            "risk": r["risk_tier"], **_money("cost", r["expected_cost_usd_micros"]),
            "state": _w("change", status), "created_at": _iso(r["created_at"]),
            "operations": [{"action": o.get("action"), "resource_type": o.get("resource_type"),
                            "resource_ref": o.get("resource_ref") or o.get("resource_id"),
                            "destructive": bool(o.get("destructive"))} for o in ops[:8]],
            # Show JSON: whole when it fits the row budget, else cut and marked.
            "operations_json": raw_json if len(raw_json) <= 600 else raw_json[:599] + "…",
            "rollback_plan": [str(x)[:120] for x in (_json(r["rollback_plan"]) or [])][:5],
            "receipts": (receipts.get(r["id"]) or [])[:4],
            "drift": (drift.get(r["provider_account_id"]) or [])[:3]
            if r["provider_account_id"] else [],
            "can": {"approve": can_write and status == "pending_approval" and not via,
                    "apply": can_write and status == "approved" and not via,
                    # ONE DOOR. A deploy's ChangeSet is approved through its decision — the
                    # one the deploy drawer and Approvals answer — and applied by BOSS's
                    # worker; the raw ChangeSet routes refuse it.
                    "approve_deploy": bool(via) and can_write and status == "pending_approval"
                    and r["decision_status"] == "pending"},
            "decision_id": r["approval_decision_id"] if via else None,
        })
    return {"rows": out, "filters": {},
            "header": {"accounts": _accounts_list(connection, project_id)}}


def cloud(connection: Any, settings: Any, project_id: str, view: str, *,
          viewer: Optional[str] = None, filters: Optional[dict[str, str]] = None,
          profiles: Optional[dict[str, Any]] = None) -> CloudBody:
    """`profiles` (additive): the resource plane's provider connection profiles, passed by the
    API (this module must not import resource_plane, which loads the SSH adapter)."""
    view = view if view in CLOUD_VIEWS else "accounts"
    partial: list[str] = []
    since = _now() - timedelta(days=30)
    can_write = viewer_can_write(connection, settings, project_id, viewer,
                                 catalogue_permission="cloud_account:manage")
    empty: dict[str, Any] = {"rows": [], "filters": {}, "header": {}}
    if view == "accounts":
        part = _guard(connection, partial, "accounts", lambda: _cloud_accounts(
            connection, project_id, since, can_write=can_write, profiles=profiles), empty)
    elif view == "resources":
        part = _guard(connection, partial, "resources",
                      lambda: _cloud_resources(connection, project_id, filters or {}), empty)
    elif view == "capacity":
        part = _guard(connection, partial, "capacity",
                      lambda: _cloud_capacity(connection, settings, project_id, partial), empty)
    elif view == "spend":
        part = _guard(connection, partial, "spend",
                      lambda: _cloud_spend(connection, project_id, since), empty)
    else:
        part = _guard(connection, partial, "changes",
                      lambda: _cloud_changes(connection, project_id, can_write=can_write), empty)
    counts = _guard(connection, partial, "counts", lambda: _cloud_counts(connection, project_id),
                    {k: 0 for k in ("accounts", "resources", "pools", "providers", "changes",
                                    "anomalies")})
    notices = []
    if not can_write:
        notices.append({"key": "read_only", "tip": READ_ONLY_TIP})
    if community(settings):
        notices.append({"key": "byok", "tip": BYOK_TIP})
    body: dict[str, Any] = {"view": view, "counts": counts, "rows": part.get("rows") or [],
                            "filters": part.get("filters") or {},
                            "header": part.get("header") or {}, "notices": notices,
                            "partial": partial}
    return _finish(body)  # type: ignore[return-value]


# ── Attention (§2.2) ──────────────────────────────────────────────────────────────────────────

def _infra_item(*, id_: str, infra_kind: str, title: str, detail: str, area: str, open_: str,
                answers: list[dict[str, Any]], at: Any) -> dict[str, Any]:
    return {"id": id_, "kind": "infra", "infra_kind": infra_kind, "decision_kind": None,
            "decision_id": None, "title": title, "detail": detail, "what_to_do": None,
            "role_key": None, "run": None, "actions": [], "answers": answers,
            "decide_inline": False, "content_hash": None, "preview": None, "risk_tier": None,
            "fix": None, "at": _iso(at), "area": area, "open": open_}


def deploy_href(ref: str) -> str:
    return f"?area=infra&view=deploys&deploy={ref}"


def _needs_you_count(items: list[dict[str, Any]]) -> int:
    from .command_center import attention_counts

    return attention_counts(items)["needs_you"]


def _approvals(connection: Any, project_id: str, now: datetime) -> list[dict[str, Any]]:
    from . import agents_live_board, decision_actions

    # EVERY DEPLOY WAITING ON A PERSON: a stack deployment's decision and a governed host
    # deployment's (governed_host_deploy.py). Whether its decision is open is decided below by
    # decision_actions (the inbox's rule), never by a status list here. Listing only the first left a host deploy out of
    # Needs you and its count while Approvals and the badge showed it.
    rows = _rows(connection,
                 "SELECT 'stack' AS kind, s.id AS deployment_id, s.commit_sha, s.ref, "
                 "       t.app_name, t.environment, "
                 "       d.id, d.task_id, d.question, d.status, d.risk_tier, d.default_action, "
                 "       d.recommendation, d.deadline_at, d.created_at, d.delegated_to "
                 "FROM stack_deployments s JOIN decisions d ON d.id = s.decision_id "
                 "JOIN deployment_targets t ON t.id = s.target_id "
                 "WHERE s.project_id = :p AND s.status = 'awaiting_approval' "
                 "UNION ALL "
                 "SELECT 'delivery', r.id, r.commit_ref, r.commit_ref, t.app_name, t.environment, "
                 "       d.id, d.task_id, d.question, d.status, d.risk_tier, d.default_action, "
                 "       d.recommendation, d.deadline_at, d.created_at, d.delegated_to "
                 "FROM deployment_runs r JOIN decisions d ON d.id = r.decision_id "
                 "JOIN deployment_targets t ON t.id = r.deployment_target_id "
                 "WHERE r.project_id = :p AND r.status = 'awaiting_approval' "
                 "ORDER BY created_at DESC", {"p": project_id})
    needs, decisions, extra = [], [], {}
    for r in rows:
        decision = {k: r.get(k) for k in ("id", "task_id", "question", "status", "risk_tier",
                                          "default_action", "recommendation", "deadline_at",
                                          "created_at", "delegated_to")}
        decision["recommendation"] = _json(decision.get("recommendation")) or {}
        described = decision_actions.describe(decision, now)
        if described["attention"] != "needs_action":
            continue
        decisions.append(decision)
        short = sha7(r["commit_sha"])
        named = r["ref"] if r["ref"] and r["ref"] != r["commit_sha"] or not short else None
        needs.append({
            "id": str(r["id"]), "kind": "approval", "decision_kind": described["kind"],
            "title": f"Deploy {r['app_name']} to {r['environment']}?",
            "detail": " · ".join(x for x in (short, f"from {named}" if named else None) if x),
            "what_to_do": None, "decision_id": r["id"],
            "allowed_actions": [dict(a) for a in described["allowed_actions"]],
            "risk_tier": r["risk_tier"], "at": _iso(r["created_at"]),
        })
        extra[str(r["id"])] = f"{r['kind']}:{r['deployment_id']}"
    items = agents_live_board.attention_items(connection, project_id, needs, decisions)
    for item in items:
        item["area"] = "infra"  # type: ignore[typeddict-unknown-key]
        item["infra_kind"] = "deploy_approval"  # type: ignore[typeddict-unknown-key]
        item["open"] = deploy_href(extra[item["id"]])  # type: ignore[typeddict-unknown-key]
    return items  # type: ignore[return-value]


def _failed_deploys(connection: Any, settings: Any, project_id: str) -> list[dict[str, Any]]:
    params = _union_params(project_id)
    newest = _rows(connection,
                   "WITH u AS (" + _UNION + ") "
                   "SELECT DISTINCT ON (u.target_id) u.*, u.kind || ':' || u.id AS ref, "
                   "       COALESCE(u.finished_at, u.started_at, u.created_at) AS ts, "
                   "       t.app_name, t.environment, t.target_kind "
                   "FROM u JOIN deployment_targets t ON t.id = u.target_id "
                   "WHERE t.status <> 'retired' "
                   "ORDER BY u.target_id, COALESCE(u.finished_at, u.started_at, u.created_at) DESC",
                   params)
    failed = [r for r in newest if r["status"] == "failed"]
    if not failed:
        return []
    tids = [r["target_id"] for r in failed]
    stack_hist = _stack_history(connection, tids)
    run_hist = _run_history(connection, tids)
    is_community = community(settings)
    out = []
    for r in failed:
        can = _deploy_can({**r, "decision_status": None}, can_write=True,
                          is_community=is_community, stack_hist=stack_hist, run_hist=run_hist)
        answers = ([{"key": "rollback", "label": "Roll back", "enabled": True}]
                   if can["rollback"] else []) + [{"key": "open", "label": "Open",
                                                   "enabled": True}]
        detail = " · ".join(x for x in (r["environment"], sha7(r["sha"]),
                                        deploy_failure_line(r)) if x)
        out.append(_infra_item(id_=f"infra:deploy_failed:{r['target_id']}",
                               infra_kind="deploy_failed",
                               title=f"{r['app_name']} deploy failed", detail=detail,
                               area="infra", open_=deploy_href(r["ref"]), answers=answers,
                               at=r["ts"]))
    return out


def _apps_down(connection: Any, project_id: str, now: datetime) -> list[dict[str, Any]]:
    targets = _rows(connection, "SELECT id, app_name, environment FROM deployment_targets "
                    "WHERE project_id = :p AND status <> 'retired'", {"p": project_id})
    states = health_states(connection, [t["id"] for t in targets], now=now)
    out = []
    for t in targets:
        h = states.get(t["id"]) or {}
        if h.get("status") != "down":
            continue
        out.append(_infra_item(id_=f"infra:app_down:{t['id']}", infra_kind="app_down",
                               title=f"{t['app_name']} is down", detail=t["environment"],
                               area="observability",
                               open_=f"?area=observability&view=apps&app={t['id']}",
                               answers=[{"key": "open", "label": "Open", "enabled": True}],
                               at=h.get("since")))
    return out


def _apps_erroring(connection: Any, project_id: str) -> list[dict[str, Any]]:
    """An open error-rate incident (app_metrics' alert) is a card on Observability. Read from
    the incidents ledger the alert writes — never scraped on a poll."""
    rows = _rows(connection,
                 "SELECT i.id AS incident_id, i.title, i.first_seen, t.id, t.app_name, "
                 "       t.environment FROM incidents i "
                 "JOIN deployment_targets t ON i.dedupe_key = 'app_error_rate:' || t.id "
                 "WHERE i.project_id = :p AND i.dedupe_key LIKE 'app_error_rate:%' "
                 "AND i.status NOT IN ('resolved', 'dismissed') AND t.status <> 'retired'",
                 {"p": project_id})
    return [_infra_item(id_=f"infra:app_errors:{r['id']}", infra_kind="app_errors",
                        title=f"{r['app_name']} requests failing", detail=r["title"],
                        area="observability",
                        open_=f"?area=observability&view=apps&app={r['id']}",
                        answers=[{"key": "open", "label": "Open", "enabled": True}],
                        at=r["first_seen"]) for r in rows]


def _hosts_unreachable(connection: Any, project_id: str) -> list[dict[str, Any]]:
    hosts = [_host_ready(h) for h in _rows(
        connection,
        "SELECT h.*, (SELECT count(*) FROM deployment_targets t WHERE t.host_id = h.id "
        "             AND t.status <> 'retired') AS apps "
        "FROM deployment_hosts h WHERE h.project_id = :p AND h.status <> 'retired'",
        {"p": project_id})]
    out = []
    for h in hosts:
        if not _host_unreachable(h) or not int(h.get("apps") or 0):
            continue
        n = int(h["apps"])
        out.append(_infra_item(id_=f"infra:host_unreachable:{h['id']}",
                               infra_kind="host_unreachable", title=f"{h['name']} unreachable",
                               detail=f"{n} app{'s' if n != 1 else ''} on it", area="infra",
                               open_=f"?area=infra&view=fleet&host={h['id']}",
                               answers=[{"key": "reverify", "label": "Re-verify",
                                         "enabled": True},
                                        {"key": "open", "label": "Open", "enabled": True}],
                               at=h.get("last_preflight_at") or h.get("updated_at")))
    return out


def attention(connection: Any, settings: Any, project_id: str) -> list[dict[str, Any]]:
    """Every §2.2 infra/observability item, each with `area`. One derivation, every reader."""
    now = _now()
    partial: list[str] = []
    items: list[dict[str, Any]] = []
    items += _guard(connection, partial, "approvals",
                    lambda: _approvals(connection, project_id, now), [])
    items += _guard(connection, partial, "failed deploys",
                    lambda: _failed_deploys(connection, settings, project_id), [])
    items += _guard(connection, partial, "health", lambda: _apps_down(connection, project_id, now), [])
    items += _guard(connection, partial, "app errors",
                    lambda: _apps_erroring(connection, project_id), [])
    items += _guard(connection, partial, "hosts",
                    lambda: _hosts_unreachable(connection, project_id), [])
    return items
