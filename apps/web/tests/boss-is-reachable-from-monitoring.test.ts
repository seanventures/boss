import { describe, expect, it } from "vitest";

import { monitoringStarters } from "../app/monitoring-ui";
import { codeOnly } from "./_helpers/source";

/**
 * "I want you to expose the BOSS Agent also in Monitoring section, I want to propose few
 * changes I want to see if BOSS Agent can help."
 *
 * Monitoring was the one operating screen with no way to ask. It is also the screen where the
 * question is least likely to be "what happened" and most likely to be "what should we be
 * watching instead" — so the starters cover both, and they are built from the fleet ON SCREEN
 * rather than from a fixed list: naming the app that is actually down beats "ask about an app",
 * and the first question then carries facts BOSS would otherwise spend three lookups
 * rediscovering.
 */
const target = (over: Record<string, unknown> = {}) => ({
  target_id: "deptgt_1", app_name: "Change Calendar", environment: "staging",
  url: "http://2.29.2.202:3013", status: "up", status_since: null,
  uptime_24h: 1, uptime_7d: 1, avg_ms: 10, p95_ms: 20, last_checked: null,
  last_latency_ms: 10, last_error: null, sample_count: 10, series: [], incidents: [],
  deployed_commit: null, deployed_at: null, deployment_run_id: null, ...over,
});
const health = (targets: unknown[]) => ({
  project_id: "p", targets,
  summary: { up: 0, degraded: 0, down: 0, unknown: 0, overall: "ok", total: targets.length },
}) as never;

describe("the questions it offers", () => {
  it("names the app that is actually broken, with its error", () => {
    const [first] = monitoringStarters(health([
      target(), target({ app_name: "Lead-Triage Inbox", status: "down",
                         url: "http://2.29.2.202:3014", last_error: "connection refused" }),
    ]));
    expect(first).toContain("Lead-Triage Inbox");
    expect(first).toContain("connection refused");
    expect(first).toContain("http://2.29.2.202:3014");
  });

  it("lists them when several are unwell", () => {
    const [first] = monitoringStarters(health([
      target({ app_name: "A", status: "down" }),
      target({ app_name: "B", status: "degraded" }),
    ]));
    expect(first).toContain("A, B");
  });

  it("distrusts an all-green board rather than congratulating it", () => {
    // Three apps read "up · 100% uptime · 0ms" for two days with nothing ever probed. A green
    // light not wired to anything is the same reassurance a working system gives.
    const [first] = monitoringStarters(health([target(), target({ app_name: "Other" })]));
    expect(first).toContain("Check the probes are really running");
  });

  it("separates 'nothing deployed' from 'nothing watching'", () => {
    const qs = monitoringStarters(health([target({ status: "unknown", app_name: "Quiet" })]));
    expect(qs.some((q) => q.includes("Quiet") && q.includes("no health signal"))).toBe(true);
  });

  it("always offers the one an operator arrives with an idea for", () => {
    for (const h of [health([]), health([target()]), null]) {
      expect(monitoringStarters(h).some((q) => q.includes("change what we monitor"))).toBe(true);
    }
  });

  it("says something even with no fleet at all", () => {
    expect(monitoringStarters(null).length).toBeGreaterThan(0);
    expect(monitoringStarters(health([])).length).toBeGreaterThan(0);
  });
});

describe("how it opens", () => {
  const UI = codeOnly("app/monitoring-ui.tsx");

  it("through the console's own dock, not a fourth chat surface", () => {
    expect(UI).toContain('url.searchParams.set("agent", "general");');
    expect(UI).toContain('url.searchParams.set("dock_ask", question);');
  });

  it("it is present whether or not anything is set up", () => {
    // "Nothing is set up" is exactly when an operator wants to ask what setting it up involves.
    expect(UI.split("<AskBossAboutMonitoring").length - 1).toBe(2);
  });

  it("and it says up front what it will not do on its own", () => {
    expect(UI).toContain("nothing is\n        reconfigured until you click");
  });
});
