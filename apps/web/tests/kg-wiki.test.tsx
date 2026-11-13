// @vitest-environment happy-dom
/**
 * BOSSAPP-891, the console side: Knowledge Graph › Wiki (app/knowledge/kg-wiki.tsx), the other half
 * of apps/api/tests/test_wiki_api.py. Breaks covered: the list not opening the first page; a
 * person's Save not sending exactly the typed title and body to PUT /v1/wiki/pages/{slug}; a page
 * with an agent's pending change not saying it waits for approval; Refresh from the graph not
 * saying what it updated and what it proposed.
 *
 * BOSSAPP-1037 (data loss): the new-page editor said "Saved as <slug>" before any PUT, nothing
 * guarded unsaved text, and a save did not show in the list until a reload. Covered: the slug
 * line never reads as saved until the PUT answered; unsaved text holds a beforeunload guard and
 * an in-app move asks first (and stays when told to); a save shows in the list at once and says
 * "Saved"; a list read answering late never paints over the saved list.
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WikiTab, wikiDraft } from "../app/knowledge/kg-wiki";
import { LEAVE_UNSAVED } from "../lib/unsaved-guard";
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

describe("Wiki › nothing typed is lost silently (BOSSAPP-1037)", () => {
  it("wikiDraft: unsaved until it differs from what was loaded; the slug line is a promise, not a receipt", () => {
    const blank = { title: "", body: "" };
    expect(wikiDraft({ slug: null, initial: blank, title: "", body: "" })).toEqual({ dirty: false, target: "", slugLine: null });
    const typed = wikiDraft({ slug: null, initial: blank, title: "On-call Rota", body: "" });
    expect(typed).toEqual({ dirty: true, target: "on-call-rota", slugLine: "Will be saved as on-call-rota" });
    expect(typed.slugLine).not.toMatch(/^Saved/);
    const page = { title: "Deploys", body: "x" };
    expect(wikiDraft({ slug: "deploys", initial: page, title: "Deploys", body: "x" })).toMatchObject({ dirty: false, slugLine: null });
    expect(wikiDraft({ slug: "deploys", initial: page, title: "Deploys", body: "y" }).dirty).toBe(true);
  });

  function withWikiWrites(state: { saved: boolean }, slowFirstList?: { release?: () => void }) {
    const base = mockKgFetch({
      "/v1/wiki/pages$": () => ({ body: state.saved ? { pages: [...PAGES.pages, NEW_ROW] } : { pages: [] } }),
      "/v1/wiki/pages/rota$": { body: { ...PAGE, slug: "rota", title: "Rota", body: "Who is on call.", backlinks: [], pending: [] } },
    } as never);
    let firstList = true;
    const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const u = String(input);
      if (init?.method === "PUT" && u.includes("/v1/wiki/pages/rota")) {
        state.saved = true;
        return new Response(JSON.stringify({ changed: true, revision: { ...REV, revision_no: 1, title: "Rota", author_kind: "person", author_id: "ceo" } }),
                            { status: 200, headers: { "content-type": "application/json" } });
      }
      if (slowFirstList && firstList && /\/v1\/wiki\/pages\?/.test(u)) {
        firstList = false;
        // The first list read answers only after the save: it holds the old (empty) list.
        const stale = await base(input, init);
        await new Promise<void>((r) => { slowFirstList.release = r; });
        return stale;
      }
      return base(input, init);
    });
    vi.stubGlobal("fetch", fn);
    return fn;
  }
  const NEW_ROW = { slug: "rota", title: "Rota", revision_no: 1, author_kind: "person", author_id: "ceo", updated_at: null,
                    maintained_by: null, pending: 0, chars: 15 };

  it("a new page says 'Will be saved as', guards the unsaved text, and only a real save says Saved", async () => {
    const state = { saved: false };
    withWikiWrites(state);
    await mount(<WikiTab projectId="alpha" actor="ceo" />);
    expect($('[data-testid="wiki-empty"]')).toBeTruthy();
    await press($('[data-testid="wiki-new"]'));
    await act(async () => type($('[data-testid="wiki-title"]') as HTMLInputElement, "Rota"));
    await act(async () => type($('[data-testid="wiki-body"]') as HTMLTextAreaElement, "Who is on call."));
    expect($('[data-testid="wiki-slug"]')?.textContent).toBe("Will be saved as rota");
    expect($('[data-testid="wiki-unsaved"]')?.textContent).toBe("Not saved yet");
    expect(document.body.textContent).not.toMatch(/Saved as|Saved “/);

    // Closing the tab asks while the text is unsaved.
    const leaving = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(leaving);
    expect(leaving.defaultPrevented).toBe(true);

    // Cancel asks first; "stay" keeps every word.
    const confirm = vi.fn(() => false);
    vi.stubGlobal("confirm", confirm);
    await press($('[data-testid="wiki-cancel"]'));
    expect(confirm).toHaveBeenCalledWith(LEAVE_UNSAVED);
    expect(($('[data-testid="wiki-body"]') as HTMLTextAreaElement).value).toBe("Who is on call.");

    await press($('[data-testid="wiki-save"]'));
    expect(state.saved).toBe(true);
    expect($('[data-testid="wiki-editor"]')).toBeNull();
    expect($('[data-testid="wiki-note"]')?.textContent).toBe("Saved “Rota”.");
    const listed = [...document.querySelectorAll('[data-testid="wiki-page-link"]')].map((b) => b.textContent);
    expect(listed).toContain("Rota");
    expect($('[data-testid="wiki-empty"]')).toBeNull();
    // Saved: nothing is held any more.
    const after = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(after);
    expect(after.defaultPrevented).toBe(false);
  });

  it("moving to another page with unsaved edits asks, and goes only when told to", async () => {
    routes();
    await mount(<WikiTab projectId="alpha" actor="ceo" />);
    await press($('[data-testid="wiki-edit"]'));
    await act(async () => type($('[data-testid="wiki-body"]') as HTMLTextAreaElement, "Half a thought"));
    const confirm = vi.fn(() => false);
    vi.stubGlobal("confirm", confirm);
    const runbook = [...document.querySelectorAll<HTMLElement>('[data-testid="wiki-page-link"]')].find((b) => b.textContent === "Runbook");
    await press(runbook!);
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(($('[data-testid="wiki-body"]') as HTMLTextAreaElement).value).toBe("Half a thought");
    confirm.mockReturnValue(true);
    await press(runbook!);
    expect($('[data-testid="wiki-editor"]')).toBeNull();
  });

  it("a list read that answers after the save never paints the old list back", async () => {
    const state = { saved: false };
    const slow: { release?: () => void } = {};
    withWikiWrites(state, slow);
    await mount(<WikiTab projectId="alpha" actor="ceo" />);
    await press($('[data-testid="wiki-new"]'));
    await act(async () => type($('[data-testid="wiki-title"]') as HTMLInputElement, "Rota"));
    await press($('[data-testid="wiki-save"]'));
    await act(async () => slow.release?.());
    await settle();
    const listed = [...document.querySelectorAll('[data-testid="wiki-page-link"]')].map((b) => b.textContent);
    expect(listed).toContain("Rota");
  });
});
