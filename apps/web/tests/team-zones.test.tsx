// @vitest-environment happy-dom
/**
 * Teams as soft zones (agents-home spec §4.6, §7, §8.2 team-zones): Live › Agent force and
 * Organization › Zones are one component. Rendered and asserted on what is drawn:
 *   - compact: word badges, live faces first, expand in place with Talk, `Open map ↗`;
 *   - full: selecting a face draws ONLY that agent's observed edges, others dim, Esc clears;
 *   - `?run=` focuses one run's path with a `<run> ×` chip;
 *   - under 640px the map is a drill-down list;
 *   - the hidden table lists every member with their state;
 *   - `⋯ → Move to…` sets the team with the signed-in actor.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SHIP_IT_MISSION, board, stubMatchMedia } from "./_helpers/agents-home-fixtures";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { TeamZones } = await import("../app/agents/team-zones");
const { OrgView } = await import("../app/agents/org-view");

let root: Root | null = null;
let host: HTMLElement | null = null;
let calls: { url: string; init?: RequestInit }[] = [];

beforeEach(() => {
  calls = [];
  stubMatchMedia([]);
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, init });
    if (url.includes("/v1/agents/software_engineer/lineage")) {
      return Response.json({
        nodes: [{ id: "agent:software_engineer", kind: "agent" }, { id: "agent:pm", kind: "agent" },
                { id: "agent:qa", kind: "agent" }],
        edges: [{ from: "agent:pm", to: "agent:software_engineer", acts: 14, how_top: "model_pick", label: "Picked by BOSS ×14" },
                { from: "agent:software_engineer", to: "agent:qa", acts: 3, how_top: "next_stage", label: "Next stage ×3" },
                { from: "person:ceo", to: "agent:software_engineer", acts: 2, how_top: "started", label: "Started by you" }],
      });
    }
    if (url.includes("/v1/agents/qa") && init?.method === "PATCH") return Response.json({ role_key: "qa" });
    return Response.json({});
  }));
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
    await render(<TeamZones projectId="p1" board={board(live)} density="compact" />);
    expect($$(".ag-team-zone").map((z) => z.dataset.team)).toEqual(
      ["leadership", "architecture", "engineering", "quality", "operations", "cost", "media", "thinkers", "yours"]);
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

describe("Organization › Zones (full)", () => {
  it("selecting a face draws only its edges to agents on the map, dims the rest, and Esc clears", async () => {
    await render(<TeamZones projectId="p1" board={board(live)} density="full" />);
    expect($('[data-testid="ag-zones"]')!.dataset.layout).toBe("map");
    expect($$(".ag-zones-edge")).toHaveLength(0);
    await act(async () => { $('[data-testid="ag-zones-agent-software_engineer"]')!.click(); });
    await settle();
    expect(calls.some((c) => c.url.includes("/v1/agents/software_engineer/lineage") && c.url.includes("days=7")
      && c.url.includes("mode=observed"))).toBe(true);
    // Agent-to-agent edges only; a person's edge has no face on this map.
    expect($$(".ag-zones-edge").map((e) => e.dataset.edge).sort()).toEqual(["pm>software_engineer", "software_engineer>qa"]);
    expect($$(".ag-zones-edge title").map((t) => t.textContent)).toContain("Picked by BOSS ×14");
    expect($('[data-testid="ag-zones-agent-pm"]')!.dataset.dim).toBeUndefined();
    expect($('[data-testid="ag-zones-agent-sre"]')!.dataset.dim).toBe("other");
    await act(async () => { window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })); });
    expect($$(".ag-zones-edge")).toHaveLength(0);
    expect($('[data-testid="ag-zones-agent-sre"]')!.dataset.dim).toBeUndefined();
  });

  it("a focused run lights its agents with a clearable chip; others dim", async () => {
    const onClear = vi.fn();
    stubMatchMedia(["prefers-reduced-motion"]);
    await render(<TeamZones projectId="p1" board={board(live, { missions: [SHIP_IT_MISSION] })} density="full"
                            focusRunId="wfr_1" onClearFocus={onClear} />);
    const chip = $('[data-testid="ag-zones-focus"]')!;
    expect(chip.textContent).toContain("Ship it #182");
    expect($$(".ag-zones-agent[data-lit]").map((b) => b.dataset.role)).toEqual(["pm", "chief_architect", "software_engineer"]);
    expect($$(".ag-zones-edge").map((e) => e.dataset.edge)).toEqual(["pm>chief_architect", "chief_architect>software_engineer"]);
    expect($('[data-testid="ag-zones-agent-qa"]')!.dataset.dim).toBe("other");
    await act(async () => { chip.click(); });
    expect(onClear).toHaveBeenCalled();
  });

  it("the faces are one roving tab stop; Enter selects, Shift+Enter opens the agent", async () => {
    await render(<TeamZones projectId="p1" board={board(live)} density="full" />);
    const stops = $$(".ag-zones-agent").filter((b) => b.tabIndex === 0);
    expect(stops).toHaveLength(1);
    stops[0].focus();
    await act(async () => { stops[0].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true })); });
    expect($$(".ag-zones-agent").filter((b) => b.tabIndex === 0)[0]).not.toBe(stops[0]);
    const now = $$(".ag-zones-agent").filter((b) => b.tabIndex === 0)[0];
    await act(async () => { now.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); });
    expect(now.getAttribute("aria-pressed")).toBe("true");
    await act(async () => { now.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", shiftKey: true, bubbles: true })); });
    expect(new URL(window.location.href).searchParams.get("role")).toBe(now.dataset.role);
  });

  it("a hidden table lists every member with their state", async () => {
    await render(<TeamZones projectId="p1" board={board(live)} density="full" />);
    const rows = $$('[data-testid="ag-zones-table"] tbody tr');
    expect(rows).toHaveLength(34);
    const qa = rows.find((r) => r.textContent?.includes("Waiting on you: Publish"))!;
    expect(qa.textContent).toContain("Quality");
    expect(qa.textContent).toContain("Needs you");
  });

  it("Move to… sets the team with the signed-in actor and moves the face", async () => {
    await render(<TeamZones projectId="p1" board={board(live)} density="full" actor="paramg" />);
    await act(async () => { $('[data-testid="ag-zones-agent-qa"]')!.click(); });
    await settle();
    await act(async () => { $('[data-testid="ag-zones-more"]')!.click(); });
    expect($('[role="menu"]')!.textContent).toContain("Move to…");
    await act(async () => { $('[data-testid="ag-zones-move-media"]')!.click(); });
    await settle();
    const patch = calls.find((c) => c.init?.method === "PATCH")!;
    expect(patch.url).toContain("/v1/agents/qa");
    expect(JSON.parse(String(patch.init!.body))).toMatchObject({ team: "media", actor: "paramg", project_id: "p1" });
    expect($('[data-team="media"] [data-testid="ag-zones-agent-qa"]')).not.toBeNull();
  });

  it("under 640px it is a drill-down list: team rows, then members by state", async () => {
    stubMatchMedia(["max-width: 639px"]);
    await render(<TeamZones projectId="p1" board={board(live)} density="full" />);
    expect($('[data-testid="ag-zones"]')!.dataset.layout).toBe("list");
    expect($(".ag-zones-edges")).toBeNull();
    await act(async () => { $('[data-testid="ag-zones-team-engineering"]')!.click(); });
    const forge = $('[data-testid="ag-zones-member-software_engineer"]')!;
    expect(forge.textContent).toMatch(/· Working$/);
    expect($('[data-testid="ag-zones-member-backend_engineer"]')!.textContent).toMatch(/· Idle$/);
  });
});

describe("Organization (the segment)", () => {
  it("Zones | Lines | Swarm: Lines is the shell's chart, unchanged; Swarm is hidden under 640px", async () => {
    await render(<OrgView projectId="p1" board={board(live)} live={new Map()} lines={<div data-testid="lines-stub" />} />);
    expect($$(".ag-org-bar button").map((b) => b.textContent)).toEqual(["Zones", "Lines", "Swarm"]);
    expect($('[data-testid="ag-zones"]')).not.toBeNull();
    await act(async () => { $('[data-testid="ag-org-lines"]')!.click(); });
    expect($('[data-testid="lines-stub"]')).not.toBeNull();
    act(() => root?.unmount());
    host?.remove();
    stubMatchMedia(["max-width: 639px"]);
    await render(<OrgView projectId="p1" board={board(live)} live={new Map()} lines={<div />} />);
    expect($$(".ag-org-bar button").map((b) => b.textContent)).toEqual(["Zones", "Lines"]);
  });
});
