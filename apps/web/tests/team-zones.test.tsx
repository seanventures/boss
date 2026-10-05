// @vitest-environment happy-dom
/**
 * Teams as soft zones (agents-home spec §4.6, §7, §8.2 team-zones): Live › Agent force.
 * Rendered and asserted on what is drawn: word badges, live faces first, expand in place with
 * Talk, `Open map ↗`, and search dimming. Organization's full-density map is retired; what it
 * did is pinned on Teams and Flow (the-org-view-keeps-what-zones-did.test.tsx, org-view spec §5).
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { board, stubMatchMedia } from "./_helpers/agents-home-fixtures";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { TeamZones } = await import("../app/agents/team-zones");

let root: Root | null = null;
let host: HTMLElement | null = null;

beforeEach(() => {
  stubMatchMedia([]);
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({})));
});
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

async function settle() {
  for (let i = 0; i < 10; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
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
const live = { software_engineer: ["working", "Now: Coding · Build · Ship it #182"],
               qa: ["needs_you", "Waiting on you: Publish"] } as Record<string, [string, string]>;

describe("Live › Agent force (compact)", () => {
  it("draws every team in order with word badges and live faces first", async () => {
    await render(<TeamZones projectId="p1" board={board(live, { attention: [{ id: "dec_1", kind: "approval", role_key: "qa", title: "Publish" }] })} density="compact" />);
    // An empty Yours is not drawn (agents-home walk D17).
    expect($$(".ag-team-zone").map((z) => z.dataset.team)).toEqual(
      ["leadership", "architecture", "engineering", "quality", "operations", "cost", "media", "thinkers"]);
    const eng = $('[data-testid="ag-team-engineering"]')!;
    expect(eng.textContent).toContain("Engineering");
    expect(eng.textContent).toContain("1 working");
    expect($('[data-testid="ag-team-quality"]')!.textContent).toContain("1 needs you");
    // Live faces first: Forge leads Engineering's faces.
    const faces = [...eng.querySelectorAll<HTMLElement>(".ag-team-face")];
    expect(faces[0].dataset.state).toBe("working");
    // An all-idle team is a label, a count and one stacked pile.
    const lead = $('[data-testid="ag-team-leadership"]')!;
    expect(lead.querySelector(".ag-stack")).not.toBeNull();
    expect(lead.querySelectorAll(".ag-team-face")).toHaveLength(0);
    expect(lead.querySelector(".ag-team-label")!.textContent).toBe("Leadership");
    expect($('[data-team="thinkers"] .ag-team-label')!.getAttribute("title")).toBe("Inspired by a way of thinking. Not real people.");
    expect($('[data-team="quality"] .ag-team-label')!.getAttribute("title")).toBe("Quality and security");
    expect($('[data-team="cost"] .ag-team-label')!.getAttribute("title")).toBe("Cost and providers");
  });

  it("expands a team in place: members, their line, Talk, and Open map", async () => {
    const onOpenMap = vi.fn();
    await render(<TeamZones projectId="p1" board={board(live)} density="compact" onOpenMap={onOpenMap} />);
    expect($('[data-testid="ag-team-members-engineering"]')).toBeNull();
    await act(async () => { $('[data-testid="ag-team-engineering"]')!.click(); });
    const list = $('[data-testid="ag-team-members-engineering"]')!;
    expect(list.querySelectorAll(".ag-team-row")).toHaveLength(4);
    expect(list.textContent).toContain("Now: Coding · Build · Ship it #182");
    expect(list.querySelector('[aria-label^="Talk to"]')).not.toBeNull();
    await act(async () => { $('[data-testid="ag-team-map-engineering"]')!.click(); });
    expect(onOpenMap).toHaveBeenCalledWith("engineering");
  });

  it("search dims the faces that do not match", async () => {
    await render(<TeamZones projectId="p1" board={board(live)} density="compact" search="zzz" />);
    expect($('[data-team="engineering"] .ag-team-face')!.dataset.dim).toBe("search");
  });
});
