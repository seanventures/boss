/**
 * SAMPLE DATA for the Infra Agent's storage screens (`?infra_demo=1`, the tests and the
 * wireframe walk). The numbers are the wireframes' (S1–S5, BOSSAPP-1156): the 14 November
 * afternoon, 358 pre-upgrade backups, a Docker VM holding 130 GB for 64 GB of use. Shapes are
 * the WIRE's (docs/infra/INFRA_AGENT_API.md, snake_case), parsed by lib/infra-storage.ts like a
 * real answer, so the sample and the API can never drift apart silently.
 */
import {
  parseAsk, parseCurrent, parseIncidentDetail, parseSummary,
  type CurrentIncidents, type IncidentDetail, type StorageAsk, type StorageSummary,
} from "./infra-storage";

export type DemoMode = "1" | "asking" | "resolved" | "quiet";

const HOST = "laptop-boss";
const HOST_LABEL = "laptop-boss";
const INCIDENT_ID = "si_20261114_laptop";
const DECISION_ID = "dec_storage_backups_1";
const DAY = "2026-11-14";
const at = (hm: string) => `${DAY}T${hm}:00Z`;

const BACKUP_RULE = { keep_newest: 10, keep_days: 7, cap_gb: 20 };

function plan(asking: boolean, resolved: boolean) {
  const s = (i: number) => (resolved ? "done" : i < 2 ? "done" : i === 2 ? (asking ? "done" : "running") : asking ? "done" : "planned");
  const step = (tool: string, label: string, tier: number, state: string, items: number | null, gbv: number | null, extra: Record<string, unknown> = {}) => ({
    tool, tier, where: tool === "storage.remove_pg_temp" || tool === "storage.drop_scratch_dbs" ? "api" : "host", label, state, items, gb: gbv,
    freed_gb: state === "done" ? gbv ?? 0 : 0, plan_hash: "ph_" + tool.slice(8, 14), decision_id: null, receipt_id: state === "done" ? `rcp_${tool.slice(8, 14)}` : null,
    action_id: `act_${tool.slice(8, 14)}`, reason: null, ...extra,
  });
  return [
    step("storage.prune_build_cache", "Build cache", 1, s(0), 1, 12, { detail: "docker builder prune · cap 10 GB" }),
    step("storage.remove_images", "Old images", 1, s(1), 19, 26, { detail: "kept current + previous version" }),
    step("storage.remove_pg_temp", "Postgres temp files", 1, s(2), 2, resolved || asking ? 11 : null, { detail: "of sessions no longer running" }),
    step("storage.remove_orphan_volumes", "Orphan volumes", 1, s(3), 13, 4.4, { detail: "13 volumes labelled boss/test, older than 1 day" }),
    step("storage.docker_trim", "Logs · scratch DBs · Docker trim", 1, s(4), null, 5, { detail: "container logs, boss_scratch_* over 24 h, hand space back to macOS" }),
    step("storage.remove_backups", "Backups past retention", 3, resolved ? "done" : "ask", 348, 161, { detail: "348 of 358 · asks you first", decision_id: DECISION_ID, receipt_id: resolved ? "rcp_backups" : null }),
  ];
}

function flow(asking: boolean, resolved: boolean, freed: number) {
  const node = (key: string, type: string, label: string, state: string, detail: string, when: string | null, icon: string) =>
    ({ key, type, label, badges: [detail], icon, state, at: when, detail });
  return [
    node("detected", "signal", "Detected", "done", "9 % free", at("18:02"), "Warning"),
    node("paused", "action", "Paused", "done", "14 runs", at("18:02"), "Pause"),
    node("cleaning", "action", resolved ? "Cleaned" : "Cleaning", resolved || asking ? "done" : "active", resolved ? "42 GB" : "3 of 6", resolved || asking ? at("18:03") : at("18:03"), "Broom"),
    node("asking", "needs_you", resolved ? "Asked" : "Retention", resolved ? "done" : asking ? "ask" : "waiting", resolved ? "Alice ✓" : "348 backups", resolved || asking ? at("18:09") : null, "Archive"),
    node("freed", "outcome", resolved ? "Removed" : "Freed", resolved ? "done" : "waiting", resolved ? "161 GB" : `${freed} GB so far`, resolved ? at("18:12") : null, "Check"),
    node("resumed", "outcome", "Resumed", resolved ? "done" : "waiting", resolved ? "14 runs" : "waits", resolved ? at("18:14") : null, "Play"),
    ...(resolved ? [node("reported", "learning", "Learned", "done", "1 rule", at("18:21"), "Sparkle")] : []),
  ];
}

const REPORT = {
  summary: "Backups with no retention filled the disk; the Infra Agent paused 14 runs, freed 203 GB and resumed them. One rule was added.",
  freed_gb: 203, paused_runs: 14, waited_seconds_max: 11 * 60, lost_or_repeated: 0, cost_usd: 0.11, duration_seconds: 19 * 60,
  cause: { key: "backups.no_retention", text: "Every upgrade wrote a 1 GB backup and nothing removed the old ones: 358 backups, 170 GB, since 30 September. A test query also spilled 11 GB of temp files twice today because Postgres had no temp-space limit.",
           chips: ["no retention", "no temp-space limit", "Docker VM never trimmed"] },
  removed: [
    { type: "Pre-upgrade backups", detail: "348 files", by: "person", gb: 161, receipt_id: "rcp_backups" },
    { type: "Old images", detail: "19 images", by: "rebuildable", gb: 26, receipt_id: "rcp_images" },
    { type: "Build cache", detail: "", by: "rebuildable", gb: 12, receipt_id: "rcp_cache" },
    { type: "Orphan volumes", detail: "13 volumes", by: "rebuildable", gb: 4.4, receipt_id: "rcp_volumes" },
    { type: "Postgres temp files", detail: "dead sessions", by: "rebuildable", gb: 11, receipt_id: "rcp_pgtemp" },
    { type: "Scratch databases", detail: "boss_scratch_* · 2", by: "rule", gb: 2.1, receipt_id: "rcp_scratch" },
    { type: "Container logs", detail: "", by: "rule", gb: 0.6, receipt_id: "rcp_logs" },
    { type: "Docker-on-Mac trim", detail: "66 GB back to macOS", by: "rebuildable", gb: 66, receipt_id: "rcp_trim" },
  ],
  learned: {
    rule_added: { id: "r_backups", host_id: null, kind: "backups", rule: BACKUP_RULE, enabled: true, source: "learned", decision_id: DECISION_ID, created_by: "Alice", created_at: at("18:11"), updated_at: null, label: "Pre-upgrade backups" },
    rule_proposed: { decision_id: "dec_rule_pgtemp", kind: "pg_temp", rule: { cap_gb: 5 } },
  },
  timeline: flow(false, true, 203),
};

function incidentWire(mode: DemoMode, tick: number): Record<string, unknown> | null {
  if (mode === "quiet") return null;
  const asking = mode === "asking";
  const resolved = mode === "resolved";
  const freed = resolved ? 203 : asking ? 42 : Math.min(42, 38 + tick);
  const freeNow = resolved ? 61 : asking ? 17 : Math.min(17, 15 + tick * 0.5);
  return {
    id: INCIDENT_ID, host_id: HOST, host_label: HOST_LABEL, status: resolved ? "resolved" : asking ? "asking" : "critical", severity: "critical",
    opened_at: at("18:02"), resolved_at: resolved ? at("18:21") : null,
    cause_key: "backups.no_retention", cause_text: "backups grew 18 GB this week, one per upgrade, none removed",
    free_pct_start: 9, free_pct_now: freeNow, target_pct: 25,
    freed_gb: freed, eta_seconds: resolved ? 0 : asking ? null : Math.max(60, 360 - tick * 10),
    hold: { active: !resolved, since: at("18:02"), released_at: resolved ? at("18:14") : null, paused_runs: resolved ? 0 : 14 },
    plan: plan(asking, resolved), flow: flow(asking, resolved, freed),
    acknowledged_by: null, acknowledged_at: null, spend_usd: resolved ? 0.11 : 0.04, agent_run_id: "run_infra_1",
    helper: { last_seen_at: at("18:10"), reachable: true },
    report: resolved ? REPORT : null,
  };
}

export function demoIncident(mode: DemoMode, tick = 0): CurrentIncidents {
  const w = incidentWire(mode, tick);
  const open = w && w.status !== "resolved" ? [w] : [];
  return parseCurrent({ incidents: open, generated_at: at("18:10"),
    hold: { active: open.length > 0, incident_id: open.length ? INCIDENT_ID : null, host_id: open.length ? HOST : null, since: open.length ? at("18:02") : null,
            scope: "all", exempt_kinds: ["agent.turn.interactive", "infra.*"], actor: "agent:infra", paused_runs: open.length ? 14 : 0, released_at: null } });
}

export function demoIncidentDetail(incidentId: string): IncidentDetail | null {
  const incident = incidentWire("resolved", 0);
  if (!incident) return null;
  return parseIncidentDetail({
    incident: { ...incident, id: incidentId, acknowledged_by: "Alice", acknowledged_at: at("18:04") },
    actions: Array.from({ length: 9 }, (_, i) => ({ id: `sa_${i}`, tool: "storage.x", tier: 1, where: "host", dry_run: i % 2 === 0, status: "done", items: 1, bytes: 1e9, plan_hash: "ph", receipt_id: `rcp_${i}`, decision_id: null, actor: "agent:infra", started_at: null, ended_at: null, result: {} })),
    receipts: Array.from({ length: 9 }, (_, i) => ({ id: `rcp_${i}`, dedupe_key: `k${i}`, effect_kind: "storage", status: "done", created_at: at("18:10") })),
    paused_work: [
      { job_id: "job_4127", kind: "code_task", run_id: "4127", title: "AMEX statements import", paused_at: at("18:02"), resumed_at: at("18:13"), waited_seconds: 11 * 60 },
      { job_id: "job_4129", kind: "workflow", run_id: "4129", title: "HelpDesk PR #88 review", paused_at: at("18:03"), resumed_at: at("18:13"), waited_seconds: 10 * 60 },
      { job_id: "job_4131", kind: "code_task", run_id: "4131", title: "NorthPeak checkout fix", paused_at: at("18:04"), resumed_at: at("18:13"), waited_seconds: 9 * 60 },
    ],
    decisions: [{ id: DECISION_ID, kind: "storage_delete", status: "approved", question: "Delete 348 old backups?", created_at: at("18:09") },
                { id: "dec_rule_pgtemp", kind: "storage_rule", status: "pending", question: "Cap Postgres temp space at 5 GB per query?", created_at: at("18:21") }],
  });
}

const BACKUP_NAMES = ["boss-backup-2026-09-30T08-12.tar.zst", "boss-backup-2026-10-01T09-40.tar.zst", "boss-backup-2026-10-01T14-02.tar.zst", "boss-backup-2026-10-02T07-58.tar.zst", "boss-backup-2026-11-04T17-55.tar.zst"];

export function demoDecision(decisionId: string): StorageAsk | null {
  if (decisionId !== DECISION_ID) return null;
  return parseAsk({
    id: DECISION_ID, status: "pending", question: "Delete 348 old backups?", created_at: at("18:09"),
    recommendation: {
      action: "storage_delete", incident_id: INCIDENT_ID, host_id: HOST, tool: "storage.remove_backups",
      args: BACKUP_RULE, plan_hash: "ph_7c1e9a",
      items: 348, gb: 161.2, keeps: 10, to_delete: BACKUP_NAMES, kept: ["boss-backup-2026-11-14T17-40.tar.zst", "boss-backup-2026-11-13T22-10.tar.zst"],
      already_done_gb: 42.0, free_pct_now: 17, target_pct: 25, paused_runs: 14,
      rule_proposal: { kind: "backups", rule: BACKUP_RULE, preticked: true },
      alternatives: [{ label: "Keep 20 instead", args: { keep_newest: 20 } }],
      never_automatic: ["backups", "databases", "data volumes", "anything unlabelled"],
    },
  });
}

/** 30 days of used GB climbing 2.1 GB/day to 436 of 480. */
function trend(resolved: boolean) {
  const start = Date.parse("2026-10-15T18:00:00Z");
  return Array.from({ length: 30 }, (_, i) => {
    const used = Math.round(375 + i * 2.1 + (i > 25 ? (i - 25) * 3 : 0)) - (resolved && i === 29 ? 203 : 0);
    return { at: new Date(start + i * 86_400_000).toISOString(), used_gb: used, free_gb: 480 - used };
  });
}

export function demoSummary(mode: DemoMode, hostId: string | null): StorageSummary | null {
  const resolved = mode === "resolved" || mode === "quiet";
  const freePct = resolved ? 61 : mode === "asking" ? 17 : 15;
  const total = 480;
  const free = Math.round((total * freePct) / 100);
  const types = resolved
    ? [["backups", "Backups", 9.6, false], ["images", "Images", 8, true], ["pg_db", "Database", 22, false], ["build_cache", "Caches", 2, true], ["volumes", "Volumes", 1.2, false], ["logs", "Logs", 0.1, true], ["other", "Graph & other", 4.4, true]]
    : [["backups", "Backups", 170, false], ["images", "Images", 34, true], ["pg_db", "Database", 22, false], ["build_cache", "Caches", 10, true], ["volumes", "Volumes", 4.4, false], ["logs", "Logs", 0.7, true], ["other", "Graph & other", 4.4, true], ["docker_vm_unreclaimed", "Docker VM (unreclaimed)", 66, true]];
  const rule = (id: string, kind: string, label: string, r: Record<string, unknown>, source = "default") =>
    ({ id, host_id: null, kind, rule: r, enabled: true, source, decision_id: null, created_by: null, created_at: at("00:00"), updated_at: null, label });
  return parseSummary({
    host_id: hostId || HOST, host_label: hostId || HOST_LABEL, sampled_at: at("18:00"), source: "steward", stale: false,
    disk: { total_gb: total, free_gb: free, used_pct: 100 - freePct, free_pct: freePct },
    by_type: [...types.map(([key, label, gbv, rebuildable]) => ({ key, label, gb: gbv, pct: Math.round(((gbv as number) / total) * 1000) / 10, rebuildable })),
              { key: "free", label: "Free", gb: free, pct: freePct, rebuildable: false }],
    forecast: { growth_gb_per_day: 2.1, days_until_full: resolved ? 84 : 1.8, full_at: null, window_days: 7, samples: 20160, with_rules_days_until_full: resolved ? 400 : 120 },
    trend: trend(resolved),
    thresholds: { warn_free_pct: 20, critical_free_pct: 15, target_free_pct: 25, floor_free_pct: 10, floor_free_gb: 20, warn_days: 7, critical_days: 3, pg_temp_critical_gb: 5, docker_unreclaimed_gb: 20 },
    level: resolved ? "ok" : mode === "asking" ? "warn" : "critical",
    rules: [
      rule("r_backups", "backups", "Pre-upgrade backups", BACKUP_RULE, resolved ? "learned" : "default"),
      rule("r_images", "images", "Docker images", { keep_versions: 2 }),
      rule("r_cache", "build_cache", "Build cache", { cap_gb: 10 }),
      rule("r_logs", "logs", "Container logs", { max_size_mb: 50, max_file: 3 }),
      rule("r_telemetry", "telemetry", "Telemetry tables", { keep_days: 30, tables: {} }),
      rule("r_scratch", "scratch_db", "Scratch databases", { ttl_hours: 24 }),
      rule("r_kg", "kg_derived", "Graph views & vectors", { unused_days: 14 }),
      rule("r_trim", "docker_trim", "Docker-on-Mac trim", { enabled: true, schedule: "weekly · and after every cleanup" }),
    ],
    last_cleanup: resolved ? { incident_id: INCIDENT_ID, at: at("18:14"), freed_gb: 203, cause_key: "backups.no_retention" } : { incident_id: "si_20261102_laptop", at: "2026-11-02T09:10:00Z", freed_gb: 12, cause_key: "images.no_retention" },
    open_incident_id: resolved ? null : INCIDENT_ID,
    telemetry: {
      gb: 1.62, share_pct: 21.4,
      tables: [
        { table: "events", gb: 0.52, keep_days: 90, partitioned: true },
        { table: "receipts", gb: 0.42, keep_days: 30, partitioned: false },
        { table: "anomaly_signals", gb: 0.31, keep_days: 30, partitioned: false },
        { table: "agent_turns", gb: 0.13, keep_days: null, partitioned: false },
        { table: "app_metrics_scrapes", gb: 0.11, keep_days: 7, partitioned: false },
      ],
      last_sweep: { at: "2026-11-14T17:00:00Z", rows: 182_340, bytes: 214_000_000, partitions: [], archived: 0, errors: [], receipt_id: "rcpt_demo_sweep" },
    },
    hosts: [
      { host_id: HOST, host_label: HOST_LABEL, free_pct: freePct, days_until_full: resolved ? 84 : 1.8, level: resolved ? "ok" : "critical", spark: [60, 58, 55, 52, 40, 30, freePct] },
      { host_id: "boss-1", host_label: "boss-1", free_pct: 41, days_until_full: 41, level: "ok", spark: [44, 44, 43, 43, 42, 41, 41] },
      { host_id: "kvm-a", host_label: "kvm-a · Fleet doctor", free_pct: 63, days_until_full: 120, level: "ok", spark: [63, 63, 63, 63, 63, 63, 63] },
    ],
  });
}
