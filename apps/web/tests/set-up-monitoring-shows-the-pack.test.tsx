// @vitest-environment happy-dom
/**
 * SET UP MONITORING (BOSSAPP-601): the step names Beacon, says what the pack watches, and lists
 * every rule with its SEV, threshold, last-hour preview, runbook and PromQL, with links to the
 * app's rules and dashboard. The release that carries it shows the same pack, approved with it;
 * a release with no pack says whether it may ship (monitoring_gate).
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MonitoringPackCard, monitoringPackOf, packForStep } from "../app/workflows/monitoring-pack";
import { $, $$, mount, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); });

const PACK = {
  by: { role_key: "sre", role: "Site Reliability Engineer" },
  app: "Order API", environment: "staging", summary: "Watches order creation and the payments upstream.",
  adding: false, branch: "boss/code/crun_1", manifest_path: "boss.deploy.yaml", gate: "block",
  rules: [
    { alert: "AppHealthCheckFailing", kind: "health_check", label: "Health check", severity: "sev1",
      expr: "sum(count_over_time(probe_success{app=\"Order API\"}[10m])) >= 3", threshold: "3 failed probes in 10 min",
      enabled: true, runbook: "runbooks/order-api-down.md", runbook_title: "The Order API is down", new: true,
      preview: { fired: 0, word: "Would have fired 0 times in the last hour" } },
    { alert: "OrderPaymentFailures", kind: "custom", label: "OrderPaymentFailures", severity: "sev2",
      expr: "sum(rate(payment_failures_total{app=\"Order API\",environment=\"staging\"}[5m])) > 0.2",
      threshold: "> 0.2 failed charges/s for 5 min", enabled: true, runbook: "runbooks/payments-failing.md",
      runbook_title: "Payments are failing", new: true, preview: { fired: 2, word: "Would have fired 2 times in the last hour" } },
  ],
  runbooks: [{ path: "runbooks/order-api-down.md", title: "The Order API is down" },
             { path: "runbooks/payments-failing.md", title: "Payments are failing" }],
  panels: ["Orders created / s"], tuned: [{ alert: "AppErrorRate", before: "> 0.5%", after: "> 3%" }],
  rules_href: "?area=observability&view=apps&app=deptgt_1&atab=alerts",
  dashboard_url: "https://grafana.example.test/d/boss-order-api", dashboard_why: null,
};

describe("Set up monitoring's pack", () => {
  it("names Beacon and shows every rule with SEV, threshold, preview, runbook and PromQL", async () => {
    await mount(<MonitoringPackCard pack={monitoringPackOf(PACK)!} />);
    const who = $("[data-testid='mp-who']")!;
    expect(who.textContent).toContain("Beacon");
    expect(who.textContent).toContain("Site Reliability Engineer");
    expect($("[data-testid='mp-pack']")?.textContent).toContain("Set up monitoring");
    expect($("[data-testid='mp-summary']")?.textContent).toContain("payments upstream");
    const rules = $$("[data-testid='mp-rule']");
    expect(rules.map((r) => r.getAttribute("data-alert"))).toEqual(["AppHealthCheckFailing", "OrderPaymentFailures"]);
    const pay = rules[1];
    expect(pay.querySelector("[data-testid='obs-sev']")?.getAttribute("data-sev")).toBe("sev2");
    expect(pay.textContent).toContain("> 0.2 failed charges/s for 5 min");
    expect(pay.querySelector("[data-testid='mp-preview']")?.textContent).toBe("fired 2× last hour");
    expect(rules[0].querySelector("[data-testid='mp-preview']")?.textContent).toBe("quiet last hour");
    expect(pay.querySelector("[data-testid='mp-rule-runbook']")?.textContent).toBe("Runbook: Payments are failing");
    expect(pay.querySelector("[data-testid='mp-rule-expr']")?.textContent).toContain("payment_failures_total");
    expect($("[data-testid='mp-tuned']")?.textContent).toContain("Tuned 1 rule");
    expect($("[data-testid='mp-foot']")?.textContent).toContain("2 rules, 2 runbooks, 1 dashboard panel");
    expect($("[data-testid='mp-rules-link']")?.getAttribute("href")).toContain("atab=alerts");
    expect($("[data-testid='mp-dashboard-link']")?.getAttribute("href")).toContain("boss-order-api");
  });

  it("on the release card it says the pack is approved with the release", async () => {
    await mount(<MonitoringPackCard pack={monitoringPackOf(PACK)!} onCard />);
    expect($("[data-testid='mp-approve-with']")?.textContent).toContain("approves its monitoring pack");
  });

  it("a release with no pack says whether it may ship", async () => {
    await mount(<MonitoringPackCard pack={monitoringPackOf({ missing: true, gate: "block", why: "no model is connected" })!} />);
    expect($("[data-testid='mp-missing']")?.textContent).toContain("cannot ship without one");
    unmount();
    await mount(<MonitoringPackCard pack={monitoringPackOf({ missing: true, gate: "warn", why: "no model" })!} />);
    expect($("[data-testid='mp-missing']")?.textContent).toContain("goes on without one");
  });

  it("the release step finds the pack of the Set up monitoring step in front of it", () => {
    const steps = [
      { step_id: "monitoring", capability: "monitoring_pack", status: "succeeded", output: { pack: PACK } },
      { step_id: "deploy_again", capability: "deploy", status: "waiting", output: {} },
      { step_id: "deploy", capability: "deploy", status: "succeeded", output: {} },
    ];
    const next = (id: string) => (id === "monitoring" ? ["deploy_again"] : []);
    expect(packForStep(steps, steps[0], next)?.rules).toHaveLength(2);
    expect(packForStep(steps, steps[1], next)?.app).toBe("Order API");
    expect(packForStep(steps, steps[2], next)).toBeNull();          // the first deploy carries none
    const failed = { step_id: "monitoring", capability: "monitoring_pack", status: "failed",
                     output: { gate: "block", note: "No monitoring pack: no model" } };
    expect(packForStep([failed, steps[1]], steps[1], next)?.missing).toBe(true);
  });
});
