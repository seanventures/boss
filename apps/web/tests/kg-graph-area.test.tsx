// @vitest-environment happy-dom

/**
 * THE GRAPH AREA NEVER OPENS UNSTYLED (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §5.1, §5.8).
 *
 * Its three tabs (Graph · Rules review · Settings) each open on a designed body: with no
 * repository the Graph tab is the "Connect a repository" card linking to Repo and models; Rules
 * review is the queue or its empty card; Settings is the knowledge settings moved from
 * Reliability. Deep links pick the tab. Every state is in tests/kg-states.test.tsx.
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useSearchParams: () => null, useRouter: () => ({ refresh: vi.fn(), push: vi.fn() }), usePathname: () => "/" }));

import { GraphArea } from "../app/knowledge/graph-area";
import { mockKgFetch } from "./_helpers/kg-fetch";
import { $, $$, mount, settle, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
beforeEach(() => window.history.replaceState(null, "", "/?area=knowledge"));
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const NO_REPO = { "/knowledge/repositories$": { body: { project_id: "p1", repositories: [] } } };

describe("the Graph area", () => {
  it("opens on Graph; with no repository, the designed way in (never an empty canvas) and the three layers", async () => {
    mockKgFetch(NO_REPO);
    await mount(<GraphArea projectId="p1" actor="ceo" orgId="o1" />);
    const tabs = $$('[role="tab"]').filter((t) => t.closest(".inf-head")).map((t) => t.textContent);
    expect(tabs).toEqual(["Graph", "Rules review", "Settings"]);
    expect($('[data-testid="kg-start"]')?.className).toBe("kg-stage");
    expect($(".kg-empty-title")?.textContent).toBe("Start a knowledge graph for this project");
    expect($$(".kg-layer-name").map((n) => n.textContent)).toEqual(["Code", "Rules", "Learning"]);
    expect($('[data-testid="kg-start-setup"]')?.textContent).toBe("Set up the knowledge graph");
  });

  it("each tab has its own designed body", async () => {
    mockKgFetch({ ...NO_REPO, "/view/rules$": { body: { items: [], counts: { proposed: 0, approved: 0, rejected: 0, superseded: 0 }, scan_id: null } } });
    await mount(<GraphArea projectId="p1" actor="ceo" orgId="o1" />);
    await act(async () => ($('[data-testid="kg-tab-rules"]') as HTMLButtonElement).click());
    await settle();
    expect($('[data-testid="kg-review-empty"] .kg-empty-title')?.textContent).toBe("Nothing waiting for review");
    await act(async () => ($('[data-testid="kg-tab-settings"]') as HTMLButtonElement).click());
    await settle();
    expect($('[data-testid="kg-settings"]')).not.toBeNull();
    expect($('[data-testid="knowledge-retention"]')?.textContent).toContain("one year");
  });

  it("a deep link opens its tab: a scan's rules, or the fact queue", async () => {
    mockKgFetch();
    window.history.replaceState(null, "", "/?area=knowledge&kg_tab=rules&kg_scan=scan_fixture_loan_1");
    await mount(<GraphArea projectId="p1" actor="ceo" />);
    expect($('[data-testid="kg-tab-rules"]')?.getAttribute("aria-selected")).toBe("true");
    expect(($('[data-testid="kg-review-scan"]') as HTMLSelectElement).value).toBe("scan_fixture_loan_1");
    unmount();
    mockKgFetch();
    window.history.replaceState(null, "", "/?area=knowledge&kg_tab=rules&kg_sub=facts");
    await mount(<GraphArea projectId="p1" actor="ceo" />);
    expect($('[data-testid="kg-facts"]')).not.toBeNull();
  });

  it("an in-console link to a node focuses it without a reload (popstate)", async () => {
    mockKgFetch();
    await mount(<GraphArea projectId="p1" actor="ceo" />);
    expect($('[data-testid="kg-canvas"]')?.getAttribute("data-mode")).toBe("overview");
    window.history.pushState(null, "", "/?area=knowledge&kg_node=kgn_f192838785604d53a573");
    await act(async () => { window.dispatchEvent(new PopStateEvent("popstate")); });
    await settle();
    expect($('[data-testid="kg-canvas"]')?.getAttribute("data-mode")).toBe("focus");
    expect($('[data-testid="kg-panel"]')?.getAttribute("data-node")).toBe("kgn_f192838785604d53a573");
  });
});
