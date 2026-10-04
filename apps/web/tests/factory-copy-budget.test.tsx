// @vitest-environment happy-dom
/**
 * Tooltips, not paragraphs, on the Factory's Floor, header, Apps and sheets (factory spec §5.1).
 * Each is RENDERED from fixtures, opened, and walked with the Studio's walker: no visible run
 * over six words outside a tooltip, no <p>, and none of the banned words. App names, build
 * titles and anything an agent or operator wrote are `data-user-text`.
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { overBudget } from "./_helpers/copy-budget";
import { attentionBudget, floor } from "./_helpers/factory-fixtures";
import { $, click, mount, settle, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { CodeTasksHome } = await import("../app/code-tasks-ui");

const SKIP = "[data-tip], [role='tooltip'], [title], textarea, script, style, option, [hidden], "
  + "[aria-hidden='true'], .ag-sr, [data-user-text]";
const BANNED = /(\b(persona|fingerprint|ledger|acts?|sentient|alive|conscious|impersonate|signed|tokens?|eta|estimated|almost|projects?|code tasks?|pipeline|fail-closed|machinery|governed|ratchet|mission)\b|%)/i;

function visibleRuns(root: Element): string[] {
  const out: string[] = [];
  for (const el of [root, ...root.querySelectorAll("*")]) {
    if (el.closest(SKIP)) continue;
    const own = [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent ?? "").join("").trim();
    if (own) out.push(own);
  }
  return out;
}
function audit(selector = ".fx, .fx-sheet") {
  const roots = [...document.querySelectorAll(selector)];
  return {
    surfaces: roots.length,
    over: [...new Set(roots.flatMap((r) => overBudget(r)))],
    paragraphs: roots.flatMap((r) => [...r.querySelectorAll("p")].map((p) => p.textContent ?? "")),
    banned: [...new Set(roots.flatMap(visibleRuns).filter((t) => BANNED.test(t)))],
  };
}

const body = floor({ attention: [...(floor().attention as unknown[]), attentionBudget()] });
beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) =>
    String(url).startsWith("/v1/factory/floor") ? Response.json(body) : new Response("{}", { status: 404 })));
});
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const home = () => (
  <CodeTasksHome projectId="p1" apiReachable actor="ceo" benchmarks={[]}
                 codeTasks={[{ id: "crun_a1", title: "Invoice export", status: "completed", spec: { owner: "acme", repo: "app" } } as never]} />
);

describe("the Factory copy budget", () => {
  it("the header and the Floor, every card's details open", async () => {
    window.history.replaceState(null, "", "/?area=code_tasks");
    await mount(home());
    for (const b of [...document.querySelectorAll<HTMLElement>("[data-testid=ag-att-details]")]) await act(async () => { b.click(); });
    await settle();
    const a = audit();
    expect(a.surfaces).toBeGreaterThan(0);
    expect(a.over).toEqual([]);
    expect(a.paragraphs).toEqual([]);
    expect(a.banned).toEqual([]);
  });

  it("+ New build's menu and the Direct change sheet", async () => {
    window.history.replaceState(null, "", "/?area=code_tasks");
    await mount(home());
    await click($("[data-testid=fx-new-build]"));
    expect(audit()).toMatchObject({ over: [], paragraphs: [], banned: [] });
    await click($("[data-testid=fx-new-direct]"));
    expect($("[data-testid=fx-direct-sheet]")).not.toBeNull();
    expect(audit()).toMatchObject({ over: [], paragraphs: [], banned: [] });
  });

  it("the waiver sheet", async () => {
    window.history.replaceState(null, "", "/?area=code_tasks");
    await mount(home());
    await click($("[data-testid=ag-att-waive]"));
    expect($("[data-testid=fx-waive-sheet]")).not.toBeNull();
    expect(audit()).toMatchObject({ over: [], paragraphs: [], banned: [] });
  });

  it("the Apps tab, an app opened", async () => {
    window.history.replaceState(null, "", "/?area=code_tasks&view=apps");
    await mount(home());
    await click($("[data-testid^=fx-app-] .fx-row-open"));
    expect($("[data-testid^=fx-app-build-]")).not.toBeNull();
    expect(audit()).toMatchObject({ over: [], paragraphs: [], banned: [] });
  });
});
