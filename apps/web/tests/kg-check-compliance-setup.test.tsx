// @vitest-environment happy-dom
/**
 * KG P7b, console side (criteria 5-6): "Check compliance" (app/knowledge/kg-documents.tsx,
 * `CheckCompliance`) failed twice on the dry run before the pipeline ever started —
 * "no delivery workflow 'banking_regulatory_compliance'" (the project predates the catalogue
 * entry) and "needs 1 input it was not given: subject_id" (the clause has no linked rule yet).
 * Both must offer a one-press recovery that reuses an existing endpoint, not a raw error.
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DocumentsTab } from "../app/knowledge/kg-documents";
import { calls, mockKgFetch } from "./_helpers/kg-fetch";
import { $, mount, settle, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn() }), usePathname: () => "/", useSearchParams: () => new URLSearchParams() }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const press = async (el: Element | null) => { await act(async () => (el as HTMLElement).click()); await settle(); };

const WITH_PACK = { current: { pack_id: "banking-regulatory", version: "1" }, available: [{ pack_id: "banking-regulatory", version: "1" }] };
const ONE_DOC = {
  documents: [{
    id: "doc_1", title: "Reg Z", citation: "12 CFR 1026", archived: false,
    clauses: [{ id: "clause_1", section: "1026.36(c)(1)", text: "A loan originator shall not.", citation: "12 CFR 1026.36(c)(1)" }],
  }],
};

function Docs() {
  return <DocumentsTab projectId="northpeak-bank-b" actor="ceo" />;
}

describe("Check compliance: no delivery workflow installed yet", () => {
  it("offers to install the workflow from the catalogue, then retries the original start", async () => {
    let startAttempts = 0;
    const fn = mockKgFetch({
      "/v1/knowledge/documents$": { body: ONE_DOC },
      "/v1/knowledge/packs$": { body: WITH_PACK },
      "/v1/knowledge/documents/clause_1/linked-rules$": { body: { rules: [] } },
      "/api/delivery-workflows$": (() => {
        return () => {
          startAttempts += 1;
          if (startAttempts === 1) {
            return { status: 404, body: { error: "no delivery workflow 'banking_regulatory_compliance' in this project" } };
          }
          return { body: { id: "wfr_1" } };
        };
      })(),
    });
    await mount(<Docs />);
    await press($('[data-testid="kg-doc-check"]'));

    const installOffer = $('[data-testid="kg-doc-install-workflow"]');
    expect(installOffer, "a 404 for the missing workflow must offer to install it, not just show the raw error").toBeTruthy();

    await press(installOffer);

    const posts = fn.mock.calls.filter(([u, i]) => String(u).includes("/api/delivery-workflows") && i?.method === "POST");
    // The proxy's action is "install" (app/api/delivery-workflows/route.ts ACTIONS → POST
    // /v1/delivery-workflows/catalogue/install); "catalogue/install" is refused there as unknown.
    const installCall = posts.find(([, i]) => JSON.parse(String(i?.body ?? "{}")).action === "install");
    expect(installCall, "pressing install must call the proxy's install action").toBeTruthy();
    expect(JSON.parse(String(installCall?.[1]?.body)).project_id).toBe("northpeak-bank-b");

    expect($('[data-testid="kg-doc-check-msg"]')?.textContent).toMatch(/started/i);
  });
});

describe("Check compliance: the clause has no linked rule yet", () => {
  it("offers a rule picker on the missing subject_id error, instead of a raw error", async () => {
    mockKgFetch({
      "/v1/knowledge/documents$": { body: ONE_DOC },
      "/v1/knowledge/packs$": { body: WITH_PACK },
      "/v1/knowledge/documents/clause_1/linked-rules$": { body: { rules: [] } },
      "/api/delivery-workflows$": {
        status: 422,
        body: { error: "this workflow needs 1 input(s) it was not given: subject_id" },
      },
    });
    await mount(<Docs />);
    await press($('[data-testid="kg-doc-check"]'));

    expect(calls.some((c) => c.startsWith("GET") && c.includes("rules"))).toBe(true);
    const linkRule = $('[data-testid="kg-doc-link-subject"]');
    expect(linkRule, "a missing-subject_id validation error must offer to link a rule, not just show the raw error").toBeTruthy();
    expect($('[data-testid="kg-doc-check-msg"]')?.textContent ?? "").not.toMatch(/needs 1 input/);
  });

  it("selects a project rule from the picker and retries start with that rule as subject_id", async () => {
    let lastStartBody: Record<string, unknown> | null = null;
    // The POST that starts the workflow must 422 with the missing-subject_id error on the
    // FIRST attempt (no rule chosen yet) and succeed once the retry carries a chosen rule's
    // node id as subject_id — a plain mockKgFetch answer is fixed, so this test drives fetch
    // directly to vary that one route's response across the two calls it must prove happen.
    let startAttempts = 0;
    let linkRuleCalls = 0;
    let lastLinkRuleBody: Record<string, unknown> | null = null;
    const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, "http://console.test");
      if (url.pathname === "/v1/knowledge/documents/clause_1/link-rule" && init?.method === "POST") {
        linkRuleCalls += 1;
        lastLinkRuleBody = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
        return new Response(JSON.stringify({ edge_id: "e_1", kind: "implemented_by" }), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (url.pathname === "/api/delivery-workflows" && init?.method === "POST") {
        const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
        if (body.action === "start") {
          startAttempts += 1;
          lastStartBody = body;
          if (startAttempts === 1) {
            return new Response(JSON.stringify({ error: "this workflow needs 1 input(s) it was not given: subject_id" }), { status: 422, headers: { "content-type": "application/json" } });
          }
          return new Response(JSON.stringify({ id: "wfr_1" }), { status: 200, headers: { "content-type": "application/json" } });
        }
      }
      if (url.pathname === "/v1/knowledge/documents") return new Response(JSON.stringify(ONE_DOC), { status: 200, headers: { "content-type": "application/json" } });
      if (url.pathname === "/v1/knowledge/packs") return new Response(JSON.stringify(WITH_PACK), { status: 200, headers: { "content-type": "application/json" } });
      if (url.pathname === "/v1/knowledge/documents/clause_1/linked-rules") return new Response(JSON.stringify({ rules: [] }), { status: 200, headers: { "content-type": "application/json" } });
      if (url.pathname === "/v1/knowledge/view/rules") {
        return new Response(JSON.stringify({ items: [{ node: { id: "kgn_fixture_rule_01", key: "rule:fixture01", kind: "rule", layer: "rules", label: "A loan cannot be disbursed on a future date", trust_tier: "approved" } }] }), { status: 200, headers: { "content-type": "application/json" } });
      }
      return new Response("{}", { status: 404 });
    });
    vi.stubGlobal("fetch", fetchSpy);

    await mount(<Docs />);
    await press($('[data-testid="kg-doc-check"]'));

    const linkRule = $('[data-testid="kg-doc-link-subject"]');
    expect(linkRule, "the missing-subject_id error must offer a rule picker").toBeTruthy();

    const select = linkRule?.querySelector("select") as HTMLSelectElement;
    expect(select, "the picker must list the project's candidate rules").toBeTruthy();
    await act(async () => {
      select.value = "kgn_fixture_rule_01";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await settle();

    const linkAndStart = linkRule?.querySelector('button[class*="ag-btn-primary"]') as HTMLButtonElement;
    expect(linkAndStart.disabled, "Link rule & start must be enabled once a rule is chosen").toBe(false);
    await press(linkAndStart);

    expect(linkRuleCalls, "choosing a rule must persist the clause→rule link, not just pass it as this run's subject_id").toBe(1);
    expect(lastLinkRuleBody).toMatchObject({ rule_id: "kgn_fixture_rule_01" });
    expect(startAttempts, "choosing a rule must retry start, not just close the picker").toBe(2);
    expect(lastStartBody, "the retried start must carry the chosen rule as subject_id").toBeTruthy();
    expect((lastStartBody as Record<string, unknown> | null)?.input).toMatchObject({ subject_id: "kgn_fixture_rule_01" });
    expect($('[data-testid="kg-doc-check-msg"]')?.textContent ?? "").toMatch(/started/i);
  });

  it("lets the operator search for a rule not in the initial candidate list", async () => {
    const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, "http://console.test");
      if (url.pathname === "/api/delivery-workflows" && init?.method === "POST") {
        return new Response(JSON.stringify({ error: "this workflow needs 1 input(s) it was not given: subject_id" }), { status: 422, headers: { "content-type": "application/json" } });
      }
      if (url.pathname === "/v1/knowledge/documents") return new Response(JSON.stringify(ONE_DOC), { status: 200, headers: { "content-type": "application/json" } });
      if (url.pathname === "/v1/knowledge/packs") return new Response(JSON.stringify(WITH_PACK), { status: 200, headers: { "content-type": "application/json" } });
      if (url.pathname === "/v1/knowledge/documents/clause_1/linked-rules") return new Response(JSON.stringify({ rules: [] }), { status: 200, headers: { "content-type": "application/json" } });
      if (url.pathname === "/v1/knowledge/view/rules") return new Response(JSON.stringify({ items: [] }), { status: 200, headers: { "content-type": "application/json" } });
      if (url.pathname === "/v1/knowledge/view/search") {
        return new Response(JSON.stringify({
          q: url.searchParams.get("q") ?? "", meaning: false, meaning_available: false,
          hits: [{ node: { id: "kgn_searched_rule", key: "rule:searched", kind: "rule", layer: "rules", label: "Repayment-order rule", trust_tier: "approved" }, match: "text" }],
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      return new Response("{}", { status: 404 });
    });
    vi.stubGlobal("fetch", fetchSpy);

    await mount(<Docs />);
    await press($('[data-testid="kg-doc-check"]'));

    const linkRule = $('[data-testid="kg-doc-link-subject"]');
    expect(linkRule).toBeTruthy();
    // The initial candidate list is empty — a rule can only be found by searching for it.
    const selectBefore = linkRule?.querySelector("select") as HTMLSelectElement;
    expect(selectBefore.querySelectorAll("option").length).toBe(1); // only the "Choose a rule…" placeholder

    const input = $('[data-testid="kg-doc-link-search"]') as HTMLInputElement;
    const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    await act(async () => { setValue.call(input, "repayment"); input.dispatchEvent(new Event("input", { bubbles: true })); });
    await press($('[data-testid="kg-doc-link-search-btn"]'));

    const searchedGet = fetchSpy.mock.calls.find(([u]) => {
      const p = new URL(typeof u === "string" ? u : u instanceof URL ? u.href : (u as Request).url, "http://console.test");
      return p.pathname === "/v1/knowledge/view/search" && p.searchParams.get("q") === "repayment";
    });
    expect(searchedGet, "pressing Search must query the search endpoint with the typed text").toBeTruthy();
    const selectAfter = linkRule?.querySelector("select") as HTMLSelectElement;
    const option = [...selectAfter.querySelectorAll("option")].find((o) => o.textContent === "Repayment-order rule");
    expect(option, "a searched rule must be offered in the picker").toBeTruthy();
  });
});
