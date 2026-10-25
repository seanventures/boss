// @vitest-environment happy-dom
/**
 * THE GRAPH AREA, OPERATOR ROUND 2 (live review on boss-dogfood):
 *   1. it opens at a readable scale (bubbles and labels at their designed size), Fit stays a
 *      button; the legend has its own gutter and can never cover a node;
 *   2. the canvas starts high and fills the viewport;
 *   3. the details panel is a drawer, closed until something is selected, closed with × or Esc,
 *      remembered for the session;
 *   4. the class is the node: a class in the tree focuses the class; its methods are listed in its
 *      panel, each with its own connections, and open the method-level view;
 *   5. a Python or JavaScript area says its functions, never "0 classes".
 */
import { readFileSync } from "node:fs";
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { READABLE } from "../app/knowledge/graph-canvas";
import { GraphArea } from "../app/knowledge/graph-area";
import { PANEL_KEY, bubbleSub } from "../app/knowledge/graph-view";
import { NodePanel } from "../app/knowledge/node-panel";
import { countWords } from "../app/knowledge/project-tree";
import { FX, calls, mockKgFetch } from "./_helpers/kg-fetch";
import { $, $$, mount, settle, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn() }), usePathname: () => "/", useSearchParams: () => new URLSearchParams(window.location.search) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
beforeEach(() => { window.history.replaceState(null, "", "/?area=knowledge"); window.sessionStorage.clear(); });
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const press = async (el: Element | null) => { await act(async () => (el as HTMLElement).click()); await settle(); };
const css = readFileSync("app/knowledge/kg.css", "utf8");
const pct = () => Number(($('[data-testid="kg-zoom-pct"]')?.textContent ?? "0").replace("%", ""));

describe("1. it opens readable; Fit is a button; the legend has its own gutter", () => {
  it("opens no smaller than the readable scale, and Fit can go below it", async () => {
    mockKgFetch();
    await mount(<GraphArea projectId="p" actor="ceo" />);
    expect(pct()).toBeGreaterThanOrEqual(Math.round(READABLE * 100));
    expect(pct()).toBeLessThanOrEqual(100);
    await press($('[data-testid="kg-fit"]'));
    expect(pct()).toBeLessThanOrEqual(100);
  });

  it("the legend sits in a gutter under the canvas, never over it", () => {
    expect(css).toMatch(/\.kg-canvas-wrap \{ position: absolute; inset: 0 0 var\(--kg-gutter, 34px\) 0; \}/);
    expect(css).toMatch(/\.kg-legend \{[^}]*bottom: 0;[^}]*height: var\(--kg-gutter, 34px\)/);
  });
});

describe("2. the canvas starts high", () => {
  it("the area's frame, header and toolbar are slim", () => {
    expect(css).toContain('.panel-stack[data-area="knowledge"] { padding: 10px 12px 12px; gap: 6px; }');
    expect(css).toMatch(/\.bx-content\[data-active="knowledge"\]:has\(\.kg\) \{[^}]*padding-top: 12px/);
    expect(css).toMatch(/\.kg-toolbar \{[^}]*padding: 6px 10px/);
    expect(css).toMatch(/\.kg-subbar > \.kg-view-note \{[^}]*white-space: nowrap/);
  });
});

describe("3. the details panel is a drawer", () => {
  it("closed until something is selected; a tree row opens it; Esc closes it and it is remembered", async () => {
    mockKgFetch();
    await mount(<GraphArea projectId="p" actor="ceo" />);
    const work = $('[data-testid="kg-col-tree"]')!.parentElement!;
    expect(work.hasAttribute("data-panel-open")).toBe(false);
    expect($('[data-testid="kg-col-panel"]')?.getAttribute("aria-hidden")).toBe("true");
    expect(($('[data-testid="kg-details-toggle"]') as HTMLButtonElement).disabled).toBe(true);
    await press($$('[data-testid="kg-tree-row"]')[2]);
    expect(work.hasAttribute("data-panel-open")).toBe(true);
    expect($('[data-testid="kg-details-toggle"]')?.getAttribute("aria-expanded")).toBe("true");
    await act(async () => { $('[data-testid="kg-panel"]')!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true })); });
    expect(work.hasAttribute("data-panel-open")).toBe(false);
    expect(window.sessionStorage.getItem(PANEL_KEY)).toBe("closed");
    await press($('[data-testid="kg-details-toggle"]'));
    expect(work.hasAttribute("data-panel-open")).toBe(true);
    expect(window.sessionStorage.getItem(PANEL_KEY)).toBe("open");
    expect(css).toMatch(/\.kg-col-panel \{\s*position: absolute;[^}]*transform: translateX\(105%\)/);
    expect(css).toMatch(/\.kg-work \{[^}]*grid-template-columns: 14\.21053rem minmax\(0, 1fr\);/);
  });

  it("a deep link keeps the drawer closed when this session closed it", async () => {
    window.sessionStorage.setItem(PANEL_KEY, "closed");
    window.history.replaceState(null, "", `/?area=knowledge&kg_node=${FX.hood.center.id}`);
    mockKgFetch();
    await mount(<GraphArea projectId="p" actor="ceo" />);
    expect($('[data-testid="kg-canvas"]')?.getAttribute("data-mode")).toBe("focus");
    expect($('[data-testid="kg-col-tree"]')!.parentElement!.hasAttribute("data-panel-open")).toBe(false);
  });
});

describe("4. the class is the node", () => {
  const CLS_ROW = FX.tree.classes.rows[0];

  it("a class in the tree focuses that class at class level", async () => {
    mockKgFetch({ "/tree/children$": (u) => ({ body: u.searchParams.get("parent") ? { ...FX.tree.classes, parent: u.searchParams.get("parent") } : { ...FX.tree.areas, rows: FX.tree.classes.rows.map((r) => ({ ...r, parent_key: null })) } }) });
    await mount(<GraphArea projectId="p" actor="ceo" />);
    await press($(`[data-key="${CSS.escape(CLS_ROW.key)}"]`));
    expect(calls.some((c) => c.includes("/view/neighbourhood") && c.includes(`node=${CLS_ROW.node_id}`) && c.includes("depth=1"))).toBe(true);
    expect($('[data-testid="kg-canvas"]')?.getAttribute("data-mode")).toBe("focus");
  });

  it("a class's panel lists its methods, each with its own connections, each opening the method view", async () => {
    const methods = FX.tree.methods.rows.slice(0, 3).map((r) => ({ ...FX.detail.node, id: r.node_id!, key: r.key, label: `${r.label}()` }));
    const classDetail = {
      ...FX.detail, node: { ...FX.hood.center },
      connections: [{ kind: "defines", direction: "out", label: "Defines", total: 32, items: methods }, ...FX.detail.connections.slice(0, 1)],
    };
    mockKgFetch({ [`/view/node/${FX.hood.center.id}$`]: { body: classDetail } });
    const onFocusNode = vi.fn();
    await mount(<NodePanel projectId="p" actor="ceo" nodeId={FX.hood.center.id} onFocusNode={onFocusNode} />);
    const members = $('[data-testid="kg-panel-members"]')!;
    expect(members.textContent).toContain("Methods · 32");
    expect(members.querySelectorAll('[data-testid="kg-member"]')).toHaveLength(3);
    expect($('[data-testid="kg-members-more"]')?.textContent).toContain("3 of 32");
    // Not listed twice: the Connections summary leaves "Defines" to Methods.
    expect($('[data-testid="kg-panel-conn-summary"]')?.textContent).not.toContain("Defines");
    await press(members.querySelector('[data-testid="kg-member-toggle"]'));
    expect(members.querySelector('[data-testid="kg-member-conn"]')?.textContent).toContain("Calls 119");
    await press(members.querySelector('[data-testid="kg-member-open"]'));
    expect(onFocusNode).toHaveBeenCalledWith(methods[0].id, methods[0]);
  });
});

describe("5. Python and JavaScript areas say their functions", () => {
  it("never '0 classes' — functions, then packages", () => {
    const area = (counts: Record<string, number>) => ({ id: "a", key: "a", kind: "area", layer: "code" as const, label: "app", trust_tier: "observed" as const, counts, sub: "0 classes" });
    expect(bubbleSub(area({ classes: 0, methods: 24, packages: 3 }))).toBe("24 functions");
    expect(bubbleSub(area({ classes: 0, methods: 0, packages: 3 }))).toBe("3 packages");
    expect(bubbleSub({ ...area({ classes: 0, methods: 7 }), kind: "package" })).toBe("7 functions");
  });

  it("the tree's counts say what they count", () => {
    expect(countWords({ ...FX.tree.areas.rows[0], count: 85 })).toBe("85 packages");
    expect(countWords({ ...FX.tree.classes.rows[0], count: 11 })).toBe("11 members");
  });
});

// ── tester round 2 (build 654adf929) ─────────────────────────────────────────────────────────
import { FOCUS_RING_CAP, groupFocus, placeNodeLabels, pickOverview } from "../lib/kg-layout";
import { RETRY_DELAYS_MS, distinctPaths, kgRead, rankHits } from "../lib/kg-view";
import { dependents } from "../app/knowledge/node-panel";
import { ProjectMenu } from "../app/project-menu";
import { IdentityProvider } from "../components/identity/identity-provider";

describe("tester round 2: never an unlabelled or overlapping node", () => {
  it("every node gets a label where there is room — its shortest form, or another side, when the full one has none", () => {
    const v = [{ w: 200, h: 20 }, { w: 80, h: 20 }, { w: 40, h: 16 }];
    const items = [
      { id: "a", x: 300, y: 100, r: 14, priority: 3, variants: v },
      { id: "b", x: 360, y: 100, r: 14, priority: 2, variants: v },
      { id: "c", x: 420, y: 100, r: 14, priority: 1, variants: v },
    ];
    const chosen = placeNodeLabels(items, [], { x0: 0, y0: 0, x1: 800, y1: 400 });
    expect([...chosen.keys()].sort()).toEqual(["a", "b", "c"]);
    expect(chosen.get("a")!.variant).toBe(0);
    expect(chosen.get("b")!.variant > 0 || chosen.get("b")!.side !== "below").toBe(true);
  });

  it("Focus caps each ring and folds the rest into package groups: 2 hops or 200 read stays readable and deterministic", () => {
    const nodes = Array.from({ length: 120 }, (_, i) => ({
      id: `n${i}`, key: `symbol:r:mod/src/pkg${i % 7}/C${i}.java:C${i}`, kind: "symbol", layer: "code", label: `C${i}`,
      sub: `mod/src/pkg${i % 7}/C${i}.java`, hop: i < 70 ? 1 : 2,
    }));
    const center = { id: "c", key: "c", kind: "symbol", layer: "code", label: "Center", hop: 0 };
    const edges = nodes.map((n, i) => ({ src: "c", dst: n.id, kind: "calls", weight: 200 - i, label: `${200 - i} calls` }));
    const one = groupFocus("c", [center, ...nodes], edges);
    const two = groupFocus("c", [center, ...nodes].reverse(), [...edges].reverse());
    expect(one.nodes.map((n) => n.id).sort()).toEqual(two.nodes.map((n) => n.id).sort());
    const hop1 = one.nodes.filter((n) => (n as { hop: number }).hop === 1);
    expect(hop1.filter((n) => n.kind !== "group")).toHaveLength(FOCUS_RING_CAP[1]);
    const groups = hop1.filter((n) => n.kind === "group");
    expect(groups.length).toBeGreaterThan(0);
    expect(groups.length).toBeLessThanOrEqual(6);
    expect(groups[0].label).toMatch(/^\+\d+ in /);
    // Every folded node is in exactly one group, and its edge moved to the group.
    const members = [...one.members.values()].flat();
    expect(members.length + one.nodes.filter((n) => n.kind !== "group").length).toBe(121);
    expect(one.edges.some((e) => e.dst.startsWith("group:1:"))).toBe(true);
  });

  it("a small canvas draws fewer areas per Whole-repo page (still every one a press away)", () => {
    const o = FX.overview;
    const pick = pickOverview(o.areas, o.layers, o.edges, 0, (f) => ({ ...o.areas[0], id: "more:areas", label: `${f.length} more` }), 6);
    expect(pick.nodes.filter((n) => n.kind === "area" && n.id !== "more:areas")).toHaveLength(6);
    expect(pick.pages).toBe(6);
  });
});

describe("tester round 2: the rest", () => {
  it("a cold console is retried with backoff, then said in words — never identity_unavailable", async () => {
    const saved = [...RETRY_DELAYS_MS];
    RETRY_DELAYS_MS.splice(0, RETRY_DELAYS_MS.length, 1, 1, 1);
    let n = 0;
    vi.stubGlobal("fetch", async () => (n++ < 2
      ? new Response(JSON.stringify({ detail: "identity_unavailable" }), { status: 503 })
      : new Response(JSON.stringify({ ok: 1 }), { status: 200 })));
    expect(await kgRead<{ ok: number }>("/v1/x", {})).toEqual({ ok: true, data: { ok: 1 } });
    expect(n).toBe(3);
    vi.stubGlobal("fetch", async () => new Response(JSON.stringify({ detail: "identity_unavailable" }), { status: 503 }));
    const r = await kgRead("/v1/x", {});
    expect(r.ok).toBe(false);
    expect(!r.ok && r.error).not.toMatch(/identity_unavailable/);
    expect(!r.ok && r.error).toMatch(/starting up/);
    RETRY_DELAYS_MS.splice(0, RETRY_DELAYS_MS.length, ...saved);
  });

  it("blast radius counts every incoming code edge: references, imports, calls, dispatch", () => {
    const g = (kind: string, direction: "in" | "out", total: number) => ({ kind, direction, label: kind, total, items: [] });
    const d = { ...FX.detail, connections: [g("references", "in", 112), g("imports", "in", 53), g("calls", "in", 5), g("calls", "out", 9), g("defines", "in", 1)] } as never;
    expect(dependents(d).map((x) => [x.kind, x.total])).toEqual([["references", 112], ["imports", 53], ["calls", 5]]);
  });

  it("a package picked in the tree is shown on the canvas", async () => {
    mockKgFetch({ "/tree/children$": () => ({ body: { ...FX.tree.packages, parent: null, rows: FX.tree.packages.rows.map((r) => ({ ...r, parent_key: null })) } }) });
    await mount(<GraphArea projectId="p" actor="ceo" />);
    const pkg = FX.tree.packages.rows[0];
    await press($(`[data-key="${CSS.escape(pkg.key)}"]`));
    expect(calls.some((c) => c.includes("/view/neighbourhood") && decodeURIComponent(c).includes(`node=${pkg.key}`))).toBe(true);
  });

  it("Rules review and back keeps the Graph as it was; the tab is in the URL", async () => {
    window.history.replaceState(null, "", `/?area=knowledge&kg_node=${FX.hood.center.id}`);
    mockKgFetch();
    await mount(<GraphArea projectId="p" actor="ceo" />);
    expect($('[data-testid="kg-canvas"]')?.getAttribute("data-mode")).toBe("focus");
    await press($('[data-testid="kg-tab-rules"]'));
    expect(new URLSearchParams(window.location.search).get("kg_tab")).toBe("rules");
    expect($('[data-testid="kg-graph-pane"]')?.hasAttribute("hidden")).toBe(true);
    await press($('[data-testid="kg-tab-graph"]'));
    expect(new URLSearchParams(window.location.search).get("kg_tab")).toBeNull();
    expect($('[data-testid="kg-canvas"]')?.getAttribute("data-mode")).toBe("focus");
    expect($('[data-testid="kg-panel"]')?.getAttribute("data-node")).toBe(FX.hood.center.id);
  });

  it("search keeps each file name and ranks types above modules above members", () => {
    const p = distinctPaths(["fineract-loan/src/main/java/org/apache/fineract/portfolio/loanproduct/domain/LoanProductRelatedDetailRepositoryWrapper.java"]);
    expect(p[0].endsWith(".java")).toBe(true);
    const hit = (label: string, kind: string) => ({ node: { label, symbol_kind: kind }, match: "prefix" });
    expect(rankHits("LoanProduct", [hit("loanproduct", "module"), hit("loanProductX", "method"), hit("LoanProduct", "class")]).map((h) => h.node.label))
      .toEqual(["LoanProduct", "loanproduct", "loanProductX"]);
  });

  it("the project menu closes on a pick, on Esc and on a press outside it", async () => {
    const me = { principal_id: "usr_v", display_name: "V", org: { id: "o", name: "O" }, csrf_token: "c", epoch: 1 } as never;
    const org = { epoch: 1, scope: "org", permissions: ["project:create", "project:delete"], step_up_fresh_until: null, catalogue_fingerprint: "x" } as never;
    mockKgFetch();
    await mount(<IdentityProvider me={me} org={org}><ProjectMenu projects={[{ id: "a", name: "A" }, { id: "b", name: "B" }]} activeId="a" homeId="b" orgId="o" /></IdentityProvider>);
    const menu = $('[data-testid="project-menu"]') as HTMLDetailsElement;
    const open = async () => { menu.open = true; await act(async () => { menu.dispatchEvent(new Event("toggle")); }); };
    await open();
    await act(async () => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })); });
    expect(menu.open).toBe(false);
    await open();
    await act(async () => { document.body.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true })); });
    expect(menu.open).toBe(false);
    await open();
    await act(async () => { ($('[data-testid="project-menu-b"]') as HTMLAnchorElement).dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true })); });
    expect(menu.open).toBe(false);
  });

  it("the drawer never hides a state card; the panel and tree scroll without moving the page", () => {
    expect(css).toMatch(/\.kg-col-canvas > \.kg-stage, \.kg-col-canvas > \.kg-stage-wrap, \.kg-col-canvas > \.kg-canvas-loading \{ right: var\(--kg-occlude, 0px\); \}/);
    expect(css).toMatch(/\.kg-col-panel, \.kg-tree-scroll \{ overscroll-behavior: contain; \}/);
    expect(css).toMatch(/--kg-file: #d6b48a;/);
  });
});
