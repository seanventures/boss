// @vitest-environment happy-dom
/**
 * THE MOVED KNOWLEDGE SCREENS LOST NOTHING (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §5.1, "a redesign
 * keeps every capability"). Every row of app/knowledge/parity.ts — a capability the old
 * Reliability explorer/settings or the Approvals fact queue had — is opened on its new home in
 * the Graph area with the fixture routes, pressed as the row says, and its control is there.
 * The old screens are gone, and each old place says where they went.
 */
import { existsSync, readFileSync } from "node:fs";
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { GraphArea } from "../app/knowledge/graph-area";
import { KnowledgeMovedCard } from "../app/knowledge/moved-card";
import { KG_PARITY } from "../app/knowledge/parity";
import { mockKgFetch } from "./_helpers/kg-fetch";
import { $, mount, settle, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn() }), usePathname: () => "/", useSearchParams: () => new URLSearchParams(window.location.search) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); vi.useRealTimers(); });

function setInput(el: HTMLInputElement, value: string) {
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  set.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

describe("the Graph area keeps every capability of the screens it replaced", () => {
  it("the table covers the old explorer, settings and fact queue", () => {
    const ids = KG_PARITY.map((r) => r.id);
    for (const id of ["search", "why", "blast", "index-now", "repo-status", "embedding", "retention", "promote", "reject", "forget"]) {
      expect(ids, id).toContain(id);
    }
    expect(new Set(ids).size).toBe(ids.length);
  });

  for (const row of KG_PARITY) {
    it(`${row.id}: ${row.capability} → ${row.now}`, async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      mockKgFetch();
      const node = row.check.press?.find((p) => p.startsWith("@node:"))?.slice(6);
      window.history.replaceState(null, "", `/?area=knowledge&kg_tab=${row.check.tab}${node ? `&kg_node=${node}` : ""}`);
      await mount(<GraphArea projectId="northpeak-bank-b" actor="ceo" orgId="o1" />);
      await settle();
      for (const p of row.check.press ?? []) {
        if (p.startsWith("@node:")) continue;
        if (p.startsWith("@search:")) {
          await act(async () => setInput($('[data-testid="kg-search-input"]') as HTMLInputElement, p.slice(8)));
          await act(async () => { vi.advanceTimersByTime(200); });
          await settle();
          continue;
        }
        await act(async () => ($(`[data-testid="${p}"]`) as HTMLElement).click());
        await settle();
      }
      expect($(`[data-testid="${row.check.testid}"]`), row.check.testid).not.toBeNull();
    });
  }

  it("a search pick focuses the best match, opens its detail and the tree", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockKgFetch();
    window.history.replaceState(null, "", "/?area=knowledge");
    await mount(<GraphArea projectId="northpeak-bank-b" actor="ceo" />);
    await settle();
    const input = $('[data-testid="kg-search-input"]') as HTMLInputElement;
    await act(async () => setInput(input, "disburse"));
    await act(async () => { vi.advanceTimersByTime(200); });
    await settle();
    await act(async () => { input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); });
    await settle();
    expect($('[data-testid="kg-canvas"]')?.getAttribute("data-mode")).toBe("focus");
    expect($('[data-testid="kg-panel"]')?.getAttribute("data-node")).toBe("kgn_fffaccf9cb8a4a37abbe");
    expect($('[data-testid="kg-announce"]')?.textContent).toMatch(/^Selected method disburse\(\)/);
  });

  it("the old components are gone and the old places point to the new ones", async () => {
    expect(existsSync("app/knowledge-promotion-ui.tsx")).toBe(false);
    const old = readFileSync("app/knowledge-console.tsx", "utf8");
    expect(old).not.toMatch(/export function Knowledge(Explorer|Settings)/);
    window.history.replaceState(null, "", "/?org=o1&area=reliability");
    await mount(<><KnowledgeMovedCard to="explorer" orgId="o1" /><KnowledgeMovedCard to="facts" orgId="o1" /></>);
    expect($('[data-testid="knowledge-moved-explorer"] a')?.getAttribute("href")).toBe("/?org=o1&area=knowledge");
    expect($('[data-testid="knowledge-moved-facts"] a')?.getAttribute("href")).toBe("/?org=o1&area=knowledge&kg_tab=rules&kg_sub=facts");
  });
});
