// @vitest-environment happy-dom
/**
 * Organization's state is in the URL (org-view spec §4.1): `layout`, `days` and `isolate` are
 * read and written; a stored or linked `layout=zones|swarm` lands on Teams / Flow (and the URL
 * is rewritten to say so); `?focus=<team>` shows Teams with that card; `?run=` lands on Flow.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { stubMatchMedia } from "./_helpers/agents-home-fixtures";
import { orgFetch, orgWire } from "./_helpers/org-board-fixtures";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { OrgView, readOrgUrl } = await import("../app/agents/org-view");

let root: Root | null = null;
let host: HTMLElement | null = null;
let stub: ReturnType<typeof orgFetch>;
beforeEach(() => {
  stubMatchMedia([]);
  stub = orgFetch(() => orgWire(), (url) => (url.includes("/chain") ? Response.json({ acts: [] }) : undefined));
  vi.stubGlobal("fetch", vi.fn(stub.fn));
});
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});
async function render(url: string, props: Partial<React.ComponentProps<typeof OrgView>> = {}) {
  window.history.replaceState(null, "", url);
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<OrgView projectId="p1" live={new Map()} lines={() => <div data-testid="lines-stub" />} {...props} />));
  for (let i = 0; i < 12; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}
const $ = (sel: string) => document.querySelector<HTMLElement>(sel);
const q = () => new URL(window.location.href).searchParams;
const layoutShown = () => $('[data-testid="ag-org"]')!.dataset.layout;

describe("the URL is the state", () => {
  it("reads legacy names: zones → Teams, swarm → Flow", () => {
    expect(readOrgUrl("?layout=zones").layout).toBe("teams");
    expect(readOrgUrl("?layout=swarm").layout).toBe("flow");
    expect(readOrgUrl("?layout=lines").layout).toBe("lines");
    expect(readOrgUrl("?layout=nonsense").layout).toBeNull();
    expect(readOrgUrl("?days=30").days).toBe(30);
    expect(readOrgUrl("?days=14").days).toBe(7);
    expect(readOrgUrl("?isolate=quality").isolate).toBe("quality");
  });

  it("a linked layout=swarm lands on Flow and the URL is rewritten", async () => {
    await render("/?area=workforce&view=org&layout=swarm");
    expect(layoutShown()).toBe("flow");
    expect(q().get("layout")).toBe("flow");
  });

  it("a linked layout=zones lands on Teams", async () => {
    await render("/?area=workforce&view=org&layout=zones");
    expect(layoutShown()).toBe("teams");
    expect($('[data-testid="ag-zones"]')).not.toBeNull();
  });

  it("writes layout, days and isolate as they change, and fetches the window", async () => {
    await render("/?area=workforce&view=org");
    expect(layoutShown()).toBe("teams");
    await act(async () => { $('[data-testid="ag-org-days-30"]')!.click(); });
    expect(q().get("days")).toBe("30");
    for (let i = 0; i < 6; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    expect(stub.calls.some((c) => c.url.includes("/v1/agents/org-board") && c.url.includes("days=30"))).toBe(true);
    await act(async () => { $('[data-testid="ag-org-flow"]')!.click(); });
    expect(q().get("layout")).toBe("flow");
    await act(async () => { $('[data-testid="ag-flow-node-quality"]')!.click(); });
    expect(q().get("isolate")).toBe("quality");
    await act(async () => { $('[data-testid="ag-org-lines"]')!.click(); });
    expect(q().get("layout")).toBe("lines");
    expect($('[data-testid="lines-stub"]')).not.toBeNull();
    await act(async () => { $('[data-testid="ag-org-teams"]')!.click(); });
    expect(q().get("layout")).toBeNull();
  });

  it("?focus=<team> shows Teams with that card marked; ?run= lands on Flow", async () => {
    await render("/?area=workforce&view=org&layout=flow&focus=quality", { focusTeam: "quality" });
    expect(layoutShown()).toBe("teams");
    expect($('[data-testid="ag-orgteam-quality"]')!.dataset.focus).toBe("true");
    act(() => root?.unmount());
    host?.remove();
    await render("/?area=workforce&view=org&run=wfr_1", { focusRunId: "wfr_1" });
    expect(layoutShown()).toBe("flow");
  });

  it("the segment and the window are radio groups that move with the arrows", async () => {
    await render("/?area=workforce&view=org");
    const seg = $('[role="radiogroup"][aria-label="Organization view"]')!;
    expect([...seg.querySelectorAll("[role=radio]")].map((b) => b.textContent)).toEqual(["Teams", "Flow", "Lines"]);
    await act(async () => { seg.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true })); });
    expect(layoutShown()).toBe("flow");
    const win = $('[role="radiogroup"][aria-label="Window"]')!;
    expect([...win.querySelectorAll("[role=radio]")].map((b) => b.textContent)).toEqual(["24h", "7d", "30d"]);
    expect($('[data-testid="ag-org-days-7"]')!.getAttribute("aria-checked")).toBe("true");
  });
});
