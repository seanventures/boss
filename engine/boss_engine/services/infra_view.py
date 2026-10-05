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
        "running": {"word": "Deploying", "tone": "accent"},
        "applying": {"word": "Deploying", "tone": "accent"},
        "deploying": {"word": "Deploying", "tone": "accent"},
        # `succeeded`/`healthy` say Live for the newest of a target and Done for older ones;
        # `deploy_word(raw, newest=...)` decides. This entry is the newest case.
        "succeeded": {"word": "Live", "tone": "green"},
        "healthy": {"word": "Live", "tone": "green"},
        "completed": {"word": "Live", "tone": "green"},
        "failed": {"word": "Failed", "tone": "red"},
        "rolled_back": {"word": "Rolled back", "tone": "grey"},
        "rejected": {"word": "Rejected", "tone": "grey"},
        "cancelled": {"word": "Cancelled", "tone": "grey"},
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


class LastDeploy(TypedDict, total=False):
    sha7: str
    at: str
    word: str
    ref: str              # "<kind>:<id>" → deploy drawer


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
    decision_id: Optional[str]
    run_id: Optional[str]             # the build that made it → Build ↗


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
# `area` on each item says where it lives: "infra" | "observability".

INFRA_ATTENTION_KINDS = ("deploy_approval", "deploy_failed", "app_down", "host_unreachable")


def etag_of(material: Any) -> str:
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode())
    return f'"inf-{digest.hexdigest()[:24]}"'


# ── the builders (stream S1 implements; signatures frozen) ──────────────────────────────────


def fleet(connection: Any, settings: Any, project_id: str, *, viewer: Optional[str] = None) -> Fleet:
    raise NotImplementedError("S1")


def host_detail(connection: Any, settings: Any, project_id: str, host_id: str) -> HostDetail:
    raise NotImplementedError("S1")


def target_detail(connection: Any, settings: Any, project_id: str, target_id: str) -> TargetDetail:
    raise NotImplementedError("S1")


def deploy_rows(connection: Any, settings: Any, project_id: str, *, state: Optional[str] = None,
                app: Optional[str] = None, env: Optional[str] = None, cursor: Optional[str] = None,
                limit: int = 30, viewer: Optional[str] = None) -> DeployList:
    raise NotImplementedError("S1")


def deploy_detail(connection: Any, settings: Any, project_id: str, ref: str) -> DeployDetail:
    raise NotImplementedError("S1")


def deploy_pins(connection: Any, target_ids: list[str], since: Any) -> dict[str, list[dict[str, Any]]]:
    """target_id → [{at, sha7, word, tone, ref}] for deploys that FINISHED at/after `since`."""
    raise NotImplementedError("S1")


def deploy_failure_line(row: dict[str, Any]) -> str:
    """≤ 6 words: why a deploy failed, from its error/failure_reason/failed step."""
    raise NotImplementedError("S1")


def preview_rows(connection: Any, settings: Any, project_id: str) -> PreviewList:
    raise NotImplementedError("S1")


def cloud(connection: Any, settings: Any, project_id: str, view: str, *,
          viewer: Optional[str] = None, filters: Optional[dict[str, str]] = None) -> CloudBody:
    raise NotImplementedError("S1")


def attention(connection: Any, settings: Any, project_id: str) -> list[dict[str, Any]]:
    """Every §2.2 infra/observability item, each with `area`. One derivation, every reader."""
    raise NotImplementedError("S1")
