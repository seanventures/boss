// @vitest-environment happy-dom
/**
 * The DQ case study, console side: Knowledge Graph › Documents › DQ platform (app/knowledge/
 * kg-dq-platform.tsx). "Load the NorthPeak Bank data platform" is the product action (POST
 * /v1/knowledge/samples/dq, confirmed); once loaded, Eagle's exception groups are listed by COB
 * and each one's "Investigate" starts dq_incident_rca with that group; the morning intake is a
 * confirmed schedule; a rulebook not loaded yet is said.
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

const PACKS = { current: null, packs: [], available: [{ pack_id: "banking-dq", version: "2" }] };
const group = (id: string, cob: string, extra: Record<string, unknown> = {}) => ({
  group_id: id, eagle_rule_id: "EGL-0588", rule_id: "NPB-DQ-LN-003", rule_version: 2, cob,
  grade: "Error", column: "m_loan.principal_outstanding_derived", failed: 412, population: 61220,
  incident_id: null, incident_status: null, run_id: null, run_status: null, ...extra,
});
const BASE = { has_pack: true, rules_in_rulebook: 96, workflow_key: "dq_incident_rca",
               cobs: ["2026-10-05", "2026-10-06"], running_example: "EXG-20261005-0003" };
const NOT_LOADED = { ...BASE, loaded: false, has_pack: false, rules_in_graph: 0, groups: [] };
const LOADED = { ...BASE, loaded: true, rules_in_graph: 0, groups: [
  group("EXG-20261005-0003", "2026-10-05", { incident_id: "INC-505", incident_status: "open" }),
  group("EXG-20261005-0005", "2026-10-05", { eagle_rule_id: "EGL-0646", rule_id: "NPB-DQ-AR-001" }),
  group("EXG-20261006-0007", "2026-10-06"),
] };

function Docs() {
  return <DocumentsTab projectId="northpeak-bank" actor="ceo" />;
}

const routes = (sample: () => unknown, extra: Record<string, unknown> = {}) => mockKgFetch({
  "/v1/knowledge/packs$": { body: PACKS },
  "/v1/knowledge/documents$": { body: { documents: [] } },
  "/v1/knowledge/samples/dq$": (url: string) => (url ? { body: sample() } : { body: {} }),
  ...extra,
} as never);

describe("Documents tab: DQ platform", () => {
  it("loads the platform through the confirmed product action and lists Eagle's groups by COB", async () => {
    let loaded = false;
    const fn = routes(() => (loaded ? LOADED : NOT_LOADED));
    await mount(<Docs />);
    expect($('[data-testid="kg-dq-platform-state"]')?.textContent).toBe("Not loaded");
    expect($('[data-testid="kg-dq-groups"]')).toBeNull();

    loaded = true;
    await press($('[data-testid="kg-dq-load"]'));
    const sent = fn.mock.calls.find(([u, i]) => String(u).includes("/v1/knowledge/samples/dq") && i?.method === "POST");
    expect(JSON.parse(String(sent?.[1]?.body))).toEqual({
      project_id: "northpeak-bank", actor: "ceo", step_up: "confirmed" });
    expect($('[data-testid="kg-dq-platform-state"]')?.textContent).toMatch(/0 of 96 DQ rules in the graph/);
    expect($('[data-testid="kg-dq-rules-missing"]')?.textContent).toMatch(/Load the rulebook/);
    // The latest COB first; choosing another COB shows its groups.
    expect($$('[data-testid="kg-dq-group"]').length).toBe(1);
    const select = $('[data-testid="kg-dq-cob"]') as HTMLSelectElement;
    await act(async () => { select.value = "2026-10-05"; select.dispatchEvent(new Event("change", { bubbles: true })); });
    await settle();
    expect($$('[data-testid="kg-dq-group-status"]').map((e) => e.textContent)).toEqual([
      "INC-505 · open", "Not investigated"]);
  });

  it("Investigate starts dq_incident_rca with that exception group and links the run", async () => {
    const fn = routes(() => LOADED, { "/api/delivery-workflows$": { body: { run: { id: "wfr_dq_1" } } } });
    await mount(<Docs />);
    await press($$('[data-testid="kg-dq-investigate"]')[0]);
    const start = fn.mock.calls.find(([u, i]) => String(u).includes("/api/delivery-workflows") && i?.method === "POST");
    expect(JSON.parse(String(start?.[1]?.body))).toMatchObject({
      action: "start", project_id: "northpeak-bank", workflow_key: "dq_incident_rca",
      input: { exception_group_id: "EXG-20261006-0007" } });
    const msg = $('[data-testid="kg-dq-investigate-msg"]');
    expect(msg?.textContent).toMatch(/Investigating EGL-0588 v2 on COB 2026-10-06/);
    expect(msg?.querySelector("a")?.getAttribute("href")).toContain("wfr=wfr_dq_1");
  });

  it("the morning intake is a confirmed schedule", async () => {
    const fn = routes(() => LOADED, { "/v1/knowledge/samples/dq/schedule$": { body: { created: true, enabled: true } } });
    await mount(<Docs />);
    await press($('[data-testid="kg-dq-schedule"]'));
    const sent = fn.mock.calls.find(([u, i]) => String(u).includes("/samples/dq/schedule") && i?.method === "POST");
    expect(JSON.parse(String(sent?.[1]?.body))).toMatchObject({ step_up: "confirmed" });
    expect($('[data-testid="kg-dq-msg"]')?.textContent).toMatch(/Every morning/);
  });
});
