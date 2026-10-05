/**
 * Infra's shared fixtures (infra-observability spec §6.2-§6.5): WIRE-format bodies, snake_case,
 * exactly the shapes engine/boss_engine/services/infra_view.py's TypedDicts declare, for every
 * Infra view the console polls — and `mockInfraFetch()`, which routes `fetch(url)` to them (with
 * ETags and 304s, like the API's `_etagged`) and records every write.
 *
 * The ids are the ones lib/infra-obs-parity.ts names, so the parity test and every Infra test
 * draw the same world:
 *   hosts    this (This computer) · h_ready (Ready, 2 residents) · h_setup (Setting up, stage
 *            verify) · h_down (Unreachable) · h_old (Retired; hidden unless Show retired)
 *   targets  t_live (stack, on h_ready, Up) · t_down (stack, on h_down, Down) · t_cloud
 *            (registry, NOT PLACED)
 *   deploys  stack:sd_wait (Needs you) · stack:sd_run (Deploying) · stack:sd_live (Live) ·
 *            stack:sd_failed (Failed, an earlier good deploy exists → can.rollback) ·
 *            stack:sd_ok (Done) · stack:sd_rb (Rolled back) · registry:dr_1 (promotion, change
 *            cs_1) · delivery:dd_1 (a delivery deploy)
 *   previews pv_live (Live) · pv_start (Starting, < 5 min left) · pv_fail (Failed) · pv_end (Stopped)
 *
 * Variants: "default" (everything allowed, sandbox deploys off), "blocked" (outward SSH off,
 * no host writes), "community" (Community edition: deploy-on-merge and registry deploy paused),
 * "readonly" (the viewer may not change infra).
 */
import { CLOUD_ROUTES } from "./infra-cloud-fixtures";
import { DEPLOY_EXTRA_ROUTES } from "./infra-deploy-fixtures";

export type W = Record<string, unknown>;
export type Variant = "default" | "blocked" | "community" | "readonly";

const MIN = 60_000;
/** An ISO time `m` minutes from now (negative = ago). Computed at call time, so fake timers agree. */
export const at = (m: number): string => new Date(Date.now() + m * MIN).toISOString();

const word = (w: string, tone: string) => ({ word: w, tone });
const UP = word("Up", "green");
const DOWN = word("Down", "red");
const NOT_DEPLOYED = word("Not deployed", "grey");
const READY = word("Ready", "green");

/** 32 cells × 45 min, oldest first; `[ok, n]`. */
export function strip(kind: "up" | "down" | "none" = "up"): number[][] {
  return Array.from({ length: 32 }, (_, i) => {
    if (kind === "none") return [0, 0];
    if (kind === "down") return i > 27 ? [0, 22] : i === 10 ? [0, 0] : [22, 22];
    return i === 5 ? [21, 22] : i === 11 ? [0, 0] : [22, 22];
  });
}

// ── residents ───────────────────────────────────────────────────────────────────────────────

export const residentTLive = (): W => ({
  kind: "target", id: "t_live", name: "invoice", env: "prod", health: UP, strip: strip("up"),
  last_deploy: { sha7: "9f8e7d6", at: at(-180), word: "Live", ref: "stack:sd_live" },
  expires_at: null, url: "https://invoice.example.com", port: 4100,
});
export const residentTDown = (): W => ({
  kind: "target", id: "t_down", name: "api", env: "prod", health: DOWN, strip: strip("down"),
  last_deploy: { sha7: "4c5d6e7", at: at(-1500), word: "Done", ref: "stack:sd_ok" },
  expires_at: null, url: null, port: 4200,
});
export const residentPreview = (): W => ({
  kind: "preview", id: "pv_live", name: "Preview search", env: null, health: word("Live", "green"),
  strip: [], last_deploy: null, expires_at: at(41), url: "/preview/pv_live/", port: 4127,
});
export const residentMonitoring = (): W => ({
  kind: "monitoring", id: "monitoring", name: "Monitoring", env: null, health: word("Running", "green"),
  strip: [], last_deploy: null, expires_at: null, url: "https://grafana.example.com", port: 3000,
});
export const residentTCloud = (): W => ({
  kind: "target", id: "t_cloud", name: "billing", env: "prod", health: NOT_DEPLOYED, strip: strip("none"),
  last_deploy: null, expires_at: null, url: null, port: null,
});

// ── attention (agents_live_board AttentionItem wire, area=infra) ─────────────────────────────

export const attentionApproval = (): W => ({
  id: "dec_wait", kind: "approval", decision_kind: "deploy_gate", decision_id: "dec_wait",
  title: "Deploy invoice to prod?", detail: "a1b2c3d · from main · 6 steps", role_key: null,
  actions: [
    { action: "approve", label: "Approve", tone: "approve", effect: "Deploys a1b2c3d to prod", needs: [] },
    { action: "reject", label: "Reject", tone: "reject", effect: "Nothing deploys", needs: [] },
  ],
  answers: [], decide_inline: true, at: at(-2), area: "infra", open: "?area=infra&view=deploys&deploy=stack%3Asd_wait",
});
export const attentionDeployFailed = (): W => ({
  id: "infra:deploy_failed:t_down", kind: "infra", infra_kind: "deploy_failed", title: "api deploy failed",
  detail: "api · prod · e1f2a3b", role_key: null, actions: [],
  answers: [{ key: "rollback", label: "Roll back", enabled: true }, { key: "open", label: "Open", enabled: true }],
  decide_inline: false, at: at(-60), area: "infra", open: "?area=infra&view=deploys&deploy=stack%3Asd_failed",
});
export const attentionHostDown = (): W => ({
  id: "infra:host_unreachable:h_down", kind: "infra", infra_kind: "host_unreachable", title: "edge-2 unreachable",
  detail: "1 app on it", role_key: null, actions: [],
  answers: [{ key: "reverify", label: "Re-verify", enabled: true }, { key: "open", label: "Open", enabled: true }],
  decide_inline: false, at: at(-12), area: "infra", open: "?area=infra&view=fleet&host=h_down",
});

// ── Fleet (GET /v1/infra/fleet) ─────────────────────────────────────────────────────────────

const stage = (key: string, index: number, label: string) => ({ stage: key, index, label });

export function fleetFixture(variant: Variant = "default"): W {
  const blocked = variant === "blocked";
  return {
    counts: { ready: 2, setting_up: 1, unreachable: 1, apps: 3, previews_live: 2, needs_you: 3 },
    preconditions: {
      outward_ssh_allowed: !blocked, host_writes_allowed: !blocked,
      blocker: blocked ? "BOSS_SSH_OUTWARD_ALLOWED is false on this install" : null,
      sandbox_deploys_enabled: false, community: variant === "community", can_write: variant !== "readonly",
    },
    attention: [attentionApproval(), attentionDeployFailed(), attentionHostDown()],
    this_computer: { hostname: "boss-1", arch: "arm64", os: "linux", agent_state: "installed", host_id: null, place_id: "place_local" },
    racks: [
      { id: "this", this_computer: true, name: "boss-1", address: "10.0.0.2", arch: "arm64", state: READY,
        stage: stage("ready", 5, "Ready"), checked_at: at(-2), max_apps: 8, apps: 0, ports_used: 1, ports_total: 100,
        residents: [residentMonitoring()], stage_action: null },
      { id: "h_down", this_computer: false, name: "edge-2", address: "203.0.113.9", arch: "amd64", state: word("Unreachable", "red"),
        stage: stage("ready", 5, "Ready"), checked_at: at(-12), max_apps: 4, apps: 1, ports_used: 1, ports_total: 50,
        residents: [residentTDown()], stage_action: null },
      { id: "h_setup", this_computer: false, name: "db-box", address: "10.0.0.9", arch: null, state: word("Setting up · 3/5", "accent"),
        stage: stage("verify", 3, "Verify"), checked_at: null, max_apps: null, apps: 0, ports_used: 0, ports_total: null,
        residents: [], stage_action: "verify" },
      { id: "h_ready", this_computer: false, name: "edge-1", address: "203.0.113.7", arch: "amd64", state: READY,
        stage: stage("ready", 5, "Ready"), checked_at: at(-3), max_apps: 4, apps: 1, ports_used: 2, ports_total: 50,
        residents: [residentTLive(), residentPreview()], stage_action: null },
      { id: "h_old", this_computer: false, name: "old-vm", address: "198.51.100.4", arch: "amd64", state: word("Retired", "none"),
        stage: stage("ready", 5, "Ready"), checked_at: at(-9000), max_apps: 2, apps: 0, ports_used: 0, ports_total: 20,
        residents: [], stage_action: null },
    ],
    unplaced: [residentTCloud()],
    retired_count: 1,
    partial: [],
    generated_at: at(0),
  };
}

// ── Host drawer (GET /v1/infra/hosts/{id}) ──────────────────────────────────────────────────

export const PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleExampleExampleExample boss@db-box";
export const AUTHORISE_LINE = "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
  + `printf '%s\\n' '${PUBLIC_KEY}' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys`;

const can = (variant: Variant, extra: W = {}) => {
  const writes = variant !== "blocked" && variant !== "readonly";
  const reach = variant !== "blocked" && variant !== "readonly";
  return { adopt_key: variant !== "readonly", generate_key: variant !== "readonly", verify: reach,
           install_key: writes, install_docker: writes, retire: writes, correct: false, ...extra };
};

export function hostFixture(id: string, variant: Variant = "default"): W | null {
  const facts = (address: string, arch: string | null, extra: W = {}) => ({
    address, user: "ubuntu", port: 22, arch, labels: ["edge"], port_min: 4100, port_max: 4149, max_apps: 4,
    key_fingerprint: "SHA256:k3yF1ngerPr1nt0000000000000000000000000", ...extra,
  });
  switch (id) {
    case "this":
      return {
        id: "this", this_computer: true, name: "boss-1", state: READY, stage: stage("ready", 5, "Ready"),
        facts: { hostname: "boss-1", arch: "arm64", os: "linux", agent_state: "installed" },
        key_public: null, authorise_line: null, install_command: null, route_options: [],
        findings: [], checked_at: at(-2), residents: [residentMonitoring()],
        can: { ...can(variant), retire: false, correct: variant !== "readonly" }, etag: '"h-this"',
      };
    case "h_ready":
      return {
        id: "h_ready", this_computer: false, name: "edge-1", state: READY, stage: stage("ready", 5, "Ready"),
        facts: facts("203.0.113.7", "amd64"), key_public: PUBLIC_KEY, authorise_line: AUTHORISE_LINE, install_command: null,
        route_options: [],
        findings: [
          { kind: "ssh", label: "SSH reachable", ok: true, detail: null, remedy: null },
          { kind: "docker", label: "Docker running", ok: true, detail: "Docker 27.1", remedy: null },
        ],
        checked_at: at(-3), residents: [residentTLive(), residentPreview()], can: can(variant), etag: '"h-ready"',
      };
    case "h_setup":
      return {
        id: "h_setup", this_computer: false, name: "db-box", state: word("Setting up · 3/5", "accent"),
        stage: stage("verify", 3, "Verify"), facts: { ...facts("10.0.0.9", null), max_apps: null, labels: [] },
        key_public: PUBLIC_KEY, authorise_line: AUTHORISE_LINE,
        install_command: "ssh-copy-id -i boss.pub -p 22 ubuntu@10.0.0.9 && echo BOSS_KEY_INSTALLED",
        route_options: [
          { key: "from_laptop", label: "SSH from here", command: "ssh-copy-id -i boss.pub -p 22 ubuntu@10.0.0.9 && echo BOSS_KEY_INSTALLED" },
          { key: "on_host", label: "Provider console", command: AUTHORISE_LINE },
          { key: "cloud_init", label: "Not created yet", command: "#cloud-config\nssh_authorized_keys:\n  - " + PUBLIC_KEY },
        ],
        findings: [{ kind: "key_not_installed", label: "Key not on the host yet", ok: false,
                     detail: "Permission denied (publickey)", remedy: "Run the authorise line on the host" }],
        checked_at: at(-30), residents: [], can: can(variant), etag: '"h-setup"',
      };
    case "h_down":
      return {
        id: "h_down", this_computer: false, name: "edge-2", state: word("Unreachable", "red"), stage: stage("ready", 5, "Ready"),
        facts: facts("203.0.113.9", "amd64"), key_public: PUBLIC_KEY, authorise_line: AUTHORISE_LINE, install_command: null,
        route_options: [],
        findings: [{ kind: "unreachable", label: "Host unreachable", ok: false, detail: "ssh: connect to host 203.0.113.9 port 22: Connection timed out", remedy: "Check the machine is up" }],
        checked_at: at(-12), residents: [residentTDown()], can: can(variant), etag: '"h-down"',
      };
    default:
      return null;
  }
}

// ── Deploys (GET /v1/infra/deploys, /v1/infra/deploys/{ref}) ────────────────────────────────

const steps = (states: string[]) => states.map((s, i) => ({ s, t: ["Clone", "Build image", "Push", "Migrate", "Start", "Health check"][i] ?? `Step ${i + 1}` }));
const noCan = { approve: false, reject: false, rollback: false, redeploy: false };
const hostRef = (id: string, name: string) => ({ id, name });

export function deployRow(ref: string): W {
  const rows: Record<string, W> = {
    "stack:sd_wait": {
      ref: "stack:sd_wait", kind: "stack", target_id: "t_live", app: "invoice", env: "prod", host: hostRef("h_ready", "edge-1"),
      account: null, sha7: "a1b2c3d", ref_name: "main", trigger: "merge", rollback_of_sha7: null, approver: null,
      state: "awaiting_approval", word: "Needs you", tone: "amber", steps: steps(["pending", "pending", "pending", "pending", "pending", "pending"]),
      started_at: at(-2), finished_at: null, duration_ms: null, can: { ...noCan, approve: true, reject: true },
      decision_id: "dec_wait", run_id: "run_inv_1",
    },
    "stack:sd_run": {
      ref: "stack:sd_run", kind: "stack", target_id: "t_inbox", app: "team-inbox", env: "dev", host: hostRef("this", "boss-1"),
      account: null, sha7: "5e6f7a8", ref_name: "feat/inbox", trigger: "workflow", rollback_of_sha7: null, approver: { role_key: "deployment_manager" },
      state: "running", word: "Deploying", tone: "accent", steps: steps(["done", "done", "running", "pending", "pending", "pending"]),
      started_at: at(-1), finished_at: null, duration_ms: null, can: noCan, decision_id: "dec_run", run_id: "run_inbox_2",
    },
    "stack:sd_live": {
      ref: "stack:sd_live", kind: "stack", target_id: "t_live", app: "invoice", env: "prod", host: hostRef("h_ready", "edge-1"),
      account: null, sha7: "9f8e7d6", ref_name: "main", trigger: "manual", rollback_of_sha7: null, approver: { actor: "you" },
      state: "succeeded", word: "Live", tone: "green", steps: steps(["done", "done", "done", "done", "done", "done"]),
      started_at: at(-182), finished_at: at(-180), duration_ms: 112_000, can: { ...noCan, redeploy: true },
      decision_id: "dec_live", run_id: "run_inv_0",
    },
    "stack:sd_failed": {
      ref: "stack:sd_failed", kind: "stack", target_id: "t_down", app: "api", env: "prod", host: hostRef("h_down", "edge-2"),
      account: null, sha7: "e1f2a3b", ref_name: "main", trigger: "manual", rollback_of_sha7: null, approver: { actor: "you" },
      state: "failed", word: "Failed", tone: "red", steps: steps(["done", "done", "done", "failed", "pending", "pending"]),
      started_at: at(-61), finished_at: at(-60), duration_ms: 41_000, can: { ...noCan, rollback: true, redeploy: true },
      rollback_to_sha7: "4c5d6e7",
      decision_id: "dec_failed", run_id: null,
    },
    "stack:sd_ok": {
      ref: "stack:sd_ok", kind: "stack", target_id: "t_down", app: "api", env: "prod", host: hostRef("h_down", "edge-2"),
      account: null, sha7: "4c5d6e7", ref_name: "main", trigger: "merge", rollback_of_sha7: null, approver: { actor: "you" },
      state: "succeeded", word: "Done", tone: "green", steps: steps(["done", "done", "done", "done", "done", "done"]),
      started_at: at(-1502), finished_at: at(-1500), duration_ms: 98_000, can: noCan, decision_id: "dec_ok", run_id: null,
    },
    "stack:sd_rb": {
      ref: "stack:sd_rb", kind: "stack", target_id: "t_live", app: "invoice", env: "prod", host: hostRef("h_ready", "edge-1"),
      account: null, sha7: "77aa01b", ref_name: "main", trigger: "rollback", rollback_of_sha7: "e1f2a3b", approver: { actor: "you" },
      state: "rolled_back", word: "Rolled back", tone: "grey", steps: steps(["done", "done", "done", "done", "done", "done"]),
      started_at: at(-1562), finished_at: at(-1560), duration_ms: 123_000, can: noCan, decision_id: null, run_id: null,
    },
    "registry:dr_1": {
      ref: "registry:dr_1", kind: "registry", target_id: "t_cloud", app: "billing", env: "prod", host: null,
      account: "DigitalOcean · prod", sha7: "c0ffee1", ref_name: "v1.4.0", trigger: "manual", rollback_of_sha7: null, approver: { actor: "you" },
      state: "succeeded", word: "Live", tone: "green", steps: steps(["done", "done", "done"]),
      started_at: at(-2902), finished_at: at(-2900), duration_ms: 150_000, can: noCan, decision_id: null, run_id: null,
    },
    "delivery:dd_1": {
      ref: "delivery:dd_1", kind: "delivery", target_id: "t_inbox", app: "team-inbox", env: "staging", host: hostRef("this", "boss-1"),
      account: null, sha7: "1234abc", ref_name: "main", trigger: "workflow", rollback_of_sha7: null, approver: null,
      state: "succeeded", word: "Done", tone: "green", steps: steps(["done", "done", "done", "done"]),
      started_at: at(-4322), finished_at: at(-4320), duration_ms: 64_000, can: { ...noCan, redeploy: true }, decision_id: null, run_id: "run_inbox_1",
    },
  };
  return rows[ref] ?? rows["stack:sd_live"];
}

export const DEPLOY_REFS = ["stack:sd_wait", "stack:sd_run", "stack:sd_failed", "stack:sd_live", "stack:sd_ok", "stack:sd_rb",
  "registry:dr_1", "delivery:dd_1"];

export function deploysFixture(filter: { state?: string | null; app?: string | null; env?: string | null } = {}): W {
  let rows = DEPLOY_REFS.map(deployRow);
  if (filter.state === "needs_you") rows = rows.filter((r) => r.state === "awaiting_approval");
  if (filter.state === "live") rows = rows.filter((r) => r.word === "Live");
  if (filter.state === "failed") rows = rows.filter((r) => r.state === "failed");
  if (filter.app) rows = rows.filter((r) => r.app === filter.app);
  if (filter.env) rows = rows.filter((r) => r.env === filter.env);
  return {
    counts: { needs_you: 1, live: 2, failed: 1, all: DEPLOY_REFS.length },
    rows, next_cursor: filter.state || filter.app || filter.env ? null : "cur_2",
    filters: { apps: ["api", "billing", "invoice", "team-inbox"], envs: ["dev", "prod", "staging"] },
    attention: [attentionApproval()], partial: [], generated_at: at(0),
  };
}
/** The page after `cur_2` (`Show older`). */
export const deploysOlderFixture = (): W => ({
  counts: { needs_you: 1, live: 2, failed: 1, all: DEPLOY_REFS.length + 1 },
  rows: [{ ...deployRow("stack:sd_ok"), ref: "stack:sd_older", sha7: "0ld0001", started_at: at(-12000), finished_at: at(-11998) }],
  next_cursor: null, filters: { apps: [], envs: [] }, attention: [], partial: [], generated_at: at(0),
});

const PLAN = { steps: ["git clone acme/invoice@main", "docker build -t invoice .", "docker compose up -d", "curl -fsS /healthz"],
               env_keys: ["DATABASE_URL", "STRIPE_KEY"], health: { path: "/healthz", expect: 200 } };
const detailSteps = (row: W) => (row.steps as Array<{ s: string; t: string }>).map((s, i) => ({
  title: s.t, state: s.s, duration_ms: s.s === "done" ? 8_000 + i * 1000 : s.s === "failed" ? 4_000 : null }));

export function deployDetailFixture(ref: string): W {
  const row = deployRow(ref);
  const base = {
    row, steps: detailSteps(row), waiting_on: null, failure: null, plan: PLAN,
    preflight: [{ kind: "host", label: "Host reachable", ok: true, detail: null, remedy: null },
                { kind: "ports", label: "Port 4100 free", ok: true, detail: null, remedy: null }],
    log_tail: "Cloning acme/invoice@main\nBuilding image\nStarted container invoice-prod\nHealth check passed",
    health_check_results: [], promotion: null, open_url: null, changeset_id: null, live: false, generated_at: at(0),
  };
  switch (ref) {
    case "stack:sd_wait": return { ...base, waiting_on: "Your approval", log_tail: null, live: true };
    case "stack:sd_run": return { ...base, waiting_on: "Building image", log_tail: "Cloning acme/team-inbox\nBuilding image", live: true };
    case "stack:sd_live": return { ...base, open_url: "https://invoice.example.com" };
    case "stack:sd_failed": return {
      ...base, failure: { line: "Migration failed", error: "psql: relation \"invoices\" already exists (exit 1)" },
      log_tail: "Cloning acme/api@main\nBuilding image\nPushed\nRunning migrations\nERROR: relation \"invoices\" already exists",
    };
    case "registry:dr_1": return {
      ...base, plan: null, log_tail: null, changeset_id: "cs_1", open_url: "https://billing.example.com",
      health_check_results: [{ name: "GET /", ok: true, status: 200, latency_ms: 182 }, { name: "GET /healthz", ok: true, status: 200, latency_ms: 95 }],
      promotion: { id: "promo_1", phases: [
        { key: "pipeline", label: "Pipeline verdict", state: "done" }, { key: "staging", label: "Staging", state: "done" },
        { key: "smoke", label: "Smoke", state: "done" }, { key: "production", label: "Production", state: "running" },
        { key: "rollout", label: "Rollout", state: "pending" },
      ] },
    };
    default: return base;
  }
}

// ── Target drawer (GET /v1/infra/targets/{id}) ──────────────────────────────────────────────

export function targetFixture(id: string, variant: Variant = "default"): W | null {
  const community = variant === "community";
  const writes = variant !== "readonly";
  const disabled = community ? { auto_deploy: "Paused in Community" } : {};
  switch (id) {
    case "t_live":
      return {
        id: "t_live", app: "invoice", env: "prod", kind: "stack", health: UP, since: at(-180),
        toggles: { auto_deploy: true, paused: false },
        where: { host: hostRef("h_ready", "edge-1"), repo: "acme/invoice", ref: "main", manifest: "boss.deploy.yaml",
                 domain: "invoice.example.com", public_url: "https://invoice.example.com", account: null },
        strip: strip("up"), uptime_24h: 0.994,
        recent: ["stack:sd_wait", "stack:sd_live", "stack:sd_rb"].map(deployRow),
        secrets: [{ name: "ssh_key", kind: "ssh_private_key", status: "active" }, { name: "DATABASE_URL", kind: "api_token", status: "active" }],
        can: { deploy: writes, auto_deploy: writes && !community, move: writes, retire: writes, check: true }, disabled, etag: '"t-live"',
      };
    case "t_down":
      return {
        id: "t_down", app: "api", env: "prod", kind: "stack", health: DOWN, since: at(-60),
        toggles: { auto_deploy: false, paused: false },
        where: { host: hostRef("h_down", "edge-2"), repo: "acme/api", ref: "main", manifest: "boss.deploy.yaml",
                 domain: null, public_url: null, account: null },
        strip: strip("down"), uptime_24h: 0.875, recent: ["stack:sd_failed", "stack:sd_ok"].map(deployRow),
        secrets: [], can: { deploy: writes, auto_deploy: writes && !community, move: writes, retire: writes, check: true }, disabled, etag: '"t-down"',
      };
    case "t_cloud":
      return {
        id: "t_cloud", app: "billing", env: "prod", kind: "digitalocean_app", health: NOT_DEPLOYED, since: null,
        toggles: { auto_deploy: false, paused: false },
        where: { host: null, repo: "acme/billing", ref: "v1.4.0", manifest: null, domain: "billing.example.com",
                 public_url: "https://billing.example.com", account: { id: "acct_1", name: "DigitalOcean · prod" } },
        strip: strip("none"), uptime_24h: null, recent: [deployRow("registry:dr_1")],
        secrets: [{ name: "DO_TOKEN", kind: "api_token", status: "active" }],
        can: { deploy: writes && !community, auto_deploy: writes && !community, move: false, retire: writes, check: true },
        disabled: community ? { auto_deploy: "Paused in Community", deploy: "Paused in Community" } : {}, etag: '"t-cloud"',
      };
    default:
      return null;
  }
}

// ── Previews (GET /v1/infra/previews) ───────────────────────────────────────────────────────

export function previewRow(id: string): W {
  const rows: Record<string, W> = {
    pv_live: { id: "pv_live", run_id: "run_search_1", app: "search-revamp", branch: "feat/search", state: "running", word: "Live", tone: "green",
               substrate: "Docker", port: 4127, host: hostRef("this", "boss-1"), url: "/preview/pv_live/", created_at: at(-19),
               expires_at: at(41), last_seen_at: at(-1), can: { open: true, log: true, stop: true, restart: true } },
    pv_start: { id: "pv_start", run_id: "run_inv_1", app: "invoice", branch: "fix/rounding", state: "starting", word: "Starting", tone: "accent",
                substrate: "microVM", port: 4131, host: hostRef("this", "boss-1"), url: null, created_at: at(-56),
                expires_at: at(4), last_seen_at: null, can: { open: false, log: true, stop: true, restart: false } },
    pv_fail: { id: "pv_fail", run_id: "run_api_3", app: "api", branch: "main", state: "failed", word: "Failed · Port never opened", tone: "red",
               substrate: "Process", port: 4120, host: hostRef("this", "boss-1"), url: null, created_at: at(-41),
               expires_at: at(-40), last_seen_at: null, can: { open: false, log: true, stop: false, restart: true } },
    pv_end: { id: "pv_end", run_id: "run_inv_0", app: "invoice", branch: "main", state: "stopped", word: "Stopped", tone: "grey",
              substrate: "Rootfs", port: 4119, host: hostRef("this", "boss-1"), url: null, created_at: at(-300),
              expires_at: at(-240), last_seen_at: at(-250), can: { open: false, log: true, stop: false, restart: true } },
  };
  return rows[id] ?? rows.pv_live;
}
export const previewsFixture = (): W => ({
  counts: { live: 2, failed_24h: 1, ended_24h: 1 },
  live: [previewRow("pv_live"), previewRow("pv_start")], failed_24h: [previewRow("pv_fail")], ended_24h: [previewRow("pv_end")],
  partial: [], generated_at: at(0),
});
export const previewLogFixture = (): W => ({
  lines: ["> next start -p 4127", "ready - started server on 0.0.0.0:4127"], status: "running",
});

// ── the other reads the drawers make ────────────────────────────────────────────────────────

export const secretsFixture = (): W => ({
  project_id: "project_1",
  secrets: [
    { secret_ref_id: "sec_1", name: "ssh_key", backend: "database", secret_kind: "ssh_private_key", status: "active",
      fingerprint: "SHA256:abcdefabcdefabcdefabcdef", last4: null, bound_to: ["t_live"], last_verified_at: at(-600), created_at: at(-9000) },
    { secret_ref_id: "sec_2", name: "DATABASE_URL", backend: "platform_secret_store", secret_kind: "api_token", status: "active",
      fingerprint: null, last4: "9xQz", bound_to: [], last_verified_at: null, created_at: at(-800) },
  ],
});
export const refsFixture = (): W => ({ default_branch: "main", branches: [{ name: "main", sha: "a1b2c3d" }, { name: "feat/search", sha: "5e6f7a8" }] });
/** deployment_connectors.preflight(): `ready` plus the concrete list of what is missing. */
export const preflightFixture = (): W => ({
  ready: false, problems: ["port 4100 is held by another process"], connector: "ssh", required_secrets: ["ssh_key"],
});
export const repositoriesFixture = (): W => ({
  repositories: [{ full_name: "acme/invoice", role: "primary" }, { full_name: "acme/api" }, { full_name: "acme/billing" }],
  suggested: "acme/invoice",
});
export const hardwarePlacesFixture = (): W => ({
  places: [{ id: "place_local", kind: "local", name: "This computer", hostname: "boss-1", arch: "arm64", os: "linux", agent_state: "installed" }],
  default_place_id: "place_local",
});
export const policiesFixture = (): W => ({
  items: [{ id: "pol_1", environment: "production", runtime_mode: "production_allowed", allowed_target_kinds: ["digitalocean_app"],
            requires_change_set: true, requires_budget_reservation: false, requires_approval: true, requires_health_check: true,
            requires_rollback_plan: true, status: "active" }],
});

// ── mockInfraFetch ──────────────────────────────────────────────────────────────────────────

export type FetchCall = { method: string; url: string; path: string; body: unknown };
/** A route: answer `reply(url, body)` when `match` fits. A Response is returned as is; any
 *  other value becomes a 200 JSON body; `undefined` falls through to the next route. */
export type Route = {
  method?: string;
  match: RegExp;
  reply: (url: URL, body: unknown, call: FetchCall) => unknown;
  /** ETag'd like the API's `_etagged`: an unchanged body answers 304 to If-None-Match. */
  etag?: boolean;
};

function hash(s: string): string {
  let h = 2166136261;
  for (let i = 0; i < s.length; i += 1) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  return (h >>> 0).toString(16);
}
const strip304 = (body: unknown) => {
  if (!body || typeof body !== "object" || Array.isArray(body)) return body;
  const { generated_at: _g, etag: _e, ...rest } = body as W;
  return rest;
};

export function infraRoutes(variant: Variant = "default"): Route[] {
  const id = (u: URL, i: number) => decodeURIComponent(u.pathname.split("/")[i] ?? "");
  const notFound = () => new Response(JSON.stringify({ detail: "Not found." }), { status: 404, headers: { "Content-Type": "application/json" } });
  return [
    { match: /^\/v1\/infra\/fleet$/, etag: true, reply: () => fleetFixture(variant) },
    { match: /^\/v1\/infra\/hosts\/[^/]+$/, etag: true, reply: (u) => hostFixture(id(u, 4), variant) ?? notFound() },
    { match: /^\/v1\/infra\/targets\/[^/]+$/, method: "GET", etag: true, reply: (u) => targetFixture(id(u, 4), variant) ?? notFound() },
    { match: /^\/v1\/infra\/deploys$/, etag: true, reply: (u) => (u.searchParams.get("cursor") === "cur_2" ? deploysOlderFixture()
      : deploysFixture({ state: u.searchParams.get("state"), app: u.searchParams.get("app"), env: u.searchParams.get("env") })) },
    { match: /^\/v1\/infra\/deploys\/[^/]+$/, method: "GET", etag: true, reply: (u) => deployDetailFixture(id(u, 4)) },
    { match: /^\/v1\/infra\/previews$/, etag: true, reply: () => previewsFixture() },
    ...CLOUD_ROUTES,
    ...DEPLOY_EXTRA_ROUTES,
    { match: /^\/v1\/secrets$/, method: "GET", reply: () => secretsFixture() },
    { match: /^\/v1\/delivery\/repos\/[^/]+\/[^/]+\/refs$/, reply: () => refsFixture() },
    { match: /^\/v1\/delivery\/targets\/[^/]+\/preflight$/, reply: () => preflightFixture() },
    { match: /^\/v1\/projects\/[^/]+\/repositories$/, reply: () => repositoriesFixture() },
    { match: /^\/v1\/previews\/[^/]+\/logs$/, reply: () => previewLogFixture() },
    { match: /^\/api\/preview-logs/, reply: () => previewLogFixture() },
    { match: /^\/v1\/deployment-environment-policies$/, method: "GET", reply: () => policiesFixture() },
    { match: /^\/api\/hardware\/places$/, method: "GET", reply: () => hardwarePlacesFixture() },
    { match: /^\/api\/hardware\/resolve$/, reply: () => (hardwarePlacesFixture().places as W[])[0] },
    // Writes: the answers the real routes give, enough for the next screen.
    { method: "POST", match: /^\/v1\/deployment-hosts$/, reply: (_u, b) => ({ id: "h_new", name: (b as W)?.name ?? "new", status: "registered" }) },
    { method: "POST", match: /^\/v1\/deployment-hosts\/[^/]+\/key$/, reply: () => ({ public_key: PUBLIC_KEY, install_command: "ssh-copy-id … && echo BOSS_KEY_INSTALLED" }) },
    { method: "POST", match: /^\/v1\/deployment-hosts\/[^/]+\/verify$/, reply: () => ({ status: "verified" }) },
    { method: "POST", match: /^\/v1\/infra\/targets\/[^/]+\/deploy$/, reply: () => ({ ref: "stack:sd_new" }) },
    { method: "POST", match: /^\/v1\/infra\/deploys\/[^/]+\/rollback$/, reply: () => ({ ref: "stack:sd_rollback" }) },
    { method: "POST", match: /^\/v1\/deployments\/stack\/targets$/, reply: () => ({ id: "t_new" }) },
    { method: "POST", match: /^\/v1\/deployment-targets$/, reply: () => ({ id: "t_new_app" }) },
    { method: "POST", match: /^\/v1\/deployments$/, reply: () => ({ id: "t_new_reg" }) },
    { method: "POST", match: /^\/v1\/secrets\/keypair$/, reply: () => ({ public_key: PUBLIC_KEY, fingerprint: "SHA256:new" }) },
    { method: "POST", match: /^\/v1\/secrets$/, reply: (_u, b) => ({ name: (b as W)?.name, fingerprint: null, last4: "abcd" }) },
    { method: "POST", match: /^\/v1\/secrets\/[^/]+\/verify$/, reply: () => ({ resolvable: true, fingerprint_unchanged: true }) },
    { method: "POST", match: /^\/api\/hardware\/places\/agents$/, reply: () => ({ command: "curl -fsSL https://boss.example/enroll | sh -s -- tok_123" }) },
    { method: "POST", match: /^\/api\/hardware\/places\/(local|providers)$/, reply: () => ({ id: "place_new", kind: "provider" }) },
  ];
}

/**
 * Route `fetch` to the fixtures. Every call is recorded (`calls`); `writes()` are the
 * non-GET ones with their JSON bodies. `routes` go first, so a test can override one answer.
 * Unrouted GETs answer `{}`; unrouted writes answer `{ok: true}`.
 */
export function mockInfraFetch(opts: { variant?: Variant; routes?: Route[] } = {}) {
  const calls: FetchCall[] = [];
  const routes = [...(opts.routes ?? []), ...infraRoutes(opts.variant ?? "default")];
  const fn = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const raw = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    const url = new URL(raw, "http://console.test");
    const method = (init?.method ?? "GET").toUpperCase();
    let body: unknown = null;
    if (init?.body && typeof init.body === "string") { try { body = JSON.parse(init.body); } catch { body = init.body; } }
    const call: FetchCall = { method, url: raw, path: url.pathname, body };
    calls.push(call);
    for (const r of routes) {
      if ((r.method ?? "GET") !== method) continue;
      if (!r.match.test(url.pathname)) continue;
      const got = r.reply(url, body, call);
      if (got === undefined) continue;
      if (got instanceof Response) return got;
      if (r.etag) {
        const tag = `"fx-${hash(JSON.stringify(strip304(got)))}"`;
        const sent = new Headers(init?.headers).get("If-None-Match");
        if (sent && sent === tag) return new Response(null, { status: 304, headers: { etag: tag } });
        return new Response(JSON.stringify(got), { status: 200, headers: { "Content-Type": "application/json", etag: tag } });
      }
      return new Response(JSON.stringify(got), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    return new Response(JSON.stringify(method === "GET" ? {} : { ok: true }), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  return {
    fetch: fn as typeof fetch,
    calls,
    writes: () => calls.filter((c) => c.method !== "GET"),
    gets: (re: RegExp) => calls.filter((c) => c.method === "GET" && re.test(c.path)),
    /** Install as `globalThis.fetch`; returns the uninstaller. */
    install(): () => void {
      const prev = globalThis.fetch;
      globalThis.fetch = fn as typeof fetch;
      return () => { globalThis.fetch = prev; };
    },
  };
}
