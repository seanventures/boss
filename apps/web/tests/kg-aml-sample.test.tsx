// @vitest-environment happy-dom
/**
 * Case study 4, console side: Knowledge Graph › Documents › AML sample (app/knowledge/
 * kg-aml-sample.tsx). "Load AML sample" is the product action (POST /v1/knowledge/samples/aml,
 * confirmed), never a script; once loaded, each alert's "Investigate" starts
 * aml_alert_investigation with that alert, and the row says what a person decided.
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DocumentsTab } from "../app/knowledge/kg-documents";
import { mockKgFetch } from "./_helpers/kg-fetch";
import { $, $$, mount, settle, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn() }), usePathname: () => "/", useSearchParams: () => new URLSearchParams() }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const press = async (el: Element | null) => { await act(async () => (el as HTMLElement).click()); await settle(); };

const PACKS = { current: null, available: [{ pack_id: "banking-aml", version: "1" }] };
const alert = (id: string, extra: Record<string, unknown> = {}) => ({
  alert_id: id, node_id: `kgn_${id.toLowerCase().replace("-", "")}`, account_id: "ACC-0012",
  customer_id: "CUST-0007", rule: "FANOUT-01", raised_at: "2026-10-23", case_id: null,
  case_status: null, determination: null, ...extra,
});
const row = (id: string, name: string, extra: Record<string, unknown> = {}) => ({
  alert_id: id, customer_name: name, account_label: "Business Checking",
  rule: "FANOUT-01", rule_text: "Rapid fan-out of outbound payments.", score: 0.81,
  flagged_count: 11, raised_at: "2026-10-23", status: "open", status_label: "Open",
  is_open: true, ...extra,
});
const NOT_LOADED = { loaded: false, pack_id: null, workflow_key: "aml_alert_investigation",
                     alerts: [{ ...alert("ALERT-1"), node_id: null }],
                     problem_screen: { title: "AML alerts to review", state: "empty",
                                       summary: "No AML alerts are loaded.", alerts: [],
                                       open_count: 0 } };
const LOADED = { loaded: true, pack_id: "banking-aml", workflow_key: "aml_alert_investigation",
                 alerts: [alert("ALERT-1", { case_status: "closed_false_positive" }), alert("ALERT-2")],
                 problem_screen: { title: "AML alerts to review", state: "ok",
                                   summary: "1 of 2 alerts still open.",
                                   alerts: [
                                     row("ALERT-1", "Harbor Point Cafe LLC",
                                         { status: "closed_false_positive",
                                           status_label: "Closed False Positive", is_open: false }),
                                     row("ALERT-2", "NorthPeak Catering Co"),
                                   ], open_count: 1 } };

function Docs() {
  return <DocumentsTab projectId="northpeak-lending" actor="ceo" />;
}

describe("Documents tab: AML sample", () => {
  it("loads the sample through the confirmed product action and then lists the alerts", async () => {
    let loaded = false;
    const fn = mockKgFetch({
      "/v1/knowledge/packs$": { body: PACKS },
      "/v1/knowledge/documents$": { body: { documents: [] } },
      "/v1/knowledge/samples/aml$": (url) => (url ? { body: loaded ? LOADED : NOT_LOADED } : { body: {} }),
    });
    await mount(<Docs />);
    expect($('[data-testid="kg-aml-sample-state"]')?.textContent).toBe("Not loaded");
    expect($('[data-testid="kg-aml-alerts"]')).toBeNull();

    loaded = true;
    await press($('[data-testid="kg-aml-load"]'));
    const post = fn.mock.calls.find(([u, i]) => String(u).includes("/v1/knowledge/samples/aml") && i?.method === "POST");
    expect(post).toBeTruthy();
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({
      project_id: "northpeak-lending", actor: "ceo", step_up: "confirmed" });
    expect($$('[data-testid="kg-aml-alert"]').length).toBe(2);
    expect($$('[data-testid="kg-aml-alert-status"]').map((e) => e.textContent)).toEqual([
      "Closed False Positive", "Open"]);
    const stateText = $('[data-testid="kg-aml-sample-state"]')?.textContent ?? "";
    expect(stateText).toContain("1 of 2 alerts still open.");
  });

  it("renders from the problem screen: no raw ids, and the header count matches open rows", async () => {
    mockKgFetch({
      "/v1/knowledge/packs$": { body: PACKS },
      "/v1/knowledge/documents$": { body: { documents: [] } },
      "/v1/knowledge/samples/aml$": { body: LOADED },
    });
    await mount(<Docs />);
    const rowText = $$('[data-testid="kg-aml-alert"]').map((e) => e.textContent ?? "").join(" ");
    expect(rowText).not.toMatch(/\bCUST-\d+\b/);
    expect(rowText).not.toMatch(/\bACC-\d+\b/);
    expect(rowText).toContain("Harbor Point Cafe LLC");
    expect(rowText).toContain("Business Checking");
    const openRows = LOADED.problem_screen.alerts.filter((a) => a.is_open).length;
    expect($('[data-testid="kg-aml-sample-state"]')?.textContent).toContain(`${openRows} of`);
  });

  it("Investigate starts aml_alert_investigation with that alert and links the run", async () => {
    const fn = mockKgFetch({
      "/v1/knowledge/packs$": { body: PACKS },
      "/v1/knowledge/documents$": { body: { documents: [] } },
      "/v1/knowledge/samples/aml$": { body: LOADED },
      "/api/delivery-workflows$": { body: { run: { id: "wfr_aml_2" } } },
    });
    await mount(<Docs />);
    await press($$('[data-testid="kg-aml-investigate"]')[1]);
    const start = fn.mock.calls.find(([u, i]) => String(u).includes("/api/delivery-workflows") && i?.method === "POST");
    expect(JSON.parse(String(start?.[1]?.body))).toMatchObject({
      action: "start", project_id: "northpeak-lending", workflow_key: "aml_alert_investigation",
      input: { alert_id: "ALERT-2" } });
    const msg = $('[data-testid="kg-aml-investigate-msg"]');
    expect(msg?.textContent).toMatch(/Investigating ALERT-2/);
    expect(msg?.querySelector("a")?.getAttribute("href")).toContain("wfr=wfr_aml_2");
  });
});
