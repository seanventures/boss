/**
 * The Infra Agent's storage wire (docs/infra/INFRA_AGENT_API.md) is parsed defensively: a field
 * the API does not send yet is null or empty, never a crash, and the words the console says
 * ("38 GB", "~6 min left", "full in 84 days") come from one place (lib/infra-storage.ts).
 */
import { describe, expect, it } from "vitest";

import {
  daysWord, etaWords, fullInDays, gb, openIncident, parseAsk, parseCurrent, parseFlowNode, parseIncident, parseIncidentDetail,
  parsePlanStep, parseSummary, progressToTarget, ruleWords, storageTone, stripHeadline, stripSubline, whenWord, withInfraStatus,
} from "../lib/infra-storage";
import { demoDecision, demoIncident, demoIncidentDetail, demoSummary } from "../lib/infra-storage-demo";

describe("the storage incident wire", () => {
  it("parses the sample incident into the console's shape (the flow is StripNodes with a state and a time)", () => {
    const i = openIncident(demoIncident("1"))!;
    expect(i.id).toBe("si_20261114_laptop");
    expect(i.status).toBe("critical");
    expect(i.hostLabel).toBe("laptop-boss");
    expect(i.flow.map((n) => n.key)).toEqual(["detected", "paused", "cleaning", "asking", "freed", "resumed"]);
    expect(i.flow.map((n) => n.state)).toEqual(["done", "done", "active", "waiting", "waiting", "waiting"]);
    expect(i.flow[0]).toMatchObject({ type: "signal", label: "Detected", detail: "9 % free", at: "2026-11-14T18:02:00Z" });
    expect(i.plan).toHaveLength(6);
    expect(i.plan.find((s) => s.tier === 3)?.state).toBe("ask");
    expect(i.askDecisionId).toBe("dec_storage_backups_1");
    expect(i.pausedRuns).toBe(14);
    expect(i.hold.active).toBe(true);
    expect(i.stepIndex).toBe(3);
    expect(i.stepCount).toBe(6);
  });

  it("never crashes on a thin or odd answer", () => {
    expect(parseIncident(null)).toBeNull();
    expect(parseIncident({})).toBeNull();
    expect(parseIncident({ id: "x" })).toMatchObject({ id: "x", status: "critical", hostId: "this host", hostLabel: "this host", flow: [], plan: [], askDecisionId: null, freedGb: 0, targetPct: 25, report: null });
    expect(parseIncident({ id: "x", status: "nonsense", flow: [{ type: "banana", label: "Detected" }, { label: "" }] })!.flow)
      .toMatchObject([{ type: "action", label: "Detected", state: "waiting", at: null, detail: "" }]);
    expect(parseFlowNode({ type: "signal", label: "a very long label that goes on and on", badges: ["done"] })!.label.split(" ").length).toBeLessThanOrEqual(4);
    // An older shape with the state and time among the badges reads the same way.
    expect(parseFlowNode({ type: "signal", label: "Detected", badges: ["9 % free", "18:02", "done"] })).toMatchObject({ state: "done", at: "18:02", detail: "9 % free", badges: ["9 % free", "18:02"] });
  });

  it("maps the API's plan step states to the console's marks", () => {
    expect(parsePlanStep({ tool: "storage.prune_build_cache", tier: 1, label: "Build cache", state: "running", items: 1, gb: 12 })).toMatchObject({ state: "active", key: "storage.prune_build_cache", detail: "1 item" });
    expect(parsePlanStep({ tool: "storage.remove_images", tier: 1, state: "refused", reason: "an image carried no BOSS label" })).toMatchObject({ state: "failed", detail: "an image carried no BOSS label", label: "remove images" });
    expect(parsePlanStep({ tool: "storage.remove_backups", tier: 3, state: "ask", items: 348, gb: 161, decision_id: "d1" })).toMatchObject({ state: "ask", decisionId: "d1", detail: "348 items · asks you first" });
    expect(parsePlanStep({ tool: "x", state: "skipped", reason: "nothing to do" })).toMatchObject({ state: "skipped", detail: "nothing to do" });
    expect(parsePlanStep({})).toBeNull();
  });

  it("takes the first open incident from the envelope, a list, or one object", () => {
    const resolved = { id: "a", status: "resolved" };
    const open = { id: "b", status: "asking" };
    expect(openIncident(parseCurrent({ incidents: [resolved, open], hold: { active: true, paused_runs: 3 } }))!.id).toBe("b");
    expect(parseCurrent({ incidents: [resolved, open], hold: { active: true, paused_runs: 3 } }).hold).toMatchObject({ active: true, pausedRuns: 3 });
    expect(openIncident(parseCurrent([open]))!.id).toBe("b");
    expect(openIncident(parseCurrent({ incident: open }))!.id).toBe("b");
    expect(openIncident(parseCurrent(open))!.id).toBe("b");
    expect(openIncident(parseCurrent({ incidents: [] }))).toBeNull();
    expect(openIncident(parseCurrent(null))).toBeNull();
    expect(openIncident(null)).toBeNull();
  });

  it("says the strip's two lines from the incident", () => {
    const i = openIncident(demoIncident("1"))!;
    expect(stripHeadline(i)).toBe("Storage critical on laptop-boss · Infra Agent is cleaning");
    expect(stripSubline(i)).toMatch(/^\d+ % free · 14 runs paused at a checkpoint · nothing is lost$/);
    expect(stripHeadline(openIncident(demoIncident("asking"))!)).toContain("needs one answer");
    expect(stripSubline({ ...i, helper: { lastSeenAt: null, reachable: false } })).toContain("host helper unreachable");
  });

  it("measures progress to the target and the Command Center status", () => {
    expect(progressToTarget({ freePctStart: 9, freePctNow: 17, targetPct: 25 })).toBeCloseTo(0.5);
    expect(progressToTarget({ freePctStart: 9, freePctNow: 30, targetPct: 25 })).toBe(1);
    expect(progressToTarget({ freePctStart: null, freePctNow: null, targetPct: 25 })).toBe(0);
    const base = { key: "needs_you", text: "3 need you", tone: "amber", tip: "" };
    const open = openIncident(demoIncident("1"));
    expect(withInfraStatus(base, open)).toMatchObject({ key: "infra_incident", text: "Infra Agent is cleaning", tone: "red" });
    expect(withInfraStatus({ ...base, key: "unreachable" }, open).key).toBe("unreachable");
    expect(withInfraStatus({ ...base, key: "budget_stopped" }, open).key).toBe("budget_stopped");
    expect(withInfraStatus(base, null)).toBe(base);
  });
});

describe("the Command Center view", () => {
  it("carries infra_incident when the API sends it, and leaves it undefined when it does not", async () => {
    const { parseCommandCenterView } = await import("../lib/command-center-view");
    expect(parseCommandCenterView({ project_id: "p", infra_incident: { id: "si_1", status: "critical" } }).infraIncident?.id).toBe("si_1");
    expect(parseCommandCenterView({ project_id: "p", infra_incident: null }).infraIncident).toBeNull();
    expect(parseCommandCenterView({ project_id: "p" }).infraIncident).toBeUndefined();
  });
});

describe("the tier 3 question", () => {
  it("is read from the decision's recommendation", () => {
    const ask = demoDecision("dec_storage_backups_1")!;
    expect(ask).toMatchObject({ decisionId: "dec_storage_backups_1", tool: "storage.remove_backups", count: 348, gb: 161.2, keeps: 10, planHash: "ph_7c1e9a", pausedRuns: 14, freePctNow: 17 });
    expect(ask.rule).toEqual({ kind: "backups", words: "keep 10 · 7 days · 20 GB cap", preticked: true });
    expect(ask.alternatives).toEqual([{ label: "Keep 20 instead", args: { keep_newest: 20 } }]);
    expect(ask.toDelete).toHaveLength(5);
    expect(parseAsk({ id: "d", recommendation: { action: "deploy" } })).toBeNull();
    expect(parseAsk(null)).toBeNull();
    expect(demoDecision("other")).toBeNull();
  });
});

describe("the storage summary wire", () => {
  it("parses the sample summary (disk, by type, forecast, trend, thresholds, rules, hosts)", () => {
    const s = demoSummary("resolved", null)!;
    expect(s.hostId).toBe("laptop-boss");
    expect(s.freePct).toBe(61);
    expect(s.byType.map((t) => t.key)).toContain("backups");
    expect(s.trend).toHaveLength(30);
    expect(s.fullInDays).toBe(84);
    expect(s.thresholds).toMatchObject({ warnPct: 20, criticalPct: 15, targetPct: 25, floorPct: 10 });
    expect(s.rules).toHaveLength(8);
    expect(s.rules[0].words).toBe("keep 10 · 7 days · 20 GB cap");
    expect(s.hosts.map((h) => h.hostId)).toEqual(["laptop-boss", "boss-1", "kvm-a"]);
    expect(s.level).toBe("ok");
    expect(demoSummary("1", null)!.docker).toEqual({ unreclaimedGb: 66 });
  });

  it("fills what is missing with safe defaults", () => {
    expect(parseSummary(null)).toBeNull();
    const s = parseSummary({ host_id: "h", disk: { total_gb: 100, free_gb: 30 } })!;
    expect(s.freePct).toBe(30);
    expect(s.thresholds).toMatchObject({ warnPct: 20, criticalPct: 15, floorPct: 10, targetPct: 25 });
    expect(s.byType).toEqual([]);
    expect(s.lastCleanup).toBeNull();
    expect(s.docker).toBeNull();
    expect(s.fullInDays).toBeNull();
    expect(s.level).toBe("ok");
    expect(s.hostLabel).toBe("h");
  });

  it("says each rule in words", () => {
    expect(ruleWords("images", { keep_versions: 2 })).toBe("current + previous version");
    expect(ruleWords("build_cache", { cap_gb: 10 })).toBe("10 GB cap");
    expect(ruleWords("logs", { max_size_mb: 50, max_file: 3 })).toBe("50 MB × 3 files");
    expect(ruleWords("scratch_db", { ttl_hours: 24 })).toBe("24 h");
    expect(ruleWords("telemetry", { keep_days: 90 })).toBe("90 days");
    expect(ruleWords("mystery", { foo_bar: 7 })).toBe("foo bar 7");
  });

  it("forecasts 'full in N days' from growth when the server gives none", () => {
    expect(fullInDays(292, 2.1, 84)).toBe(84);
    expect(fullInDays(292, 2.1, null)).toBe(139);
    expect(fullInDays(292, 0, null)).toBeNull();
    expect(fullInDays(292, -1.2, null)).toBeNull();
    expect(daysWord(84)).toBe("84 days");
    expect(daysWord(1)).toBe("1 day");
    expect(daysWord(1.8)).toBe("1.8 days");
    expect(daysWord(0.2)).toBe("today");
    expect(daysWord(120)).toBe("> 90 days");
    expect(daysWord(null)).toBe("not filling");
  });

  it("is loud only under the thresholds: green above 20 %, amber to 15 %, red below", () => {
    const th = { warnPct: 20, criticalPct: 15 };
    expect(storageTone(61, th)).toBe("green");
    expect(storageTone(20, th)).toBe("amber");
    expect(storageTone(17, th)).toBe("amber");
    expect(storageTone(15, th)).toBe("red");
    expect(storageTone(9, th)).toBe("red");
  });
});

describe("the words", () => {
  it("say GB and time the way the wireframes do", () => {
    expect(gb(38)).toBe("38 GB");
    expect(gb(4.4)).toBe("4.4 GB");
    expect(gb(0.7)).toBe("0.7 GB");
    expect(gb(161.2)).toBe("161 GB");
    expect(gb(null)).toBe("…");
    expect(etaWords(360)).toBe("~6 min left");
    expect(etaWords(40)).toBe("~40 s left");
    expect(etaWords(null)).toBe("");
    expect(whenWord(new Date().toISOString())).toMatch(/today$/);
    expect(whenWord("2026-11-02T09:10:00Z")).not.toMatch(/today/);
    expect(whenWord(null)).toBe("");
  });
});

describe("the incident detail wire", () => {
  it("parses the sample detail with its report, receipts and paused work", () => {
    const d = demoIncidentDetail("si_1")!;
    expect(d.incident.id).toBe("si_1");
    expect(d.incident.status).toBe("resolved");
    const r = d.incident.report!;
    expect(r.freedGb).toBe(203);
    expect(r.lostOrRepeated).toBe(0);
    expect(r.removed.find((x) => x.by === "person")?.type).toBe("Pre-upgrade backups");
    expect(r.learned.ruleAdded?.words).toBe("keep 10 · 7 days · 20 GB cap");
    expect(r.learned.ruleProposed?.decisionId).toBe("dec_rule_pgtemp");
    expect(r.timeline.at(-1)?.key).toBe("reported");
    expect(d.pausedWork).toHaveLength(3);
    expect(d.receiptsCount).toBe(9);
    expect(d.toolCalls).toBe(9);
  });
  it("accepts a bare incident as a thin detail", () => {
    const d = parseIncidentDetail({ id: "x", status: "resolved", freed_gb: 12, spend_usd: 0.02 })!;
    expect(d.incident.freedGb).toBe(12);
    expect(d.incident.report).toBeNull();
    expect(d.pausedWork).toEqual([]);
    expect(parseIncidentDetail(null)).toBeNull();
  });
});
