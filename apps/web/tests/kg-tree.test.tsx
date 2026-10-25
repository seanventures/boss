// @vitest-environment happy-dom
/**
 * THE PROJECT TREE (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §5.5), on the real Fineract tree fixture:
 * children load lazily (one /tree/children call per opened level), the search shows matches and
 * their ancestors with the match highlighted, the selection follows the canvas, and the WAI-ARIA
 * tree pattern holds (role tree/treeitem, aria-level/expanded/selected, arrows, Enter focuses).
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ProjectTree, type TreePick } from "../app/knowledge/project-tree";
import { FX, calls, mockKgFetch } from "./_helpers/kg-fetch";
import { $, $$, mount, rerender, settle, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); vi.useRealTimers(); });

const LOAN = "area:paramg/northpeak-fineract-b:fineract-loan";
const row = (key: string) => $(`[data-key="${CSS.escape(key)}"]`);
const treeCalls = () => calls.filter((c) => c.includes("/tree/children"));
/** Scroll the virtualised list so the row with this key is drawn. */
async function scrollTo(key: string) {
  const list = $('[data-testid="kg-tree-list"]')!;
  const spacer = list.querySelector<HTMLElement>(':scope > div[style*="height"]')!;
  const at = [...spacer.children].findIndex((c) => c.getAttribute("data-key") === key);
  if (at >= 0) return;
  list.scrollTop = 0;
  for (let top = 0; top < 40000 && !row(key); top += 400) {
    list.scrollTop = top;
    await act(async () => { list.dispatchEvent(new Event("scroll")); });
  }
}

function Tree(props: Partial<React.ComponentProps<typeof ProjectTree>> & { onPick?: (p: TreePick) => void }) {
  return <ProjectTree projectId="northpeak-bank-b" actor="ceo" tab="project" onTab={() => {}} query="" selectedId={null}
                      onPick={() => {}} {...props} />;
}

describe("the project tree", () => {
  it("opens on the 36 areas with ARIA, kind dots and counts — and loads nothing else", async () => {
    mockKgFetch();
    await mount(<Tree />);
    expect($('[role="tree"]')).not.toBeNull();
    const rows = $$('[role="treeitem"]');
    // Virtualised: the list is as tall as every row, and only the rows in view (+ overscan) exist.
    const spacer = $('[data-testid="kg-tree-list"]')!.querySelector<HTMLElement>(':scope > div[style*="height"]')!;
    expect(spacer.style.height).toBe(`${FX.tree.areas.rows.length * 28}px`);
    expect(rows.length).toBeGreaterThan(20);
    expect(rows.length).toBeLessThanOrEqual(FX.tree.areas.rows.length);
    expect(rows[0].getAttribute("aria-level")).toBe("1");
    expect(rows[0].getAttribute("aria-expanded")).toBe("false");
    expect(rows[0].querySelector(".kg-dot")).not.toBeNull();
    expect(row(LOAN)?.querySelector(".kg-row-count")?.textContent).toBe(FX.tree.areas.rows.find((r) => r.key === LOAN)!.count.toLocaleString());
    expect(treeCalls()).toHaveLength(1);
    expect(treeCalls()[0]).toContain("tab=project");
    expect(treeCalls()[0]).not.toContain("parent=");
  });

  it("loads an area's packages only when it is opened, then a package's classes, then a class's methods", async () => {
    mockKgFetch();
    await mount(<Tree />);
    await scrollTo(LOAN);
    await act(async () => row(LOAN)!.click());
    await settle();
    expect(treeCalls()).toHaveLength(2);
    expect(decodeURIComponent(treeCalls()[1])).toContain(`parent=${LOAN}`);
    expect(row(LOAN)?.getAttribute("aria-expanded")).toBe("true");
    const pkg = FX.tree.classes.parent!;
    await scrollTo(pkg);
    expect(row(pkg)?.getAttribute("aria-level")).toBe("2");
    await act(async () => row(pkg)!.click());
    await settle();
    const cls = FX.tree.classes.rows[0];
    await scrollTo(cls.key);
    expect(row(cls.key)?.getAttribute("aria-level")).toBe("3");
    expect(row(cls.key)?.getAttribute("data-kind")).toBe("class");
    // Collapsing and reopening does not reload.
    await scrollTo(LOAN);
    await act(async () => (row(LOAN)!.querySelector(".kg-row-caret") as HTMLElement).click());
    await act(async () => (row(LOAN)!.querySelector(".kg-row-caret") as HTMLElement).click());
    await settle();
    expect(treeCalls()).toHaveLength(3);
  });

  it("a row with a node focuses it; the selection follows the canvas", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockKgFetch();
    const onPick = vi.fn();
    const m = FX.tree.methods;
    // Open straight to the methods by searching their class's member.
    await mount(<Tree onPick={onPick} query="validateDisbursement" />);
    await act(async () => { vi.advanceTimersByTime(200); });
    await settle();
    const target = m.rows.find((r) => r.label === "validateDisbursement")!;
    await act(async () => row(target.key)!.click());
    expect(onPick).toHaveBeenCalledWith(expect.objectContaining({ nodeId: target.node_id }));
    await rerender(<Tree onPick={onPick} query="validateDisbursement" selectedId={target.node_id} />);
    expect(row(target.key)?.getAttribute("aria-selected")).toBe("true");
    expect(row(target.key)?.getAttribute("tabindex")).toBe("0");
  });

  it("the search filters to matches plus their ancestors, opened, the match highlighted", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockKgFetch();
    await mount(<Tree query="LoanTransactionValidator" />);
    await act(async () => { vi.advanceTimersByTime(200); });
    await settle();
    const q = treeCalls().find((c) => c.includes("q=LoanTransactionValidator"));
    expect(q).toBeTruthy();
    const keys = $$('[role="treeitem"]').map((r) => r.getAttribute("data-key"));
    const match = keys.find((k) => k?.endsWith(":LoanTransactionValidator"));
    expect(match).toBeTruthy();
    // Its package and its area are shown above it, at their levels.
    expect(keys).toContain(LOAN);
    expect(row(match!)?.getAttribute("aria-level")).toBe("3");
    expect(row(match!)?.getAttribute("data-match")).toBe("true");
    expect(row(match!)?.querySelector("mark")?.textContent).toBe("LoanTransactionValidator");
    expect($('[data-testid="kg-tree-note"]')?.textContent).toMatch(/matching/);
  });

  it("says so when nothing matches", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    // Neither the tree nor the graph search finds it (one search source, round 5).
    mockKgFetch({ "/view/search$": { body: { q: "zzzz-no-such", hits: [], meaning_available: false } } });
    await mount(<Tree query="zzzz-no-such" />);
    await act(async () => { vi.advanceTimersByTime(200); });
    await settle();
    expect($('[data-testid="kg-tree-empty"]')?.textContent).toContain("Nothing in Project matches “zzzz-no-such”");
  });

  it("keyboard: arrows move and open, Left goes to the parent, Enter picks", async () => {
    mockKgFetch();
    const onPick = vi.fn();
    await mount(<Tree onPick={onPick} />);
    const first = $$('[role="treeitem"]')[0];
    expect(first.getAttribute("tabindex")).toBe("0");
    await scrollTo(LOAN);
    const key = (el: HTMLElement, k: string) => act(async () => { el.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true })); });
    await key(row(LOAN)!, "ArrowRight");
    await settle();
    expect(row(LOAN)?.getAttribute("aria-expanded")).toBe("true");
    const child = FX.tree.packages.rows[0];
    await key(row(LOAN)!, "ArrowRight");
    await scrollTo(child.key);
    await key(row(child.key)!, "ArrowLeft");
    await scrollTo(LOAN);
    await key(row(LOAN)!, "ArrowLeft");
    expect(row(LOAN)?.getAttribute("aria-expanded")).toBe("false");
    await key(row(LOAN)!, "Enter");
    await settle();
    expect(onPick).toHaveBeenCalledWith(expect.objectContaining({ nodeId: null }));
  });

  it("the Rules and Learning tabs list their groups; an error says why with Retry", async () => {
    mockKgFetch();
    await mount(<Tree tab="rules" />);
    expect($$('[role="treeitem"]').map((r) => r.textContent)).toEqual(expect.arrayContaining([expect.stringContaining("Proposed")]));
    unmount();
    mockKgFetch({ "/tree/children$": { status: 500, body: { detail: "the rollup table is missing" } } });
    await mount(<Tree tab="learning" />);
    expect($('[data-testid="kg-tree-error"]')?.textContent).toContain("the rollup table is missing");
    expect($('[data-testid="kg-tree-error"] button')?.textContent).toBe("Retry");
  });

  it("shows a skeleton while the first level loads, never a bare spinner", async () => {
    let release: () => void = () => {};
    mockKgFetch({ "/tree/children$": () => ({ body: FX.tree.areas }) });
    const real = globalThis.fetch;
    vi.stubGlobal("fetch", (...a: Parameters<typeof fetch>) => new Promise<Response>((res) => { release = () => res(real(...a) as unknown as Response); }));
    await mount(<Tree />);
    expect($('[data-testid="kg-tree-skeleton"]')).not.toBeNull();
    expect($('[role="tree"]')?.getAttribute("aria-busy")).toBe("true");
    await act(async () => release());
    await settle();
    expect($('[data-testid="kg-tree-skeleton"]')).toBeNull();
  });
});
