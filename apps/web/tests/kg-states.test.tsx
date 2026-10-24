// @vitest-environment happy-dom
/**
 * EVERY STATE OF THE GRAPH AREA IS DESIGNED (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §5.8): the whole
 * area is opened with the fixture routes bent into each state, and each one is a styled card or
 * banner that says what is true and offers the next step — never the P0 placeholder, never a bare
 * spinner, never a blank canvas.
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GraphArea } from "../app/knowledge/graph-area";
import { FX, REPOS, STATUS, calls, mockKgFetch } from "./_helpers/kg-fetch";
import { $, $$, mount, settle, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn() }), usePathname: () => "/", useSearchParams: () => new URLSearchParams(window.location.search) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

beforeEach(() => window.history.replaceState(null, "", "/?area=knowledge"));
afterEach(() => { unmount(); vi.unstubAllGlobals(); vi.useRealTimers(); });

const open = async (props: Partial<React.ComponentProps<typeof GraphArea>> = {}) => {
  await mount(<GraphArea projectId="northpeak-bank-b" actor="ceo" orgId="o1" {...props} />);
  await settle();
};
const text = (id: string) => $(`[data-testid="${id}"]`)?.textContent ?? "";

describe("the Graph area's states", () => {
  it("ready: the toolbar, the tree, the canvas with the whole repo, the legend and the detail panel", async () => {
    mockKgFetch();
    await open();
    expect($('[data-testid="kg-empty-graph"]')).toBeNull();
    expect($('[data-testid="kg-canvas"]')?.getAttribute("data-mode")).toBe("overview");
    expect($('[data-testid="kg-canvas"]')?.getAttribute("data-nodes")).toBe(String(FX.overview.areas.length + FX.overview.layers.length));
    expect($('[data-testid="kg-canvas"]')?.getAttribute("aria-label")).toMatch(/^Whole repository: 36 areas/);
    expect($$('[data-testid="kg-tree-row"]').length).toBeGreaterThan(10);
    expect($$(".kg-legend-item").map((l) => l.textContent)).toEqual(expect.arrayContaining(["Area", "Rules layer", "Learning layer"]));
    expect(text("kg-built")).toMatch(/^Built .* agoRebuild$/);
    expect(text("kg-panel-none")).toMatch(/Select a node/);
    expect(text("kg-head-counts")).toBe("51,319 code nodes");
  });

  it("loading: skeleton tree rows and a shimmer, and past 1.5 s the count it is reading", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let release: () => void = () => {};
    const ready = new Promise<void>((r) => { release = r; });
    mockKgFetch({ "/view/overview$": () => ({ body: FX.overview }) });
    const real = globalThis.fetch;
    vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes("/view/overview")) await ready;
      return real(input, init);
    });
    await open();
    expect($('[data-testid="kg-canvas-loading"] .kg-shimmer')).not.toBeNull();
    expect(text("kg-loading-note")).toBe("Loading the graph");
    await act(async () => { vi.advanceTimersByTime(1600); });
    expect(text("kg-loading-note")).toBe("Reading 51,319 nodes…");
    await act(async () => release());
    await settle();
    expect($('[data-testid="kg-canvas"]')).not.toBeNull();
  });

  it("loading the repositories: the whole area is a skeleton, not a spinner", async () => {
    mockKgFetch();
    vi.stubGlobal("fetch", () => new Promise(() => {}));
    await open();
    expect($('[data-testid="kg-loading"]')?.getAttribute("aria-busy")).toBe("true");
    expect($$('[data-testid="kg-loading"] .kg-skel-row').length).toBeGreaterThan(10);
  });

  it("empty: no repository — the card links to Repo and models", async () => {
    mockKgFetch({ "/knowledge/repositories$": { body: { project_id: "p", repositories: [] } } });
    await open();
    expect($('[data-testid="kg-empty-graph"]')?.className).toBe("kg-stage");
    expect(text("kg-empty-graph")).toContain("Connect a repository to see its graph");
    expect($('[data-testid="kg-empty-graph"] a')?.getAttribute("href")).toBe("/?org=o1&area=setup");
  });

  it("indexing: progress from the repositories, refreshing on its own", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockKgFetch({ "/knowledge/repositories$": { body: { project_id: "p", repositories: [{ ...REPOS.repositories[0], status: "running", files: 6508, parsed: 4380, graph: { nodes: 0, edges: 0 } }] } } });
    await open();
    expect(text("kg-indexing")).toContain("Indexing the repository");
    expect(text("kg-index-list")).toContain("Indexing 4,380 of 6,508 files");
    expect($('[role="progressbar"]')?.getAttribute("aria-valuenow")).toBe("4380");
    const before = calls.filter((c) => c.includes("/knowledge/repositories")).length;
    await act(async () => { vi.advanceTimersByTime(5100); });
    await settle();
    expect(calls.filter((c) => c.includes("/knowledge/repositories")).length).toBeGreaterThan(before);
  });

  it("not indexed (it failed): says why, with Try indexing again", async () => {
    mockKgFetch({ "/knowledge/repositories$": { body: { project_id: "p", repositories: [{ repo: "paramg/x", status: "failed", error: "clone refused: the token cannot read the repository", graph: { nodes: 0, edges: 0 } }] } } });
    await open();
    expect(text("kg-indexing")).toContain("could not be indexed");
    expect(text("kg-indexing")).toContain("clone refused");
    await act(async () => ($('[data-testid="kg-index-start"]') as HTMLButtonElement).click());
    await settle();
    expect(calls.some((c) => c.startsWith("POST /v1/knowledge/repositories/index"))).toBe(true);
  });

  it("empty layer: only Rules on, and no rule yet — the scan prompt", async () => {
    const none = { ...FX.overview, layers: FX.overview.layers.map((l) => (l.id === "layer:rules" ? { ...l, counts: { proposed: 0, approved: 0, rejected: 0, superseded: 0 } } : l)) };
    mockKgFetch({ "/view/overview$": { body: none } });
    await open();
    await act(async () => ($('[data-testid="kg-chip-rules"]') as HTMLButtonElement).click());
    await settle();
    expect($('[data-testid="kg-chip-rules"]')?.getAttribute("aria-pressed")).toBe("true");
    expect($('[data-testid="kg-chip-all"]')?.getAttribute("aria-pressed")).toBe("false");
    expect(calls.some((c) => c.includes("/view/overview") && c.includes("layers=rules"))).toBe(true);
    expect(text("kg-empty-layer")).toContain("No rules yet");
    expect(text("kg-empty-layer")).toContain("Scan an area for business rules");
    await act(async () => ($('[data-testid="kg-empty-layer-scan"]') as HTMLButtonElement).click());
    expect($('[data-testid="kg-chip-all"]')?.getAttribute("aria-pressed")).toBe("true");
    expect(text("kg-notice")).toMatch(/Select an area/);
  });

  it("error: the server's words and Retry; the tree keeps working", async () => {
    mockKgFetch({ "/view/overview$": { status: 503, body: { detail: "The graph view is rebuilding after the index; try again in a few seconds." } } });
    await open();
    expect(text("kg-error")).toContain("The graph view is rebuilding after the index");
    expect($('[data-testid="kg-error"]')?.getAttribute("role")).toBe("alert");
    expect($$('[data-testid="kg-tree-row"]').length).toBeGreaterThan(10);
    mockKgFetch();
    await act(async () => ($('[data-testid="kg-error-retry"]') as HTMLButtonElement).click());
    await settle();
    expect($('[data-testid="kg-error"]')).toBeNull();
    expect($('[data-testid="kg-canvas"]')).not.toBeNull();
  });

  it("stale rollup: the badge says how old, with Rebuild", async () => {
    const built = new Date(Date.now() - 2 * 3600_000).toISOString();
    mockKgFetch({ "/view/overview$": { body: { ...FX.overview, stale: true, built_at: built } } });
    await open();
    expect(text("kg-stale")).toContain("Graph is from the last index (2 h ago)");
    await act(async () => ($('[data-testid="kg-rebuild"]') as HTMLButtonElement).click());
    await settle();
    expect(calls.some((c) => c.startsWith("POST /v1/knowledge/repositories/index"))).toBe(true);
    expect(text("kg-notice")).toContain("Indexing started");
  });

  it("large neighbourhood: the truncation note and Show more (to 300, never past it)", async () => {
    window.history.replaceState(null, "", `/?area=knowledge&kg_node=${FX.hood.center.id}`);
    mockKgFetch();
    await open();
    expect($('[data-testid="kg-canvas"]')?.getAttribute("data-mode")).toBe("focus");
    expect(text("kg-truncated")).toContain(`Showing 80 of ${80 + FX.hood.hidden.nodes} connected classes`);
    await act(async () => ($('[data-testid="kg-show-more"]') as HTMLButtonElement).click());
    await settle();
    expect(calls.some((c) => c.includes("/view/neighbourhood") && c.includes("limit=300"))).toBe(true);
    expect(text("kg-truncated")).toContain("300 is the most one view draws");
    expect($('[data-testid="kg-show-more"]')).toBeNull();
  });

  it("locked: Rules and Learning off with the add-on's reason; the code graph stays", async () => {
    mockKgFetch({ "/graph-status$": { body: { ...STATUS, entitlements: { "knowledge.rules": { allowed: false, reason: "Rules and Learning need the Knowledge add-on · ask your admin" } } } } });
    await open();
    expect($('[data-testid="kg-canvas"]')).not.toBeNull();
    expect($('[data-testid="kg-chip-rules"]')?.getAttribute("aria-disabled")).toBe("true");
    expect($('[data-testid="kg-chip-rules"]')?.getAttribute("title")).toContain("Knowledge add-on");
    await act(async () => ($('[data-testid="kg-tab-rules"]') as HTMLButtonElement).click());
    await settle();
    expect($('[data-testid="kg-locked"]')?.getAttribute("title")).toContain("Knowledge add-on");
    expect($('[data-testid="kg-rule-card"]')).toBeNull();
  });

  it("archived project: a read-only banner; review and rebuild are off with the reason", async () => {
    mockKgFetch();
    await open({ archived: true });
    expect(text("kg-archived")).toContain("This project is archived");
    expect(($('[data-testid="kg-rebuild"]') as HTMLButtonElement).disabled).toBe(true);
    await act(async () => ($('[data-testid="kg-tab-rules"]') as HTMLButtonElement).click());
    await settle();
    const approve = $('[data-testid="kg-rule-approve"]') as HTMLButtonElement;
    expect(approve.disabled).toBe(true);
    expect(approve.title).toMatch(/archived/);
  });

  it("the P0 placeholder is gone wherever there is a graph", async () => {
    mockKgFetch();
    await open();
    expect(document.body.textContent).not.toContain("Connect a repository to see its graph");
    expect(document.body.textContent).not.toContain("These settings are in Reliability for now");
  });
});
