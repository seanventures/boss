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
const NOT_LOADED = { loaded: false, pack_id: null, workflow_key: "aml_alert_investigation",
                     alerts: [{ ...alert("ALERT-1"), node_id: null }] };
const LOADED = { loaded: true, pack_id: "banking-aml", workflow_key: "aml_alert_investigation",
                 alerts: [alert("ALERT-1", { case_status: "closed_false_positive" }), alert("ALERT-2")] };

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
      "Closed as a false positive", "Not investigated"]);
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
