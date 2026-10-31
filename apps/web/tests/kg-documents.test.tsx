// @vitest-environment happy-dom
/**
 * KG P7a, the console side: the Documents tab's "Add regulation" form and its ontology-pack row
 * (app/knowledge/kg-documents.tsx) — the other half of apps/api/tests/test_knowledge_documents_api.py
 * and engine/tests/test_add_regulation_document_tool.py's "asking BOSS" path, which only cover the
 * HTTP route and the agent tool, never the screen a person actually presses.
 *
 * Breaks covered: the form submitting before a pack is set (it must stay disabled and say why);
 * a submit sending something other than the operator's own typed section/text; the list not
 * reloading after a successful add; "Change" on the pack row sending the chosen pack_id.
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
function type(el: HTMLInputElement | HTMLTextAreaElement, value: string) {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

const NO_PACK = { current: null, available: [{ pack_id: "banking-regulatory", version: "1" }, { pack_id: "banking-dq", version: "1" }] };
const WITH_PACK = { current: { pack_id: "banking-regulatory", version: "1" }, available: NO_PACK.available };
const NO_DOCS = { documents: [] };

function Docs(p: Partial<React.ComponentProps<typeof DocumentsTab>> = {}) {
  return <DocumentsTab projectId="northpeak-bank-b" actor="ceo" {...p} />;
}

describe("Documents tab: Add regulation", () => {
  it("is disabled with a reason before a pack is set, and does not submit", async () => {
    mockKgFetch({ "/v1/knowledge/packs$": { body: NO_PACK }, "/v1/knowledge/documents$": { body: NO_DOCS } });
    await mount(<Docs />);
    expect($('[data-testid="kg-doc-form-disabled"]')?.textContent).toMatch(/pack first/);
    expect($('[data-testid="kg-doc-submit"]')?.hasAttribute("disabled")).toBe(true);
    await press($('[data-testid="kg-doc-submit"]'));
    expect(calls.some((c) => c.startsWith("POST"))).toBe(false);
  });

  it("sends exactly the operator's typed title, citation, url and clause text, then reloads the list", async () => {
    const fn = mockKgFetch({ "/v1/knowledge/packs$": { body: WITH_PACK }, "/v1/knowledge/documents$": { body: NO_DOCS } });
    await mount(<Docs />);
    await act(async () => type($('[data-testid="kg-doc-title"]') as HTMLInputElement, "Reg Z"));
    await act(async () => type($('[data-testid="kg-doc-citation"]') as HTMLInputElement, "12 CFR 1026"));
    await act(async () => type($('[data-testid="kg-doc-url"]') as HTMLInputElement, "https://example.test/regz"));
    await act(async () => type($('[data-testid="kg-doc-clause-section"]') as HTMLInputElement, "1026.36(c)(1)(i)"));
    await act(async () => type($('[data-testid="kg-doc-clause-textarea"]') as HTMLTextAreaElement, "A loan originator shall not."));

    await press($('[data-testid="kg-doc-submit"]'));

    const post = fn.mock.calls.find(([u, i]) => String(u).includes("/v1/knowledge/documents") && i?.method === "POST");
    expect(post).toBeTruthy();
    expect(JSON.parse(String(post?.[1]?.body))).toMatchObject({
      project_id: "northpeak-bank-b", actor: "ceo", step_up: "confirmed",
      title: "Reg Z", citation: "12 CFR 1026", source_url: "https://example.test/regz",
      clauses: [{ section: "1026.36(c)(1)(i)", text: "A loan originator shall not." }],
    });
    expect($('[data-testid="kg-doc-submit-msg"]')?.textContent).toBe("Regulation added.");
    expect(calls.filter((c) => c.startsWith("GET /v1/knowledge/documents")).length).toBe(2);
  });

  it("will not submit until every clause has a section and exact text", async () => {
    mockKgFetch({ "/v1/knowledge/packs$": { body: WITH_PACK }, "/v1/knowledge/documents$": { body: NO_DOCS } });
    await mount(<Docs />);
    await act(async () => type($('[data-testid="kg-doc-title"]') as HTMLInputElement, "Reg Z"));
    expect($('[data-testid="kg-doc-submit"]')?.hasAttribute("disabled")).toBe(true);
    await press($('[data-testid="kg-doc-submit"]'));
    expect(calls.some((c) => c.startsWith("POST"))).toBe(false);
  });
});

describe("Documents tab: ontology pack row", () => {
  it("shows no pack assigned, then Change sends the chosen pack_id", async () => {
    const fn = mockKgFetch({ "/v1/knowledge/packs$": { body: NO_PACK }, "/v1/knowledge/documents$": { body: NO_DOCS } });
    await mount(<Docs />);
    expect($('[data-testid="kg-doc-pack-value"]')?.textContent).toBe("No pack assigned");

    await press($('[data-testid="kg-doc-pack-change"]'));
    await act(async () => {
      const sel = $('[data-testid="kg-doc-pack-pick"] select') as HTMLSelectElement;
      sel.value = "banking-dq";
      sel.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await press($('[data-testid="kg-doc-pack-pick"] button'));

    const post = fn.mock.calls.find(([u, i]) => String(u).includes("/v1/knowledge/packs") && i?.method === "POST");
    expect(post).toBeTruthy();
    expect(JSON.parse(String(post?.[1]?.body))).toMatchObject({
      project_id: "northpeak-bank-b", actor: "ceo", step_up: "confirmed", pack_id: "banking-dq",
    });
  });

  it("shows the current pack once one is set", async () => {
    mockKgFetch({ "/v1/knowledge/packs$": { body: WITH_PACK }, "/v1/knowledge/documents$": { body: NO_DOCS } });
    await mount(<Docs />);
    expect($('[data-testid="kg-doc-pack-value"]')?.textContent).toBe("banking-regulatory (v1)");
    expect($('[data-testid="kg-doc-form-disabled"]')).toBeNull();
  });
});
