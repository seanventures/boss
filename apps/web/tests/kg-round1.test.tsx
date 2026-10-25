// @vitest-environment happy-dom
/**
 * THE GRAPH AREA, TESTER ROUND 1 (fc30744bb): each class of defect the independent tester found,
 * held by a test so it cannot come back. Layout and overview readability are in
 * tests/kg-layout.test.ts; area-scoped shortcuts in tests/every-area-shortcut-is-scoped-to-its-area.test.tsx;
 * unstyled buttons in tests/every-button-class-is-styled.test.ts.
 */
import { readFileSync } from "node:fs";
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GraphArea } from "../app/knowledge/graph-area";
import { tipPlace } from "../app/knowledge/graph-canvas";
import { bubbleSub } from "../app/knowledge/graph-view";
import { KnowledgeMovedCard } from "../app/knowledge/moved-card";
import { ProjectTree } from "../app/knowledge/project-tree";
import { RulesReview } from "../app/knowledge/rules-review";
import { distinctPaths, kgWhen, rankHits } from "../lib/kg-view";
import { FX, calls, mockKgFetch } from "./_helpers/kg-fetch";
import { $, $$, mount, settle, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn() }), usePathname: () => "/", useSearchParams: () => new URLSearchParams(window.location.search) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
beforeEach(() => window.history.replaceState(null, "", "/?area=knowledge"));
afterEach(() => { unmount(); vi.unstubAllGlobals(); vi.useRealTimers(); });

const press = async (el: Element | null) => { await act(async () => (el as HTMLElement).click()); await settle(); };
const key = (el: Element, k: string, init: KeyboardEventInit = {}) =>
  act(async () => { el.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true, ...init })); });

describe("P0-3 a route that is not on the install yet is a designed state", () => {
  it("Rules review says when it arrives, never 'Not Found · Retry'", async () => {
    mockKgFetch({ "/view/rules$": { status: 404, body: { detail: "Not Found" } } });
    await mount(<RulesReview projectId="p" actor="ceo" onFocusNode={() => {}} />);
    const card = $('[data-testid="kg-review-error-unavailable"]');
    expect(card?.textContent).toContain("Not on this install yet");
    expect(card?.textContent).not.toMatch(/Not Found/);
    expect(document.body.textContent).not.toMatch(/Not Found/);
  });

  it("a real refusal still says the server's words", async () => {
    mockKgFetch({ "/view/rules$": { status: 500, body: { detail: "the rules table is locked for a migration" } } });
    await mount(<RulesReview projectId="p" actor="ceo" onFocusNode={() => {}} />);
    expect($('[data-testid="kg-review-error"]')?.textContent).toContain("the rules table is locked for a migration");
  });
});

describe("P1-1 ⌘K opens the Graph's own search, and the shell's palette stands aside", () => {
  it("focuses the Graph search and marks the key as answered", async () => {
    mockKgFetch();
    await mount(<GraphArea projectId="p" actor="ceo" />);
    const ev = new KeyboardEvent("keydown", { key: "k", metaKey: true, bubbles: true, cancelable: true });
    await act(async () => { document.body.dispatchEvent(ev); });
    expect(document.activeElement).toBe($('[data-testid="kg-search-input"]'));
    expect(ev.defaultPrevented).toBe(true);
    expect(readFileSync("app/ConsoleShell.tsx", "utf8")).toMatch(/if \(e\.defaultPrevented\) return;\s*if \(\(e\.metaKey \|\| e\.ctrlKey\) && e\.key\.toLowerCase\(\) === "k"\)/);
  });
});

describe("P1-2 the moved-to-Graph cards keep the project", () => {
  it("carries ?project_id= from the page it is on, and is a styled card", async () => {
    window.history.replaceState(null, "", "/?org=o1&project_id=lab-7&area=decisions");
    await mount(<><KnowledgeMovedCard to="facts" orgId="o1" /><KnowledgeMovedCard to="explorer" orgId="o1" /></>);
    const facts = new URL($('[data-testid="knowledge-moved-facts"] a')!.getAttribute("href")!, "http://x");
    expect(Object.fromEntries(facts.searchParams)).toMatchObject({ org: "o1", project_id: "lab-7", area: "knowledge", kg_tab: "rules", kg_sub: "facts" });
    const graph = new URL($('[data-testid="knowledge-moved-explorer"] a')!.getAttribute("href")!, "http://x");
    expect(graph.searchParams.get("project_id")).toBe("lab-7");
    expect($('[data-testid="knowledge-moved-facts"]')?.className).toBe("kg-moved");
    expect(readFileSync("app/knowledge/kg.css", "utf8")).toMatch(/\.kg-moved \{/);
  });
});

describe("P1-5 the canvas and the panel show the same connections (round 2: deterministic)", () => {
  it("the canvas draws exactly what the server answered (grouped past the cap), the panel lists one row per drawn neighbour", async () => {
    const center = FX.hood.center;
    window.history.replaceState(null, "", `/?area=knowledge&kg_node=${center.id}`);
    mockKgFetch({ "/view/node/": { body: { ...FX.detail, node: center } } });
    await mount(<GraphArea projectId="p" actor="ceo" />);
    await settle();
    const drawn = Number($('[data-testid="kg-canvas"]')?.getAttribute("data-nodes"));
    // 80 read: as many drawn on the ring as the canvas shows readably, the rest in package groups; the same number every time.
    expect(drawn).toBeLessThan(FX.hood.nodes.length);
    expect($('[data-testid="kg-view-note"]')?.textContent).toMatch(/and 80 connected within 1 hop · \d+ drawn, the rest in \d+ groups?/);
    const inView = $$('[data-testid="kg-conn-view"] [data-testid="kg-conn-item"]');
    expect(inView.length).toBe(drawn - 1);
    const labels = inView.map((e) => e.textContent);
    expect(new Set(labels).size).toBe(labels.length);
  });
});

describe("P1-6 only Rules on, and none around the node: the designed prompt with Scan", () => {
  it("says 'No rules here yet' with Scan for rules here — not 'try more hops'", async () => {
    window.history.replaceState(null, "", `/?area=knowledge&kg_node=${FX.hood.center.id}`);
    mockKgFetch({ "/view/neighbourhood$": (u) => ({ body: u.searchParams.get("layers") === "rules" ? { ...FX.hood, nodes: [FX.hood.center], edges: [], truncated: false, hidden: { nodes: 0, edges: 0 } } : FX.hood }) });
    await mount(<GraphArea projectId="p" actor="ceo" />);
    await press($('[data-testid="kg-chip-rules"]'));
    expect($('[data-testid="kg-empty-layer"]')?.textContent).toContain("No rules here yet");
    expect($('[data-testid="kg-empty-layer"] [data-testid="kg-scan"]')).not.toBeNull();
    expect(document.body.textContent).not.toContain("try more hops");
  });
});

describe("P1-7 / P1-8 the tree follows the selection and keeps the WAI-ARIA pattern", () => {
  const LOAN = "area:paramg/northpeak-fineract-b:fineract-loan";
  const PKG = FX.tree.classes.parent!;
  const CLS = FX.tree.methods.parent!;

  it("a selection made elsewhere opens the tree to it: ancestors loaded and expanded, the row selected", async () => {
    mockKgFetch();
    const cls = FX.tree.classes.rows.find((r) => r.key === CLS)!;
    await mount(<ProjectTree projectId="p" actor="ceo" tab="project" onTab={() => {}} query="" selectedId={cls.node_id}
                             onPick={() => {}} reveal={{ label: cls.label, nodeId: cls.node_id! }} />);
    await settle();
    // Scrolled to it: the selected row and its package are drawn.
    expect($(`[data-key="${CSS.escape(CLS)}"]`)?.getAttribute("aria-selected")).toBe("true");
    expect($(`[data-key="${CSS.escape(PKG)}"]`)?.getAttribute("aria-expanded")).toBe("true");
    // Its area, further up, is open too.
    const list = $('[data-testid="kg-tree-list"]')!;
    list.scrollTop = 0;
    await act(async () => { list.dispatchEvent(new Event("scroll")); });
    expect($(`[data-key="${CSS.escape(LOAN)}"]`)?.getAttribute("aria-expanded")).toBe("true");
    // The tree's own filter is untouched by a selection.
    expect($('[data-testid="kg-tree-note"]')).toBeNull();
  });

  it("an area selected on the canvas is the selected row", async () => {
    mockKgFetch();
    await mount(<ProjectTree projectId="p" actor="ceo" tab="project" onTab={() => {}} query="" selectedId={FX.tree.areas.rows[1].key} onPick={() => {}} />);
    expect($(`[data-key="${CSS.escape(FX.tree.areas.rows[1].key)}"]`)?.getAttribute("aria-selected")).toBe("true");
  });

  it("Right opens, Right again goes in, Down moves one row, Enter selects without toggling, Left goes out", async () => {
    mockKgFetch();
    const onPick = vi.fn();
    await mount(<ProjectTree projectId="p" actor="ceo" tab="project" onTab={() => {}} query="" selectedId={LOAN} onPick={onPick} />);
    const loan = () => $(`[data-key="${CSS.escape(LOAN)}"]`)!;
    expect(loan().getAttribute("tabindex")).toBe("0");
    await key(loan(), "ArrowRight");
    await settle();
    expect(loan().getAttribute("aria-expanded")).toBe("true");
    await key(loan(), "ArrowRight");
    await settle();
    const first = FX.tree.packages.rows[0].key;
    expect(document.activeElement?.getAttribute("data-key")).toBe(first);
    await key(document.activeElement!, "ArrowDown");
    await settle();
    const second = FX.tree.packages.rows[1].key;
    expect(document.activeElement?.getAttribute("data-key")).toBe(second);
    await key(document.activeElement!, "Enter");
    expect(onPick).toHaveBeenLastCalledWith(expect.objectContaining({ nodeId: null, row: expect.objectContaining({ key: second }) }));
    expect($(`[data-key="${CSS.escape(second)}"]`)?.getAttribute("aria-expanded")).toBe("false");
    await key(document.activeElement!, "ArrowLeft");
    await settle();
    expect(document.activeElement?.getAttribute("data-key")).toBe(LOAN);
    // The tree never lets an arrow reach the page (another area's shortcut).
    const leak = vi.fn();
    window.addEventListener("keydown", leak);
    await key(loan(), "ArrowDown");
    window.removeEventListener("keydown", leak);
    expect((leak.mock.calls[0]?.[0] as KeyboardEvent | undefined)?.defaultPrevented).toBe(true);
  });
});

describe("P1-9 search results are told apart", () => {
  it("same-named hits show the path segment that differs", () => {
    const p = distinctPaths([
      "fineract-provider/src/main/java/org/apache/fineract/portfolio/loanaccount/service/LoanAssembler.java",
      "fineract-loan/src/main/java/org/apache/fineract/portfolio/savings/service/LoanAssembler.java",
      "fineract-core/src/main/java/org/x/Other.java",
    ]);
    expect(p[0]).toContain("loanaccount");
    expect(p[1]).toContain("savings");
    expect(p[0]).not.toBe(p[1]);
    expect(p[0].length).toBeLessThanOrEqual(56);
  });

  it("a type-like query ranks types above members inside each tier", () => {
    const hit = (label: string, kind: string, match: string) => ({ node: { label, symbol_kind: kind }, match });
    const ranked = rankHits("Loan", [hit("loan()", "method", "exact"), hit("Loan", "class", "exact"), hit("loanX()", "method", "prefix"), hit("LoanX", "class", "prefix")]);
    expect(ranked.map((h) => h.node.label)).toEqual(["Loan", "loan()", "LoanX", "loanX()"]);
    expect(rankHits("loan", [hit("loan()", "method", "exact"), hit("Loan", "class", "exact")]).map((h) => h.node.label)).toEqual(["loan()", "Loan"]);
  });
});

describe("P1-12, P1-13, P2", () => {
  it("one time formatter, labelled with its zone", () => {
    expect(kgWhen("2026-10-24T09:40:11Z")).toMatch(/UTC|GMT|[A-Z]{2,4}|[+-]\d/);
    for (const f of ["node-panel.tsx", "rules-review.tsx", "learned-strip.tsx"]) {
      expect(readFileSync(`app/knowledge/${f}`, "utf8"), f).not.toMatch(/formatDateTimeShort|toLocale(Date|Time)?String\(undefined/);
    }
  });

  it("the tooltip flips to stay inside the canvas", () => {
    expect(tipPlace({ x: 100, y: 100 }, { w: 800, h: 600 })).toMatchObject({ left: 114, top: 114 });
    const edge = tipPlace({ x: 790, y: 590 }, { w: 800, h: 600 });
    expect(edge.left).toBeUndefined();
    expect(edge.right).toBeGreaterThanOrEqual(8);
    expect(edge.bottom).toBeGreaterThanOrEqual(8);
  });

  it("a Python area says its functions, not '0 classes'", () => {
    expect(bubbleSub({ id: "a", key: "a", kind: "area", layer: "code", label: "app", trust_tier: "observed", counts: { classes: 0, methods: 24 } })).toBe("24 functions");
    expect(bubbleSub({ id: "a", key: "a", kind: "area", layer: "code", label: "app", trust_tier: "observed", counts: { classes: 869, methods: 4519 } })).toBe("869 classes");
  });

  it("the away-from-home banner is drawn in the dark tokens, never a cream fallback", () => {
    const css = readFileSync("app/globals.css", "utf8");
    const rule = css.slice(css.indexOf(".project-away {"), css.indexOf("}", css.indexOf(".project-away {")));
    // Round 2: in the console's accent tokens (a grey bar read as off-theme); a lab keeps amber.
    expect(rule).toContain("var(--accent-soft)");
    expect(css).toContain('.project-away[data-lab] { border-color: color-mix(in srgb, var(--amber) 40%, var(--border)); background: var(--amber-soft); }');
    expect(rule).not.toMatch(/#fff8e6|surface-warn/);
  });

  it("the toolbar is one row on desktop widths, never clipped; the layout counts the graph's own width (container queries)", () => {
    const css = readFileSync("app/knowledge/kg.css", "utf8");
    // Round 3: a row that does not fit wraps, its end controls together on the right — never cut off.
    expect(css).toMatch(/\.kg-toolbar \{[^}]*flex-wrap: wrap/);
    expect(css).toMatch(/\.kg-toolbar-end \{[^}]*margin-left: auto/);
    expect(css).toMatch(/@container kg \(max-width: 1150px\) \{[^}]*\.kg-tree-word/);
    expect(css).toMatch(/container: kg \/ inline-size/);
    expect(css).toMatch(/@container kg \(max-width: 860px\)/);
    expect(css).not.toMatch(/@media \(max-width: 1100px\)/);
    expect(calls).toBeDefined();
  });
});
