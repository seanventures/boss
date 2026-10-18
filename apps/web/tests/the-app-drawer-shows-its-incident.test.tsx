// @vitest-environment happy-dom
/**
 * The app drawer's Incident card (D2): an app's own alert, answered. The status chip is derived
 * from the response view (never sent); each section draws only when it has data; Respond starts
 * the catalogue's Incident Response with the view's own `respond_input`; Respond automatically
 * makes (or turns off) the `incident.opened` → `incident_response` subscription, asking for
 * consent when the API answers 409 consent_required; the report is viewable and downloadable.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { IncidentCardView } from "../app/observability/incident-card";
import { CONSENT_EVENT, type ConsentRequest } from "../lib/identity/consent";
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
    const auto = $("[data-testid='obs-incident-auto']");
    expect(auto?.getAttribute("role")).toBe("switch");
    expect(auto?.getAttribute("aria-checked")).toBe("false");
    expect(auto?.getAttribute("title")).toContain("standing grant");
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

  it("Respond automatically subscribes, asking for consent when the API needs a standing grant", async () => {
    let posts = 0;
    const consent = { error: "consent_required", scope: "workflow_run", task: "Incident Response", task_run_id: null,
                      agents: [{ id: "agt_sre", name: "SRE", role_key: "sre" }], permissions: ["workflow_run:start"], projects: ["p1"],
                      durations: [{ kind: "standing", label: "Standing", days: 30 }], max_days: 30 };
    const asked: ConsentRequest[] = [];
    const onConsent = (e: Event) => { const d = (e as CustomEvent<ConsentRequest>).detail; asked.push(d); d.onDecided?.("dlg_1"); };
    window.addEventListener(CONSENT_EVENT, onConsent);
    try {
      const rec = await openObs("?view=apps&app=t_down", { incident: incidentResponseWire(NO_RESPONSE) });
      const fetchFn = globalThis.fetch as unknown as (i: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
      vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input) === "/v1/event-subscriptions" && init?.method === "POST" && posts++ === 0) {
          rec.writes.push({ url: String(input), method: "POST", body: JSON.parse(String(init.body)) });
          return new Response(JSON.stringify(consent), { status: 409, headers: { "content-type": "application/json" } });
        }
        return fetchFn(input, init);
      }));
      await click($("[data-testid='obs-incident-auto']"));
      await settle();
      expect(asked.map((a) => a.task)).toEqual(["Incident Response"]);
      const subs = rec.writes.filter((w) => w.url === "/v1/event-subscriptions");
      expect(subs).toHaveLength(2);
      expect(subs[1].body).toEqual({ project_id: "p1", event_type: "incident.opened", sink_kind: "workflow",
                                     sink_ref: "incident_response", actor: "ceo" });
    } finally {
      window.removeEventListener(CONSENT_EVENT, onConsent);
    }
  });

  it("an existing subscription is turned off with a PATCH", async () => {
    const rec = await openObs("?view=apps&app=t_down", {
      incident: incidentResponseWire({ ...NO_RESPONSE, auto_respond: { subscription_id: "esub_1", enabled: true, on_behalf_of: "usr_1" } }),
    });
    expect($("[data-testid='obs-incident-auto']")?.getAttribute("aria-checked")).toBe("true");
    await click($("[data-testid='obs-incident-auto']"));
    await settle();
    const w = rec.writes.find((x) => x.url === "/v1/event-subscriptions/esub_1");
    expect(w?.method).toBe("PATCH");
    expect(w?.body).toEqual({ project_id: "p1", enabled: false, actor: "ceo" });
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
