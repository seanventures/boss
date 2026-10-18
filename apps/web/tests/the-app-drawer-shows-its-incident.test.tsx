// @vitest-environment happy-dom
/**
 * The app drawer's Incident card (D2): an app's own alert, answered. The status chip is derived
 * from the response view (never sent); each section draws only when it has data; Respond starts
 * the catalogue's Incident Response with the view's own `respond_input`; the card says which of
 * the app's rules respond automatically (per rule, never per app) and links to the Alerts tab,
 * with no app-wide switch; the report is viewable and downloadable.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { IncidentCardView } from "../app/observability/incident-card";
import { incidentStatus, parseIncidentResponse, responseStrip } from "../lib/incident-response";
import { INCIDENT_NOW, incidentResponseWire } from "./_helpers/incident-fixtures";
import { $, $$, click, mount, settle, unmount } from "./_helpers/live-render";
import { openObs, text } from "./_helpers/obs-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const NOW = Date.parse(INCIDENT_NOW);
const view = (over: Record<string, unknown> = {}) => parseIncidentResponse(incidentResponseWire(over));
const incident = (over: Record<string, unknown>) => ({ ...incidentResponseWire().incident, ...over });
const steps = (...s: [string, string][]) => s.map(([step_id, status]) => ({ step_id, status }));
const REPORTED_ONLY = {
  incident: incident({ title: "helpdesk: reported — Tickets fail to save" }), alert: null, deploy: null, verification: null,
  reported_only: true, response: null, report_ready: false,
  evidence: incidentResponseWire().evidence.filter((e) => e.kind === "customer_reports"),
};

describe("the status chip", () => {
  it("is derived from the data", () => {
    expect(incidentStatus(view({ incident: incident({ status: "resolved" }) }))).toBe("resolved");
    expect(incidentStatus(view({ response: null }))).toBe("alerting");
    expect(incidentStatus(view({ response: null, reported_only: true }))).toBe("reported");
    expect(incidentStatus(view())).toBe("responding");
    expect(incidentStatus(view({ response: { id: "r", status: "waiting", steps: steps(["deploy", "succeeded"], ["verify", "running"]) } })))
      .toBe("verifying");
    // A response that ended while the incident is still open is not "responding".
    expect(incidentStatus(view({ response: { id: "r", status: "failed", steps: steps(["build", "failed"]) } }))).toBe("alerting");
    // A reported incident someone is answering is responding.
    expect(incidentStatus(view({ reported_only: true }))).toBe("responding");
  });

  it("the strip keeps the catalogue's order and marks steps not started", () => {
    const strip = responseStrip(view().response!.steps);
    expect(strip.map((s) => s.id)).toEqual(["investigate", "build", "ci", "report", "deploy", "verify", "incident_report"]);
    expect(strip.find((s) => s.id === "verify")?.status).toBeNull();
  });
});

describe("the card", () => {
  it("draws every section from a full response view", async () => {
    await mount(<IncidentCardView projectId="p1" r={view()} now={NOW} />);
    const chip = $("[data-testid='obs-incident-status']");
    expect(chip?.textContent).toBe("Responding");
    expect(chip?.getAttribute("title")).toContain("Incident Response is running");
    expect(text($("[data-testid='obs-incident-title']"))).toBe("helpdesk: 12.5% of requests failing");
    expect(text($("[data-testid='obs-incident-opened']"))).toBe("opened 12m ago");

    const monitor = text($("[data-testid='obs-incident-monitor']"));
    expect(monitor).toContain("Errors12.5% of 220");
    expect(monitor).toContain("Alert above5% / 5 min");
    expect(text($("[data-testid='obs-incident-serving']"))).toBe("a1b2c3d");
    expect(text($("[data-testid='obs-incident-previous']"))).toBe("9f8e7d6");
    expect($$("[data-testid='obs-incident-routes'] .obs-chip").map((c) => c.textContent))
      .toEqual(["POST /tickets", "GET /tickets/{id}", "GET /search"]);

    const reports = $("[data-testid='obs-incident-reports']");
    expect(text(reports)).toContain("3 reports from support-desk");
    expect([...(reports?.querySelectorAll(".obs-chip") ?? [])].map((c) => c.textContent)).toEqual(["T-1042", "T-1043", "T-1044"]);

    expect(text($("[data-testid='obs-incident-rca']"))).toContain("where every failing request raised KeyError");
    expect(text($("[data-testid='obs-incident-basis']"))).toBe("release changed failing frame · confidence 90%");

    expect($("[data-testid='obs-incident-run']")?.getAttribute("href")).toBe("?area=delivery_workflows&wfr=wfr_ir_1");
    const strip = $$("[data-testid='obs-incident-steps'] li");
    expect(strip).toHaveLength(7);
    expect(strip.find((li) => li.dataset.step === "deploy")?.dataset.status).toBe("running");
    expect(strip.find((li) => li.dataset.step === "verify")?.dataset.status).toBe("not_started");
    expect($("[data-testid='obs-incident-pr']")?.getAttribute("href")).toBe("https://github.com/acme/helpdesk/pull/42");
    expect($("[data-testid='obs-incident-respond']")).toBeNull();

    const ver = $("[data-testid='obs-incident-verification'] .obs-inc-text");
    expect(ver?.textContent).toContain("Recovered: 0.4% of 260 requests");
    expect(ver?.getAttribute("title")).toBe("Judged by BOSS's monitor, not by the fixing agent");

    expect($("[data-testid='obs-incident-report-md']")?.getAttribute("href"))
      .toBe("/v1/reliability/incidents/inc_1/report?project_id=p1&format=md");
    expect($("[data-testid='obs-incident-report-json']")?.getAttribute("href"))
      .toBe("/v1/reliability/incidents/inc_1/report?project_id=p1&format=json");
  });

  it("a reported-only incident says Reported, draws no monitor section and offers Respond", async () => {
    await mount(<IncidentCardView projectId="p1" r={view(REPORTED_ONLY)} now={NOW} />);
    expect($("[data-testid='obs-incident-status']")?.textContent).toBe("Reported");
    expect($("[data-testid='obs-incident-monitor']")).toBeNull();
    expect($("[data-testid='obs-incident-rca']")).toBeNull();
    expect($("[data-testid='obs-incident-verification']")).toBeNull();
    expect($("[data-testid='obs-incident-report']")).toBeNull();
    expect(text($("[data-testid='obs-incident-reports']"))).toContain("3 reports from support-desk");
    expect(text($("[data-testid='obs-incident-respond']"))).toBe("Respond");
    // no app-wide switch: which rules respond, and where to change that
    expect($("[role='switch']")).toBeNull();
    expect(text($("[data-testid='obs-incident-auto'] > span"))).toBe("No rule responds automatically");
    expect($("[data-testid='obs-incident-alerts']")?.getAttribute("href")).toContain("atab=alerts");
  });

  it("says what each reporting app was told, and a failed call offers Send again (D2: two 404s went unsaid)", async () => {
    const told = [
      { target_id: "t_desk", app_name: "support-desk", signal_id: "s1", status: "resolved", ok: true, http: 204, word: "OK", at: null },
      { target_id: "t_desk", app_name: "support-desk", signal_id: "s2", status: "resolved", ok: false, http: 404,
        word: "The app answered 404", at: null },
    ];
    await mount(<IncidentCardView projectId="p1" now={NOW} r={view({ told })} onTell={() => undefined} />);
    const lines = $$("[data-testid='obs-incident-told']").map((el) => text(el.querySelector("span")));
    expect(lines).toEqual(["Told support-desk: root cause and fix", "Could not tell support-desk (404)"]);
    expect($$("[data-testid='obs-incident-tell']").map((b) => text(b))).toEqual(["Send again"]);
  });

  it("a resolved incident says Resolved and offers no Respond", async () => {
    await mount(<IncidentCardView projectId="p1" now={NOW} r={view({
      incident: incident({ status: "resolved" }),
      response: { id: "wfr_ir_1", status: "completed", steps: steps(["investigate", "succeeded"], ["build", "succeeded"], ["ci", "succeeded"],
        ["report", "succeeded"], ["deploy", "succeeded"], ["verify", "succeeded"], ["incident_report", "succeeded"]) },
    })} />);
    const chip = $("[data-testid='obs-incident-status']");
    expect(chip?.textContent).toBe("Resolved");
    expect(chip?.getAttribute("data-tone")).toBe("green");
    expect($$("[data-testid='obs-incident-steps'] li").every((li) => li.dataset.status === "succeeded")).toBe(true);
    expect($("[data-testid='obs-incident-respond']")).toBeNull();
  });
});

describe("in the drawer", () => {
  const NO_RESPONSE = { response: null, verification: null, report_ready: false };

  it("Open on the alert's card lands on the drawer with the incident card", async () => {
    await openObs("?view=apps&app=t_down");
    expect($("[data-testid='obs-app-drawer'] [data-testid='obs-incident-card']")).not.toBeNull();
    expect($("[data-testid='obs-incident-status']")?.textContent).toBe("Responding");
  });

  it("an app whose metrics carry no incident draws no card", async () => {
    await openObs("?view=apps&app=t_live");
    expect($("[data-testid='obs-incident-card']")).toBeNull();
  });

  it("Respond starts Incident Response with the view's respond_input", async () => {
    const rec = await openObs("?view=apps&app=t_down", {
      incident: incidentResponseWire(NO_RESPONSE),
      writes: { "/api/delivery-workflows": { body: { run_id: "wfr_new" } } },
    });
    await click($("[data-testid='obs-incident-respond']"));
    await settle();
    const start = rec.writes.find((w) => w.url === "/api/delivery-workflows");
    expect(start?.body).toEqual({ action: "start", project_id: "p1", workflow_key: "incident_response", actor: "ceo",
                                  input: incidentResponseWire().respond_input });
  });

  it("says which rules respond automatically and links to the Alerts tab — no app-wide switch", async () => {
    const rec = await openObs("?view=apps&app=t_down", {
      incident: incidentResponseWire({ ...NO_RESPONSE, auto_respond_rules: [
        { rule_id: "arule_er", kind: "error_rate", alert: "AppErrorRate", label: "Error rate", on_behalf_of: "Param" },
        { rule_id: "arule_lat", kind: "latency_p95", alert: "AppLatencyP95", label: "p95 latency", on_behalf_of: "Param" }] }),
    });
    const card = $("[data-testid='obs-incident-card']");
    expect(card?.querySelector("[role='switch']")).toBeNull();
    expect(text($("[data-testid='obs-incident-auto']"))).toContain("Error rate and p95 latency respond automatically");
    expect($("[data-testid='obs-incident-auto']")?.getAttribute("data-rules")).toBe("error_rate latency_p95");
    const link = $("[data-testid='obs-incident-alerts']") as HTMLAnchorElement | null;
    expect(link?.getAttribute("href")).toContain("atab=alerts");
    expect(link?.getAttribute("href")).toContain("app=t_down");
    await click(link);
    await settle();
    expect($("[data-testid='obs-app-tab-alerts']")?.getAttribute("aria-selected")).toBe("true");
    expect(rec.writes.filter((w) => w.url.includes("event-subscriptions"))).toEqual([]);
  });

  it("says plainly when no rule responds by itself", async () => {
    await openObs("?view=apps&app=t_down", { incident: incidentResponseWire(NO_RESPONSE) });
    expect(text($("[data-testid='obs-incident-auto']"))).toContain("No rule responds automatically");
    expect($("[data-testid='obs-incident-respond']")).not.toBeNull();
  });

  it("Send again posts the incident to its reporters once more", async () => {
    const rec = await openObs("?view=apps&app=t_down", {
      incident: incidentResponseWire({ told: [{ target_id: "t_desk", app_name: "support-desk", signal_id: "s1", status: "resolved",
                                                ok: false, http: 404, word: "The app answered 404", at: null }] }),
      writes: { "/tell-reporters": { body: { told: [{ ok: true, word: "OK" }] } } },
    });
    await click($("[data-testid='obs-incident-tell']"));
    await settle();
    const w = rec.writes.find((x) => x.url.endsWith("/v1/reliability/incidents/inc_1/tell-reporters"));
    expect(w?.method).toBe("POST");
    expect(w?.body).toEqual({ project_id: "p1" });
  });

  it("View reads the report into the drawer", async () => {
    await openObs("?view=apps&app=t_down");
    await click($("[data-testid='obs-incident-report-view']"));
    await settle();
    expect(text($("[data-testid='obs-incident-report-body']"))).toContain("The release changed tickets.create.");
    expect(text($("[data-testid='obs-incident-report-view']"))).toBe("Hide");
  });
});
