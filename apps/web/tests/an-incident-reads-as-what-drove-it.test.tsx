// @vitest-environment happy-dom
/**
 * AN INCIDENT READS AS WHAT DROVE IT (operator, Oct 18): a recovered latency incident's card was
 * labelled "Errors" because the reused incident row still carried the morning's error-rate alert.
 * The card names the driving alert and its value; error facts and failing routes only for an
 * error-rate driver. And a message nothing is configured to send is a "Set up a contact point"
 * card whose button lands on Observability › Notify, not a SEV1 self-check alert.
 * Engine side: engine/tests/test_an_incident_reads_as_what_drove_it.py.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AttentionCard } from "../app/agents/attention-card";
import { IncidentCardView } from "../app/observability/incident-card";
import { parseAttention } from "../lib/agents-live-board";
import { parseIncidentResponse } from "../lib/incident-response";
import { INCIDENT_NOW, incidentResponseWire } from "./_helpers/incident-fixtures";
import { $, mount, unmount } from "./_helpers/live-render";
import { text } from "./_helpers/obs-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const NOW = Date.parse(INCIDENT_NOW);
const LATENCY = { alert_id: "al_1", alertname: "AppLatencyP95", kind: "latency_p95", label: "p95 latency", sev: "sev3",
                  state: "resolved", value_word: "1.24 s" };

describe("the incident card", () => {
  it("a latency driver: its label and value, no Errors, no failing routes", async () => {
    await mount(<IncidentCardView projectId="p1" now={NOW} r={parseIncidentResponse(incidentResponseWire({ driver: LATENCY }))} />);
    const monitor = text($("[data-testid='obs-incident-monitor']"));
    expect(monitor).toContain("p95 latency1.24 s");
    expect(monitor).not.toContain("Errors");
    expect(monitor).not.toContain("Alert above");
    expect($("[data-testid='obs-incident-routes']")).toBeNull();
  });

  it("an error-rate driver (or an older API with none) keeps the error facts", async () => {
    await mount(<IncidentCardView projectId="p1" now={NOW} r={parseIncidentResponse(incidentResponseWire({
      driver: { ...LATENCY, alertname: "AppErrorRate", kind: "error_rate", label: "Error rate", sev: "sev1" } }))} />);
    expect(text($("[data-testid='obs-incident-monitor']"))).toContain("Errors12.5% of 220");
  });
});

describe("nothing configured to send", () => {
  it("is a setup card whose button opens Notify", async () => {
    const item = parseAttention({ id: "ready:notify_setup", kind: "readiness", area: "observability", title: "Set up a contact point",
      detail: "2 messages not sent: BOSS_EMAIL_REAL_SEND_ALLOWED is false.", what_to_do: "Add or fix a contact point in Notify, then send a test",
      fix: { area: "observability", view: "notify", label: "Notify" }, role_key: null, actions: [], answers: [], decide_inline: false });
    await mount(<AttentionCard item={item} projectId="p1" actor="ceo" />);
    const fix = $("[data-testid='ag-att-fix']")!;
    expect(fix.textContent).toBe("Notify ↗");
    expect(fix.getAttribute("href")).toContain("area=observability");
    expect(fix.getAttribute("href")).toContain("view=notify");
  });
});
