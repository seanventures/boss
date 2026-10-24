// @vitest-environment happy-dom

/**
 * THE GRAPH AREA NEVER OPENS UNSTYLED (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §5.1, §5.8; Phase 0).
 *
 * Phase 0 mounts the area before any route exists: its three tabs each show a designed state
 * (the canvas frame, a card, the three layers named beside their colour), and each state links
 * where its work is done today.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useSearchParams: () => null }));

import { GraphArea } from "../app/knowledge/graph-area";

let host: HTMLDivElement;
let root: Root;
beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

const q = (sel: string) => host.querySelector(sel);

describe("the Graph area", () => {
  it("opens on Graph with the designed empty state and the three layers", () => {
    act(() => root.render(<GraphArea projectId="p1" actor="ceo" orgId="o1" />));
    const tabs = [...host.querySelectorAll('[role="tab"]')].map((t) => t.textContent);
    expect(tabs).toEqual(["Graph", "Rules review", "Settings"]);
    expect(q('[data-testid="kg-empty-graph"]')?.className).toBe("kg-stage");
    expect(q(".kg-empty-title")?.textContent).toBe("Connect a repository to see its graph");
    const layers = [...host.querySelectorAll(".kg-layer-name")].map((n) => n.textContent);
    expect(layers).toEqual(["Code", "Rules", "Learning"]);
    expect(q('[data-testid="kg-empty-graph"] a')?.getAttribute("href")).toBe("/?org=o1&area=setup");
  });

  it("each tab has its own designed state", () => {
    act(() => root.render(<GraphArea projectId="p1" actor="ceo" orgId="o1" />));
    act(() => (q('[data-testid="kg-tab-rules"]') as HTMLButtonElement).click());
    expect(q('[data-testid="kg-empty-rules"] .kg-empty-title')?.textContent).toBe("No rules yet");
    act(() => (q('[data-testid="kg-tab-settings"]') as HTMLButtonElement).click());
    expect(q('[data-testid="kg-empty-settings"] a')?.getAttribute("href")).toBe("/?org=o1&area=reliability");
  });
});
