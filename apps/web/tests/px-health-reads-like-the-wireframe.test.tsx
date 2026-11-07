// @vitest-environment happy-dom
/**
 * Project › Health reads like wireframe 12 (BOSSAPP-913/914, GAPS #13, ACCEPTANCE 12-01…12-27):
 * one card per environment with its four numbers (uptime ring + SLO, error rate and p95 with
 * sparklines, SLO burn with "% of budget used"); the ring and sparks open the environment drawer;
 * each incident is its own story (signal → SRE squad → cause → fix → recovered) with Graph ↗;
 * the Alerts card lists each rule by NAME with its severity and where it stands (resolved 09:26,
 * quiet 3d) — never "1 rule group(s) are…"; Beacon's pack shows what it watches and its file;
 * Go deeper; and below, Observability's own alert-rules component, every rule ONE LINE until ▸.
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { ProjectPage } from "../app/projects/project-page";
import { dayTime, spanWords } from "../app/projects/tab-health";
import {
  FX_DEPLOYMENTS, FX_HEALTH, FX_KNOWLEDGE, FX_ONBOARDING, FX_OVERVIEW, FX_SETTINGS, FX_SUMMARY, FX_WORK,
} from "../lib/project-experience-fixtures";
import { parseHealth } from "../lib/project-experience";
import { historyWire, rulesWire } from "./_helpers/alerts-fixtures";
import { $$, mountAs, stubFetch, tid, unmount, type Call } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const P = "helpdesk-ai-demo";
const settle = async (n = 8) => { for (let i = 0; i < n; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };
const press = async (el: Element | null) => { if (!el) throw new Error("nothing to press"); await act(async () => { (el as HTMLElement).click(); }); await settle(); };
const params = () => new URLSearchParams(window.location.search);

function engine(health: unknown = FX_HEALTH) {
  return stubFetch((c: Call) => {
    if (c.method !== "GET") return { body: { ok: true } };
    if (c.path === "/v1/projects/summary") return { body: FX_SUMMARY };
    const m = c.path.match(/^\/v1\/projects\/([^/]+)\/(overview|work|knowledge|deployments|onboarding|settings|health|captain)$/);
    if (m) {
      const table: Record<string, unknown> = { overview: FX_OVERVIEW, work: FX_WORK, knowledge: FX_KNOWLEDGE, deployments: FX_DEPLOYMENTS,
                                               onboarding: { ...FX_ONBOARDING, state: "ready" }, settings: FX_SETTINGS, health };
      return { body: table[m[2]] };
    }
    if (/\/alert-rules$/.test(c.path)) return { body: rulesWire("tgt_staging") };
    if (/\/v1\/apps\/[^/]+\/alerts$/.test(c.path)) return { body: historyWire() };
    return undefined;
  });
}
async function openHealth() {
  window.history.replaceState(null, "", `/?org=org_1&project_id=${P}&area=project&tab=health`);
  await mountAs(<ProjectPage projectId={P} actor="ceo" orgId="org_1" />, "owner");
  await settle();
}
const text = (id: string) => (tid(id)?.textContent ?? "").replace(/\s+/g, " ").trim();

beforeEach(() => { window.history.replaceState(null, "", "/"); });
afterEach(() => { unmount(); });

describe("Health (screen 12)", () => {
  it("draws the environment's four numbers, with the SLO and how much budget the day used", async () => {
    engine();
    await openHealth();
    const env = text("px-h-env-staging");
    expect(env).toContain("Staging");
    expect(env).toContain("Healthy");
    expect(env).toContain("last 24h");
    expect(text("px-h-env-uptime-staging")).toContain("SLO 99.5%");
    expect(text("px-h-env-errors-staging")).toContain("0.4%");
    expect(text("px-h-env-p95-staging")).toContain("180");
    expect(text("px-h-env-budget-staging")).toBe("22% of budget used");
    expect(tid("px-h-env-url-staging")!.getAttribute("title")).toBe("Opens the live app");
  });

  it("the ring and the sparklines open the environment drawer with its full charts", async () => {
    engine();
    await openHealth();
    await press(tid("px-h-env-errors-staging"));
    expect(params().get("drawer")).toBe("env:tgt_staging");
  });

  it("each incident is its own story — signal, SRE squad, cause, fix, recovered — with Graph ↗", async () => {
    engine();
    await openHealth();
    const done = tid("px-h-inc-wi_inc1")!;
    expect(done.getAttribute("data-s")).toBe("resolved");
    expect(done.textContent).toContain("14 min · resolved by the squad");
    const flow = [...tid("px-h-inc-flow-wi_inc1")!.querySelectorAll(".px-fl-n")];
    expect(flow.map((n) => n.textContent?.trim())).toEqual(["Alert", "SRE squad", "Pool exhausted", "Pool 10→20", "Recovered"]);
    expect(flow.map((n) => n.getAttribute("data-t"))).toEqual(["signal", "agent", "decision", "done", "done"]);
    const waiting = [...tid("px-h-inc-flow-wi_inc2")!.querySelectorAll(".px-fl-n")];
    expect(waiting.map((n) => n.getAttribute("data-t"))).toEqual(["signal", "agent", "decision", "waiting", "todo"]);
    expect(tid("px-h-inc-wi_inc2")!.textContent).toContain("fix needs your approval");
    expect(tid("px-h-inc-graph-wi_inc1")!.getAttribute("href")).toContain("area=agent_ops");
    expect(tid("px-h-inc-all")!.getAttribute("href")).toContain(`project_id=${P}`);
  });

  it("lists each alert rule by name, severity and where it stands; the firing count is real", async () => {
    engine();
    await openHealth();
    expect(text("px-h-alerts")).toContain("0 firing");
    expect(text("px-h-al-al_5xx")).toContain("5xx on /api/tickets");
    expect(text("px-h-al-al_5xx")).toMatch(/page · resolved/);
    expect(text("px-h-al-al_p95")).toContain("ticket · quiet 3d");
    expect(tid("px-h-al-al_p95")!.getAttribute("data-s")).toBe("ok");
    expect(text("px-h-alerts")).not.toMatch(/rule group/);
  });

  it("a firing alert turns the tag red and says since when", async () => {
    engine({ ...FX_HEALTH, alerts: [{ id: "al_x", label: "Error rate", severity: "page", state: "firing", since: "2026-11-06T09:00:00Z", href: "" }] });
    await openHealth();
    expect(text("px-h-alerts")).toContain("1 firing");
    expect(text("px-h-al-al_x")).toMatch(/page · firing since/);
  });

  it("Beacon's pack says what it watches and links its file; Go deeper has Grafana, logs and Observability", async () => {
    engine();
    await openHealth();
    expect(text("px-h-monitoring")).toContain("6 rules · in repo · on deploy");
    expect([...tid("px-h-pack-kinds")!.querySelectorAll(".px-tag")].map((t) => t.textContent?.trim()))
      .toEqual(["Uptime", "5xx", "Latency", "Disk", "DB conns", "TLS"]);
    expect(tid("px-h-pack-file")!.getAttribute("href")).toContain("github.com");
    expect(tid("px-h-grafana-why")).not.toBeNull();
    expect(text("px-h-logs")).toBe("Logs · staging");
    expect(tid("px-h-observability")!.getAttribute("title")).toBe("Org-wide view of every app and BOSS itself");
  });

  it("keeps Observability's alert rules with every action, each rule one line until ▸", async () => {
    engine();
    await openHealth();
    const rules = tid("px-h-rules-tgt_staging")!;
    expect(rules.querySelector("[data-testid='obs-rule-query']")).toBeNull();
    const ids = new Set([...rules.querySelectorAll("[data-testid]")].map((e) => e.getAttribute("data-testid")));
    for (const action of ["obs-rule-add", "obs-rule-edit", "obs-rule-silence", "obs-rule-respond", "obs-rule-state", "obs-rule-more",
                          "obs-app-sources", "obs-app-history"]) {
      expect(ids, action).toContain(action);
    }
    await press(rules.querySelector("[data-testid='obs-rule-more']"));
    expect(rules.querySelector("[data-testid='obs-rule-query']")).not.toBeNull();
    expect(rules.querySelector("[data-testid='obs-rule-facts']")).not.toBeNull();
    expect($$("[data-testid='px-h-rules-tgt_staging'] [data-testid^='obs-rule-'][data-state]").length).toBe(5);
  });

  it("with no rules yet it says Beacon's pack installs on deploy", async () => {
    engine({ ...FX_HEALTH, monitoring_pack: { rules: 0, managed_in: "none", href: "" }, incidents: [], alerts: [] });
    await openHealth();
    expect(text("px-h-monitoring")).toContain("No rules yet · Beacon's pack installs on deploy");
    expect(text("px-h-incidents")).toContain("No incidents");
    expect([...tid("px-h-inc-empty-flow")!.querySelectorAll(".px-fl-n")].map((n) => n.textContent?.trim())).toEqual(["Alert", "SRE squad", "Root cause", "Fix", "Recovered"]);
  });
});

describe("Health's words", () => {
  it("reads times and spans the way the wireframe writes them", () => {
    const now = new Date(2026, 10, 6, 15, 0);
    expect(dayTime(new Date(2026, 10, 6, 9, 12).toISOString(), now)).toBe("Today 09:12");
    expect(dayTime(new Date(2026, 10, 4, 9, 12).toISOString(), now)).toBe("Wed 09:12");
    expect(spanWords(840)).toBe("14 min");
    expect(spanWords(null)).toBe("");
  });

  it("parses the engine's rows: rule states ok/resolved/firing, typed incident nodes, the pack's kinds and file", () => {
    const h = parseHealth(FX_HEALTH);
    expect(h.alerts.map((a) => a.state)).toEqual(["resolved", "ok", "ok"]);
    expect(h.incidents[0].mini[0].type).toBe("signal");
    expect(h.monitoring_pack?.kinds).toContain("TLS");
    expect(h.environments[0].slo.budget_used_pct).toBe(22);
    expect(parseHealth({}).monitoring_pack).toBeNull();
  });
});
