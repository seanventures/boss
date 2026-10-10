/**
 * Infra: the frozen contract (infra-observability spec §4.1-§4.8, §6.2-§6.5; Phase 0).
 *
 * engine/boss_engine/services/infra_view.py holds the same shapes. The wire is snake_case; the
 * parsers below are the only place it is mapped, and they never invent a field the server did
 * not send: no CPU or memory for a deploy host, no percent of progress, no ETA. Every state word
 * and dot tone is the server's (`infra_view.word()`); the web maps no raw status of its own.
 *
 * Every view is polled through `usePolled` (ETag, pause- and visibility-aware). Nothing here
 * calls `router.refresh`.
 */

import { arr, num, obj, opt, str, usePolled, type Polled } from "./factory-floor";
import { parseAttention, type AttentionItem } from "./agents-live-board";
import { logHref } from "./log-url";

// ── one state language (§4.2) ───────────────────────────────────────────────────────────────

export type Tone = "green" | "amber" | "red" | "accent" | "grey" | "hollow" | "none";
export type Word = { word: string; tone: Tone };

const TONES = new Set<Tone>(["green", "amber", "red", "accent", "grey", "hollow", "none"]);
export const toneOf = (v: unknown): Tone => (TONES.has(v as Tone) ? (v as Tone) : "hollow");
export function parseWord(w: unknown, fallback = "Unknown"): Word {
  const o = obj(w);
  return { word: str(o.word, fallback), tone: toneOf(o.tone) };
}
const optNum = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const optStr = (v: unknown): string | null => (typeof v === "string" && v ? v : null);
const bools = (v: unknown): Record<string, boolean> => {
  const out: Record<string, boolean> = {};
  for (const [k, x] of Object.entries(obj(v))) out[k] = x === true;
  return out;
};
const strings = (v: unknown): Record<string, string> => {
  const out: Record<string, string> = {};
  for (const [k, x] of Object.entries(obj(v))) if (typeof x === "string") out[k] = x;
  return out;
};
const ref = (v: unknown): { id: string; name: string } | null => {
  const o = obj(v);
  return o.id || o.name ? { id: str(o.id), name: str(o.name) } : null;
};

// ── Fleet (§4.3) ────────────────────────────────────────────────────────────────────────────

export type StageKey = "credential" | "install" | "verify" | "provision" | "ready";
export const STAGES: { key: StageKey; label: string }[] = [
  { key: "credential", label: "Credential" }, { key: "install", label: "Authorise" },
  { key: "verify", label: "Verify" }, { key: "provision", label: "Provision" }, { key: "ready", label: "Ready" },
];
export type HostStage = { stage: StageKey; index: number; label: string };
const STAGE_KEYS = new Set(STAGES.map((s) => s.key));
export function parseStage(w: unknown): HostStage {
  const o = obj(w);
  const stage = (STAGE_KEYS.has(str(o.stage) as StageKey) ? str(o.stage) : "credential") as StageKey;
  return { stage, index: num(o.index, STAGES.findIndex((s) => s.key === stage) + 1), label: str(o.label, "Credential") };
}

/** One mini-strip cell: ok of n probes in a 45-minute bucket. n === 0 is a gap, never green. */
export type StripCell = { ok: number; n: number };
export const parseStrip = (v: unknown): StripCell[] =>
  arr(v).map((c) => { const a = arr(c); return { ok: num(a[0]), n: num(a[1]) }; });

/** A newer attempt that failed after the live deploy (operator decision 2026-10-04): Live is what
 *  is running, and this is said beside it — `Last attempt failed · 2h ago`. */
export type FailedAfter = { at: string; ref: string };
const parseFailedAfter = (v: unknown): FailedAfter | null => {
  const o = obj(v);
  return o.at && o.ref ? { at: str(o.at), ref: str(o.ref) } : null;
};
export type LastDeploy = { sha7: string | null; at: string | null; word: string; ref: string | null; failedAfter: FailedAfter | null };
export type ResidentKind = "target" | "preview" | "monitoring";
export type Resident = {
  kind: ResidentKind;
  id: string;
  name: string;
  env: string | null;
  health: Word;
  strip: StripCell[];
  lastDeploy: LastDeploy | null;
  expiresAt: string | null;
  url: string | null;
  port: number | null;
};
export function parseResident(w: unknown): Resident {
  const o = obj(w);
  const kind = (["target", "preview", "monitoring"].includes(str(o.kind)) ? str(o.kind) : "target") as ResidentKind;
  const ld = o.last_deploy ? obj(o.last_deploy) : null;
  return {
    kind, id: str(o.id), name: str(o.name), env: optStr(o.env), health: parseWord(o.health, "No signal"),
    strip: parseStrip(o.strip),
    lastDeploy: ld ? { sha7: optStr(ld.sha7), at: optStr(ld.at), word: str(ld.word), ref: optStr(ld.ref),
                       failedAfter: parseFailedAfter(ld.failed_after) } : null,
    expiresAt: optStr(o.expires_at), url: optStr(o.url), port: optNum(o.port),
  };
}

export type StageAction = "generate_key" | "verify" | "install_docker" | "copy_command" | null;
export type Rack = {
  id: string;
  thisComputer: boolean;
  name: string;
  address: string | null;
  arch: string | null;
  state: Word;
  stage: HostStage;
  checkedAt: string | null;
  maxApps: number | null;
  apps: number;
  portsUsed: number;
  portsTotal: number | null;
  residents: Resident[];
  stageAction: StageAction;
};
export function parseRack(w: unknown): Rack {
  const o = obj(w);
  return {
    id: str(o.id), thisComputer: o.this_computer === true, name: str(o.name), address: optStr(o.address),
    arch: optStr(o.arch), state: parseWord(o.state), stage: parseStage(o.stage), checkedAt: optStr(o.checked_at),
    maxApps: optNum(o.max_apps), apps: num(o.apps), portsUsed: num(o.ports_used), portsTotal: optNum(o.ports_total),
    residents: arr(o.residents).map(parseResident), stageAction: (optStr(o.stage_action) as StageAction) ?? null,
  };
}

export type Preconditions = {
  outwardSshAllowed: boolean; hostWritesAllowed: boolean; blocker: string | null;
  sandboxDeploysEnabled: boolean; community: boolean; canWrite: boolean;
};
export type ThisComputer = { hostname: string | null; arch: string | null; os: string | null;
  agentState: string | null; hostId: string | null; placeId: string | null };
export type FleetCounts = { ready: number; settingUp: number; unreachable: number; apps: number; previewsLive: number; needsYou: number };
export type Fleet = {
  counts: FleetCounts;
  preconditions: Preconditions;
  attention: AttentionItem[];
  thisComputer: ThisComputer | null;
  racks: Rack[];
  unplaced: Resident[];
  retiredCount: number;
  partial: string[];
  generatedAt: string | null;
};
export function parseFleet(w: unknown): Fleet {
  const o = obj(w);
  const c = obj(o.counts);
  const p = obj(o.preconditions);
  const t = o.this_computer ? obj(o.this_computer) : null;
  return {
    counts: { ready: num(c.ready), settingUp: num(c.setting_up), unreachable: num(c.unreachable), apps: num(c.apps),
              previewsLive: num(c.previews_live), needsYou: num(c.needs_you) },
    preconditions: { outwardSshAllowed: p.outward_ssh_allowed === true, hostWritesAllowed: p.host_writes_allowed === true,
                     blocker: optStr(p.blocker), sandboxDeploysEnabled: p.sandbox_deploys_enabled !== false,
                     community: p.community === true, canWrite: p.can_write !== false },
    attention: arr(o.attention).map(parseAttention),
    thisComputer: t ? { hostname: optStr(t.hostname), arch: optStr(t.arch), os: optStr(t.os),
                        agentState: optStr(t.agent_state), hostId: optStr(t.host_id), placeId: optStr(t.place_id) } : null,
    racks: arr(o.racks).map(parseRack),
    unplaced: arr(o.unplaced).map(parseResident),
    retiredCount: num(o.retired_count),
    partial: arr(o.partial).map(String),
    generatedAt: optStr(o.generated_at),
  };
}

// ── Host and target drawers (§4.4, §4.5) ────────────────────────────────────────────────────

export type HostFinding = { kind: string; label: string; ok: boolean; detail: string | null; remedy: string | null };
const parseFinding = (w: unknown): HostFinding => {
  const o = obj(w);
  return { kind: str(o.kind), label: str(o.label, str(o.kind)), ok: o.ok === true, detail: optStr(o.detail), remedy: optStr(o.remedy) };
};
export type HostDetail = {
  id: string; thisComputer: boolean; name: string; state: Word; stage: HostStage;
  facts: Record<string, unknown>;
  keyPublic: string | null; authoriseLine: string | null; installCommand: string | null;
  routeOptions: { key: string; label: string; command: string }[];
  findings: HostFinding[]; checkedAt: string | null; residents: Resident[];
  can: Record<string, boolean>;
  /** The machine's own SSH key, pinned at Verify; every governed connection insists on it. */
  hostKey: { pinned: boolean; fingerprint: string | null; words: string } | null;
};
export function parseHostDetail(w: unknown): HostDetail {
  const o = obj(w);
  return {
    id: str(o.id), thisComputer: o.this_computer === true, name: str(o.name), state: parseWord(o.state),
    stage: parseStage(o.stage), facts: obj(o.facts), keyPublic: optStr(o.key_public),
    authoriseLine: optStr(o.authorise_line), installCommand: optStr(o.install_command),
    routeOptions: arr(o.route_options).map((r) => { const x = obj(r); return { key: str(x.key), label: str(x.label), command: str(x.command) }; }),
    findings: arr(o.findings).map(parseFinding), checkedAt: optStr(o.checked_at),
    residents: arr(o.residents).map(parseResident), can: bools(o.can),
    hostKey: o.host_key && typeof o.host_key === "object"
      ? { pinned: obj(o.host_key).pinned === true, fingerprint: optStr(obj(o.host_key).fingerprint),
          words: str(obj(o.host_key).words) }
      : null,
  };
}

export type TargetDetail = {
  id: string; app: string; env: string; kind: string; health: Word; since: string | null;
  toggles: { autoDeploy: boolean; paused: boolean };
  where: { host: { id: string; name: string } | null; repo: string | null; ref: string | null; manifest: string | null;
           domain: string | null; publicUrl: string | null; account: { id: string; name: string } | null };
  strip: StripCell[]; uptime24h: number | null; recent: DeployRow[];
  secrets: { name: string; kind: string; status: string }[];
  can: Record<string, boolean>; disabled: Record<string, string>;
};
export function parseTargetDetail(w: unknown): TargetDetail {
  const o = obj(w);
  const t = obj(o.toggles);
  const wh = obj(o.where);
  return {
    id: str(o.id), app: str(o.app), env: str(o.env), kind: str(o.kind), health: parseWord(o.health, "No signal"),
    since: optStr(o.since), toggles: { autoDeploy: t.auto_deploy === true, paused: t.paused === true },
    where: { host: ref(wh.host), repo: optStr(wh.repo), ref: optStr(wh.ref), manifest: optStr(wh.manifest),
             domain: optStr(wh.domain), publicUrl: optStr(wh.public_url), account: ref(wh.account) },
    strip: parseStrip(o.strip), uptime24h: optNum(o.uptime_24h), recent: arr(o.recent).map(parseDeployRow),
    secrets: arr(o.secrets).map((s) => { const x = obj(s); return { name: str(x.name), kind: str(x.kind), status: str(x.status) }; }),
    can: bools(o.can), disabled: strings(o.disabled),
  };
}

// ── Deploys (§4.6, §6.3) ────────────────────────────────────────────────────────────────────

export type DeployKind = "stack" | "registry" | "delivery";
export type StepState = "done" | "running" | "failed" | "pending" | "skipped";
export type Trigger = "manual" | "merge" | "workflow" | "rollback" | "redeploy";
export type DeployRow = {
  ref: string; kind: DeployKind; targetId: string | null; app: string; env: string;
  host: { id: string; name: string } | null; account: string | null;
  sha7: string | null; refName: string | null; trigger: Trigger; rollbackOfSha7: string | null;
  approver: { roleKey?: string; actor?: string } | null;
  state: string; word: string; tone: Tone;
  steps: { s: StepState; t: string }[];
  startedAt: string | null; finishedAt: string | null; durationMs: number | null;
  can: { approve: boolean; reject: boolean; rollback: boolean; redeploy: boolean };
  /** Additive (S3): the commit `Roll back` redeploys, for `Roll back to <sha7>`; null if none. */
  rollbackToSha7: string | null;
  decisionId: string | null; runId: string | null;
  /** On a Live row: a newer attempt on the same target that failed. */
  failedAfter: FailedAfter | null;
};
const STEP_STATES = new Set(["done", "running", "failed", "pending", "skipped"]);
const TRIGGERS = new Set(["manual", "merge", "workflow", "rollback", "redeploy"]);
export function parseDeployRow(w: unknown): DeployRow {
  const o = obj(w);
  const ap = o.approver ? obj(o.approver) : null;
  const can = bools(o.can);
  return {
    ref: str(o.ref), kind: (["stack", "registry", "delivery"].includes(str(o.kind)) ? str(o.kind) : "stack") as DeployKind,
    targetId: optStr(o.target_id), app: str(o.app), env: str(o.env), host: ref(o.host), account: optStr(o.account),
    sha7: optStr(o.sha7), refName: optStr(o.ref_name),
    trigger: (TRIGGERS.has(str(o.trigger)) ? str(o.trigger) : "manual") as Trigger, rollbackOfSha7: optStr(o.rollback_of_sha7),
    approver: ap ? { ...(opt(ap.role_key) ? { roleKey: str(ap.role_key) } : {}), ...(opt(ap.actor) ? { actor: str(ap.actor) } : {}) } : null,
    state: str(o.state), word: str(o.word), tone: toneOf(o.tone),
    steps: arr(o.steps).slice(0, 24).map((s) => { const x = obj(s); return { s: (STEP_STATES.has(str(x.s)) ? str(x.s) : "pending") as StepState, t: str(x.t) }; }),
    startedAt: optStr(o.started_at), finishedAt: optStr(o.finished_at), durationMs: optNum(o.duration_ms),
    can: { approve: !!can.approve, reject: !!can.reject, rollback: !!can.rollback, redeploy: !!can.redeploy },
    rollbackToSha7: optStr(o.rollback_to_sha7),
    decisionId: optStr(o.decision_id), runId: optStr(o.run_id), failedAfter: parseFailedAfter(o.failed_after),
  };
}
export type DeployList = {
  counts: { needsYou: number; live: number; failed: number; all: number };
  rows: DeployRow[]; nextCursor: string | null; filters: { apps: string[]; envs: string[] };
  attention: AttentionItem[]; partial: string[];
};
export function parseDeployList(w: unknown): DeployList {
  const o = obj(w);
  const c = obj(o.counts);
  const f = obj(o.filters);
  return {
    counts: { needsYou: num(c.needs_you), live: num(c.live), failed: num(c.failed), all: num(c.all) },
    rows: arr(o.rows).map(parseDeployRow), nextCursor: optStr(o.next_cursor),
    filters: { apps: arr(f.apps).map(String), envs: arr(f.envs).map(String) },
    attention: arr(o.attention).map(parseAttention), partial: arr(o.partial).map(String),
  };
}
export type DeployDetail = {
  row: DeployRow;
  steps: { title: string; state: StepState; durationMs: number | null }[];
  waitingOn: string | null;
  failure: { line: string; error: string } | null;
  plan: { steps: string[]; envKeys: string[]; health: Record<string, unknown> | null } | null;
  preflight: HostFinding[];
  logTail: string | null;
  healthCheckResults: Record<string, unknown>[];
  promotion: { id: string; phases: { key: string; label: string; state: string }[] } | null;
  openUrl: string | null; changesetId: string | null; live: boolean;
};
export function parseDeployDetail(w: unknown): DeployDetail {
  const o = obj(w);
  const fl = o.failure ? obj(o.failure) : null;
  const pl = o.plan ? obj(o.plan) : null;
  const pr = o.promotion ? obj(o.promotion) : null;
  return {
    row: parseDeployRow(o.row),
    steps: arr(o.steps).map((s) => { const x = obj(s); return { title: str(x.title), state: (STEP_STATES.has(str(x.state)) ? str(x.state) : "pending") as StepState, durationMs: optNum(x.duration_ms) }; }),
    waitingOn: optStr(o.waiting_on),
    failure: fl ? { line: str(fl.line), error: str(fl.error) } : null,
    plan: pl ? { steps: arr(pl.steps).map(String), envKeys: arr(pl.env_keys).map(String), health: pl.health ? obj(pl.health) : null } : null,
    preflight: arr(o.preflight).map(parseFinding), logTail: optStr(o.log_tail),
    healthCheckResults: arr(o.health_check_results).map(obj),
    promotion: pr ? { id: str(pr.id), phases: arr(pr.phases).map((p) => { const x = obj(p); return { key: str(x.key), label: str(x.label), state: str(x.state) }; }) } : null,
    openUrl: optStr(o.open_url), changesetId: optStr(o.changeset_id), live: o.live === true,
  };
}

// ── Previews (§4.7) ─────────────────────────────────────────────────────────────────────────

export type Substrate = "Process" | "Docker" | "Rootfs" | "microVM";
export type PreviewRow = {
  id: string; runId: string | null; app: string; branch: string | null; state: string; word: string; tone: Tone;
  substrate: Substrate; port: number | null; host: { id: string; name: string } | null; url: string | null;
  createdAt: string | null; expiresAt: string | null; lastSeenAt: string | null;
  can: { open: boolean; log: boolean; stop: boolean; restart: boolean };
  /** Why Restart cannot run (its build is gone); the button is shown disabled with it. */
  restartWhy: string | null;
};
export function parsePreviewRow(w: unknown): PreviewRow {
  const o = obj(w);
  const can = bools(o.can);
  const sub = str(o.substrate);
  return {
    id: str(o.id), runId: optStr(o.run_id), app: str(o.app), branch: optStr(o.branch), state: str(o.state),
    word: str(o.word), tone: toneOf(o.tone),
    substrate: (["Process", "Docker", "Rootfs", "microVM"].includes(sub) ? sub : "Process") as Substrate,
    port: optNum(o.port), host: ref(o.host), url: optStr(o.url), createdAt: optStr(o.created_at),
    expiresAt: optStr(o.expires_at), lastSeenAt: optStr(o.last_seen_at),
    can: { open: !!can.open, log: !!can.log, stop: !!can.stop, restart: !!can.restart },
    restartWhy: optStr(o.restart_why),
  };
}
export type PreviewList = {
  counts: { live: number; failed24h: number; ended24h: number };
  live: PreviewRow[]; failed24h: PreviewRow[]; ended24h: PreviewRow[]; partial: string[];
};
export function parsePreviewList(w: unknown): PreviewList {
  const o = obj(w);
  const c = obj(o.counts);
  return {
    counts: { live: num(c.live), failed24h: num(c.failed_24h), ended24h: num(c.ended_24h) },
    live: arr(o.live).map(parsePreviewRow), failed24h: arr(o.failed_24h).map(parsePreviewRow),
    ended24h: arr(o.ended_24h).map(parsePreviewRow), partial: arr(o.partial).map(String),
  };
}

// ── Cloud (§4.8) ────────────────────────────────────────────────────────────────────────────

export type CloudView = "accounts" | "resources" | "capacity" | "spend" | "changes";
export const CLOUD_VIEWS: { key: CloudView; label: string }[] = [
  { key: "accounts", label: "Accounts" }, { key: "resources", label: "Resources" },
  { key: "capacity", label: "Capacity" }, { key: "spend", label: "Spend" }, { key: "changes", label: "Changes" },
];
/** A Cloud row is one table row of its sub-view; its keys are the sub-view's columns (§4.8).
 *  Money arrives as micros AND a ready `*_usd` string; the web shows only the string. */
export type CloudRow = Record<string, unknown> & { id: string };
export type CloudBody = {
  view: CloudView; counts: Record<string, number>; rows: CloudRow[]; filters: Record<string, string[]>;
  header: Record<string, unknown>; notices: { key: string; tip: string }[]; partial: string[];
};
export function parseCloud(w: unknown): CloudBody {
  const o = obj(w);
  const counts: Record<string, number> = {};
  for (const [k, v] of Object.entries(obj(o.counts))) counts[k] = num(v);
  const filters: Record<string, string[]> = {};
  for (const [k, v] of Object.entries(obj(o.filters))) filters[k] = arr(v).map(String);
  const view = str(o.view, "accounts") as CloudView;
  return {
    view: CLOUD_VIEWS.some((c) => c.key === view) ? view : "accounts", counts,
    rows: arr(o.rows).map((r) => { const x = obj(r); return { ...x, id: str(x.id) }; }),
    filters, header: obj(o.header),
    notices: arr(o.notices).map((n) => { const x = obj(n); return { key: str(x.key), tip: str(x.tip) }; }),
    partial: arr(o.partial).map(String),
  };
}

// ── URL state (§4.1) ────────────────────────────────────────────────────────────────────────

export type InfraTab = "fleet" | "deploys" | "previews" | "cloud";
export const INFRA_TABS: { key: InfraTab; label: string; tip: string }[] = [
  { key: "fleet", label: "Fleet", tip: "Machines and what runs on them" },
  { key: "deploys", label: "Deploys", tip: "Every deploy, newest first" },
  { key: "previews", label: "Previews", tip: "Running previews and when they end" },
  { key: "cloud", label: "Cloud", tip: "Cloud accounts, resources, capacity and spend" },
];
export type InfraUrl = {
  view: InfraTab;
  host: string | null; target: string | null; deploy: string | null; preview: string | null;
  account: string | null; resource: string | null; changeset: string | null; pool: string | null;
  cloud: CloudView;
  q: string;
  state: string | null; app: string | null; env: string | null;   // Deploys filters
  /** `add=host|target|…` opens that + Add sheet once (a "set up where it deploys" link lands here). */
  add: string | null;
};
const INFRA_TAB_KEYS = new Set<string>(INFRA_TABS.map((t) => t.key));
export function readInfraUrl(search: string): InfraUrl {
  const p = new URLSearchParams(search);
  const view = p.get("view") ?? p.get("tab") ?? "";
  const cloud = p.get("cloud") ?? "";
  return {
    view: (INFRA_TAB_KEYS.has(view) ? view : "fleet") as InfraTab,
    host: p.get("host"), target: p.get("target"), deploy: p.get("deploy"), preview: p.get("preview"),
    account: p.get("account"), resource: p.get("resource"), changeset: p.get("changeset"), pool: p.get("pool"),
    cloud: (CLOUD_VIEWS.some((c) => c.key === cloud) ? cloud : "accounts") as CloudView,
    q: p.get("q") ?? "", state: p.get("state"), app: p.get("app"), env: p.get("env"),
    add: p.get("add"),
  };
}
/** Drawer keys: opening one closes the others. */
export const INFRA_DRAWERS = ["host", "target", "deploy", "preview", "account", "resource", "changeset", "pool"] as const;

// ── fetch urls + hooks ──────────────────────────────────────────────────────────────────────

const q = (projectId: string, extra: Record<string, string | number | null | undefined> = {}) => {
  const p = new URLSearchParams({ project_id: projectId });
  for (const [k, v] of Object.entries(extra)) if (v !== null && v !== undefined && v !== "") p.set(k, String(v));
  return p.toString();
};
export const fleetUrl = (projectId: string, viewer?: string) => `/v1/infra/fleet?${q(projectId, { viewer })}`;
export const hostUrl = (projectId: string, id: string) => `/v1/infra/hosts/${encodeURIComponent(id)}?${q(projectId)}`;
export const targetUrl = (projectId: string, id: string) => `/v1/infra/targets/${encodeURIComponent(id)}?${q(projectId)}`;
export const deploysUrl = (projectId: string, f: { state?: string | null; app?: string | null; env?: string | null; cursor?: string | null; viewer?: string } = {}) =>
  `/v1/infra/deploys?${q(projectId, f)}`;
export const deployUrl = (projectId: string, deployRef: string) => `/v1/infra/deploys/${encodeURIComponent(deployRef)}?${q(projectId)}`;
export const previewsUrl = (projectId: string) => `/v1/infra/previews?${q(projectId)}`;
export const cloudUrl = (projectId: string, view: CloudView, filters: Record<string, string | null | undefined> = {}) =>
  `/v1/infra/cloud?${q(projectId, { view, ...filters })}`;

/** Poll cadences (§6.2). */
export const FLEET_POLL_MS = 15_000;
export const HOST_SETUP_POLL_MS = 5_000;
export const TARGET_POLL_MS = 15_000;
export const DEPLOYS_LIVE_POLL_MS = 8_000;
export const DEPLOYS_IDLE_POLL_MS = 30_000;
export const DEPLOY_LIVE_POLL_MS = 3_000;
export const PREVIEWS_POLL_MS = 10_000;
export const CLOUD_POLL_MS = 60_000;
const NEVER = 24 * 3600_000;

export const useFleet = (projectId: string, viewer?: string): Polled<Fleet> =>
  usePolled(projectId ? fleetUrl(projectId, viewer) : null, parseFleet, () => FLEET_POLL_MS);
export const useHostDetail = (projectId: string, id: string | null): Polled<HostDetail> =>
  usePolled(projectId && id ? hostUrl(projectId, id) : null, parseHostDetail,
            (d) => (d && d.stage.stage !== "ready" ? HOST_SETUP_POLL_MS : FLEET_POLL_MS));
export const useTargetDetail = (projectId: string, id: string | null): Polled<TargetDetail> =>
  usePolled(projectId && id ? targetUrl(projectId, id) : null, parseTargetDetail, () => TARGET_POLL_MS);
// `approved`: a governed host deploy queued for its worker — it turns Deploying in seconds.
const LIVE_STATES = new Set(["planned", "awaiting_approval", "approved", "running", "applying", "created", "pending"]);
export const useDeploys = (projectId: string, f: { state?: string | null; app?: string | null; env?: string | null; viewer?: string } = {}): Polled<DeployList> =>
  usePolled(projectId ? deploysUrl(projectId, f) : null, parseDeployList,
            (d) => (d && d.rows.some((r) => LIVE_STATES.has(r.state)) ? DEPLOYS_LIVE_POLL_MS : DEPLOYS_IDLE_POLL_MS));
/** One poll for a deploy's detail: 3 s while live, then it stops (the drawer and the workflow
 *  stack-deploy step both read this). */
export const useDeployDetail = (projectId: string, deployRef: string | null): Polled<DeployDetail> =>
  usePolled(projectId && deployRef ? deployUrl(projectId, deployRef) : null, parseDeployDetail,
            (d) => (d === null || d.live ? DEPLOY_LIVE_POLL_MS : NEVER));
export const usePreviews = (projectId: string): Polled<PreviewList> =>
  usePolled(projectId ? previewsUrl(projectId) : null, parsePreviewList, () => PREVIEWS_POLL_MS);
export const useCloud = (projectId: string, view: CloudView, filters: Record<string, string | null | undefined> = {}): Polled<CloudBody> =>
  usePolled(projectId ? cloudUrl(projectId, view, filters) : null, parseCloud, () => CLOUD_POLL_MS);

// ── links into the sibling areas (§6.7) ─────────────────────────────────────────────────────

/** The Activity log, pre-filtered: through logHref (lib/log-url.ts), the one way into the Log,
 *  so the org is kept and the scope uses the Log's own names (src, subject, from). */
export function logsHref(source: "deployment" | "llm_call" | "app_log" | "event", subject?: string | null,
  since?: string | null, search?: string): string {
  return logHref({ src: source, subject: subject ?? null, since: since ?? null }, search);
}
export const buildHref = (runId: string) => `?area=code_tasks&build=${encodeURIComponent(runId)}`;
export const deployHref = (deployRef: string) => `?area=infra&view=deploys&deploy=${encodeURIComponent(deployRef)}`;
export const hostHref = (hostId: string) => `?area=infra&view=fleet&host=${encodeURIComponent(hostId)}`;
export const changeHref = (changesetId: string) => `?area=infra&view=cloud&cloud=changes&changeset=${encodeURIComponent(changesetId)}`;
