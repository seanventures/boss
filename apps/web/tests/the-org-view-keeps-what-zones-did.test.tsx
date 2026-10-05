// @vitest-environment happy-dom
/**
 * Organization keeps everything Zones did (org-view spec §5, the parity table), now on Teams:
 *   - pressing a face opens the Agent panel with its partners, from the board's agent edges;
 *   - Shift+Enter (and Shift+click) opens the Passport;
 *   - one roving 2-D tab stop across every card (nextInGrid), Enter selects;
 *   - Esc closes a menu, then the drawer, then the selection;
 *   - search dims non-matching faces to 30%;
 *   - `Move to…` calls updateAgent with the signed-in actor and toasts `Moved to Media`;
 *   - the hidden `Team · Agent · State · Now` table;
 *   - under 640px the drill-down list, with `ag-zones-member-*`;
 *   - `ag-zones` stays on the Teams grid (console_contract.py presses for it).
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { stubMatchMedia } from "./_helpers/agents-home-fixtures";
import { orgFetch, orgWire } from "./_helpers/org-board-fixtures";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const toasts: [string, string][] = [];
vi.mock("../lib/agent-world", async (orig) => {
  const real = await orig<typeof import("../lib/agent-world")>();
  return { ...real, toast: (type: string, msg: string) => { toasts.push([type, msg]); } };
});

const { OrgView } = await import("../app/agents/org-view");

let root: Root | null = null;
let host: HTMLElement | null = null;
let stub: ReturnType<typeof orgFetch>;

beforeEach(() => {
  toasts.length = 0;
  stubMatchMedia([]);
  window.history.replaceState(null, "", "/?area=workforce&view=org");
  stub = orgFetch(() => orgWire(), (url, init) => (url.includes("/v1/agents/qa") && init?.method === "PATCH"
    ? Response.json({ role_key: "qa" }) : undefined));
  vi.stubGlobal("fetch", vi.fn(stub.fn));
});
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

async function settle() {
  for (let i = 0; i < 12; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}
async function render(node: React.ReactElement) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(node));
  await settle();
}
const $ = (sel: string) => document.querySelector<HTMLElement>(sel);
const $$ = (sel: string) => [...document.querySelectorAll<HTMLElement>(sel)];
const live = new Map([["software_engineer", { state: "working" as const, line: "Now: Coding · Build · Ship it #182" }],
                      ["qa", { state: "needs_you" as const, line: "Waiting on you: Publish" }]]);
const view = (over: Partial<React.ComponentProps<typeof OrgView>> = {}) => (
  <OrgView projectId="p1" live={live} actor="paramg" lines={() => <div data-testid="lines-stub" />} {...over} />
);

describe("Teams keeps Zones' faces", () => {
  it("draws every team on the Teams grid, live faces first, with ag-zones on it", async () => {
    await render(view());
    expect($('[data-testid="ag-zones"]')).not.toBeNull();
    expect($('[data-testid="ag-orgteams"]')).not.toBeNull();
    expect($$(".ag-orgteam").map((c) => c.dataset.team)).toEqual(
      ["leadership", "architecture", "engineering", "quality", "operations", "cost", "media", "thinkers"]);
    const eng = $$('[data-testid="ag-orgteam-engineering"] .ag-orgteam-agent');
    expect(eng[0].dataset.role).toBe("software_engineer");
    expect($('[data-testid="ag-orgteam-engineering"]')!.textContent).toContain("1 working");
  });

  it("pressing a face opens the Agent panel with its partners from the board's edges", async () => {
    await render(view());
    await act(async () => { $('[data-testid="ag-zones-agent-software_engineer"]')!.click(); });
    await settle();
    const panel = $('[data-testid="ag-org-agent"]')!;
    expect(panel).not.toBeNull();
    expect(panel.textContent).toContain("Gets from");
    expect(panel.textContent).toContain("Hands to");
    expect(panel.textContent).toContain("×18");
    expect(panel.textContent).toContain("×19");
    expect($('[data-testid="ag-zones-agent-software_engineer"]')!.getAttribute("aria-pressed")).toBe("true");
  });

  it("one roving tab stop across cards; Enter selects, Shift+Enter opens the Passport", async () => {
    await render(view());
    const stops = $$(".ag-orgteam-agent").filter((b) => b.tabIndex === 0);
    expect(stops).toHaveLength(1);
    stops[0].focus();
    await act(async () => { stops[0].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true })); });
    const now = $$(".ag-orgteam-agent").filter((b) => b.tabIndex === 0)[0];
    expect(now).not.toBe(stops[0]);
    await act(async () => { now.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); });
    expect(now.getAttribute("aria-pressed")).toBe("true");
    await act(async () => { now.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", shiftKey: true, bubbles: true })); });
    expect(new URL(window.location.href).searchParams.get("role")).toBe(now.dataset.role);
  });

  it("Esc closes the menu, then the drawer, then the selection", async () => {
    await render(view());
    await act(async () => { $('[data-testid="ag-zones-agent-qa"]')!.click(); });
    await settle();
    await act(async () => { $('[data-testid="ag-zones-more"]')!.click(); });
    expect($('[role="menu"]')).not.toBeNull();
    const esc = async () => act(async () => { window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })); });
    await esc();
    expect($('[role="menu"]')).toBeNull();
    expect($('[data-testid="ag-org-agent"]')).not.toBeNull();
    await esc();
    expect($('[data-testid="ag-org-agent"]')).toBeNull();
    expect($('[data-testid="ag-zones-agent-qa"]')!.dataset.selected).toBe("true");
    await esc();
    expect($('[data-testid="ag-zones-agent-qa"]')!.dataset.selected).toBeUndefined();
  });

  it("search dims the faces that do not match", async () => {
    await render(view({ search: "zzz" }));
    expect($('[data-testid="ag-zones-agent-software_engineer"]')!.dataset.dim).toBe("search");
  });

  it("Move to… sets the team with the signed-in actor and says so", async () => {
    await render(view());
    await act(async () => { $('[data-testid="ag-zones-agent-qa"]')!.click(); });
    await settle();
    await act(async () => { $('[data-testid="ag-zones-more"]')!.click(); });
    expect($('[role="menu"]')!.getAttribute("aria-label")).toBe("Move to…");
    expect(($('[data-testid="ag-zones-move-quality"]') as HTMLButtonElement).disabled).toBe(true);
    await act(async () => { $('[data-testid="ag-zones-move-media"]')!.click(); });
    await settle();
    const patch = stub.calls.find((c) => c.init?.method === "PATCH")!;
    expect(patch.url).toContain("/v1/agents/qa");
    expect(JSON.parse(String(patch.init!.body))).toMatchObject({ team: "media", actor: "paramg", project_id: "p1" });
    expect(toasts).toContainEqual(["success", "Moved to Media"]);
    // The board is asked again, past the server's cache.
    expect(stub.calls.some((c) => c.url.includes("/v1/agents/org-board") && c.url.includes("fresh=1"))).toBe(true);
  });

  it("a hidden table lists every member with their state", async () => {
    await render(view());
    const rows = $$('[data-testid="ag-zones-table"] tbody tr');
    expect(rows).toHaveLength(34);
    const qa = rows.find((r) => r.textContent?.includes("Waiting on you: Publish"))!;
    expect(qa.textContent).toContain("Quality");
    expect(qa.textContent).toContain("Needs you");
  });

  it("under 640px Teams is the drill-down list, with a stats line and the member testids", async () => {
    stubMatchMedia(["max-width: 639px"]);
    stub = orgFetch(() => orgWire({ teams: { engineering: { work: { done: 23, failed: 2, open: 3 },
      waiting: [{ role_key: "software_engineer", act_id: "a1", status: "active", label: "Build · Ship it #182",
                  since: new Date(Date.now() - 72 * 60_000).toISOString(), run_id: "wfr_1", attention_id: null }],
      waiting_total: 1 } } }));
    vi.stubGlobal("fetch", vi.fn(stub.fn));
    await render(view());
    expect($('[data-testid="ag-zones"]')!.dataset.layout).toBe("list");
    const head = $('[data-testid="ag-zones-team-engineering"]')!;
    expect(head.textContent).toContain("Done 23 · Waiting 1h 12m");
    await act(async () => { head.click(); });
    const forge = $('[data-testid="ag-zones-member-software_engineer"]')!;
    expect(forge.textContent).toMatch(/· Working$/);
    expect($('[data-testid="ag-zones-member-backend_engineer"]')!.textContent).toMatch(/· Idle$/);
    await act(async () => { forge.click(); });
    await settle();
    expect($('[data-testid="ag-org-agent"]')).not.toBeNull();
  });
});
