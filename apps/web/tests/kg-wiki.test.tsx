// @vitest-environment happy-dom
/**
 * BOSSAPP-891, the console side: Knowledge Graph › Wiki (app/knowledge/kg-wiki.tsx), the other half
 * of apps/api/tests/test_wiki_api.py. Breaks covered: the list not opening the first page; a
 * person's Save not sending exactly the typed title and body to PUT /v1/wiki/pages/{slug}; a page
 * with an agent's pending change not saying it waits for approval; Refresh from the graph not
 * saying what it updated and what it proposed.
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WikiTab } from "../app/knowledge/kg-wiki";
import { mockKgFetch } from "./_helpers/kg-fetch";
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

const REV = { id: "wkr_1", revision_no: 2, title: "Deploys", body: "Deploy daily. See [[runbook]].", author_kind: "agent",
              author_id: "agent:pm", source_note: "", created_at: "2026-11-13T10:00:00Z", restored_from: null };
const PAGES = { pages: [
  { slug: "deploys", title: "Deploys", revision_no: 2, author_kind: "agent", author_id: "agent:pm", updated_at: null,
    maintained_by: null, pending: 1, chars: 30 },
  { slug: "runbook", title: "Runbook", revision_no: 1, author_kind: "person", author_id: "ceo", updated_at: null,
    maintained_by: null, pending: 0, chars: 8 },
] };
const PAGE = { slug: "deploys", title: "Deploys", body: REV.body, maintained_by: null, revision: REV, links: ["runbook"],
               backlinks: [{ slug: "runbook", title: "Runbook" }],
               pending: [{ id: "ccn_1", decision_id: "dec_1", requested_by: "agent:pm", diff: "-a\n+b", created_at: null }] };

function routes(extra: Record<string, unknown> = {}) {
  return mockKgFetch({
    "/v1/wiki/pages$": { body: PAGES },
    "/v1/wiki/pages/deploys$": (u: URL) => ({ body: u.pathname ? PAGE : PAGE }),
    "/v1/wiki/maintain$": { body: { written: ["kg-service", "knowledge"], proposed: ["kg-team"], unchanged: [] } },
    ...extra,
  } as never);
}

describe("Knowledge Graph › Wiki", () => {
  it("opens the first page, renders it with its links and says a change is waiting", async () => {
    routes();
    await mount(<WikiTab projectId="alpha" actor="ceo" />);
    expect($('[data-testid="wiki-page-title"]')?.textContent).toBe("Deploys");
    expect($('[data-testid="wiki-body-view"]')?.textContent).toContain("Deploy daily.");
    expect($('[data-testid="wiki-body-view"] a')?.getAttribute("href")).toContain("wiki=runbook");
    expect($('[data-testid="wiki-pending"]')?.textContent).toMatch(/wait for approval/);
    expect($('[data-testid="wiki-backlinks"]')?.textContent).toContain("Runbook");
  });

  it("saves exactly what the person typed with PUT", async () => {
    const fn = routes();
    await mount(<WikiTab projectId="alpha" actor="ceo" />);
    await press($('[data-testid="wiki-edit"]'));
    await act(async () => type($('[data-testid="wiki-body"]') as HTMLTextAreaElement, "Deploy twice a day."));
    await press($('[data-testid="wiki-save"]'));
    const put = fn.mock.calls.find(([u, i]) => String(u).includes("/v1/wiki/pages/deploys") && i?.method === "PUT");
    expect(put).toBeTruthy();
    expect(JSON.parse(String(put?.[1]?.body))).toEqual({ project_id: "alpha", actor: "ceo", title: "Deploys", body: "Deploy twice a day." });
  });

  it("refreshes from the graph and says what was updated and proposed", async () => {
    routes();
    await mount(<WikiTab projectId="alpha" actor="ceo" />);
    await press($('[data-testid="wiki-refresh"]'));
    expect($('[data-testid="wiki-note"]')?.textContent).toBe(
      "2 page(s) updated from the graph; 1 proposed for your approval (edited by a person).");
  });
});
