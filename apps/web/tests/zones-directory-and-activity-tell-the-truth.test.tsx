// @vitest-environment happy-dom
/**
 * What the independent walk of Agents home (2026-10-04) found on Organization, Directory and
 * Activity, each pinned as a class:
 *   D4  a gate is the operator's: "You approved …", "Waiting on you: …", never "<captain> working
 *       Gate"; a run that ended badly is Stopped, a step that errored Failed, never Stuck;
 *   D8  a team's "needs you" counts ITEMS waiting on you (one agent, three gates = 3), and says
 *       "1 needs you" / "2 need you";
 *   D13 arrow keys move in two dimensions (nextInGrid, which Organization › Teams uses);
 *   D14 "Has done work" counts agents whose signed work says so, when the feed sends no counts;
 *   D17 Organization never draws "Yours 0" (pinned on Teams; the zone map is retired).
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { board, stubMatchMedia } from "./_helpers/agents-home-fixtures";
import { parseActivity } from "../lib/agents-live-board";
import type { V2Agent } from "../lib/v2/workforce";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { TeamZones, nextInGrid, needYouWords } = await import("../app/agents/team-zones");
const { OrgView } = await import("../app/agents/org-view");
const { sentenceOf, riverState, riverStatusWord } = await import("../app/agents/activity-river");
const { hasDoneWork } = await import("../app/agents/directory-view");

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
async function render(node: React.ReactElement) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(node));
  for (let i = 0; i < 6; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}
const $ = (sel: string) => document.querySelector<HTMLElement>(sel);
const $$ = (sel: string) => [...document.querySelectorAll<HTMLElement>(sel)];

const gate = (id: string, role: string | null) => ({ id, kind: "approval", role_key: role, title: `Publish ${id}` });

describe("D8 a team's needs-you is items, in the right grammar", () => {
  it("draws the server's count of the team's own asks; gates waiting on you never count for a team", async () => {
    // Three gates hold the release manager's work: those are YOUR waits (D4), not Operations'.
    const b = board({ release_manager: ["idle", "Last: Working · Notify"] },
                    { attention: [gate("d1", "release_manager"), gate("d2", "release_manager"), gate("d3", "release_manager")] });
    const ops = b.teams.find((t) => t.key === "operations")!;
    ops.needsYou = 0;
    const quality = b.teams.find((t) => t.key === "quality")!;
    quality.needsYou = 2; // two of Quality's agents' own open asks, as the server counted them
    await render(<TeamZones projectId="p1" board={b} density="compact" />);
    expect($('[data-testid="ag-team-operations"]')!.textContent).not.toMatch(/need/);
    expect($('[data-testid="ag-team-quality"]')!.textContent).toContain("2 need you");
  });
  it("says `1 needs you` for one and `2 need you` for more", () => {
    expect(needYouWords(1)).toBe("1 needs you");
    expect(needYouWords(2)).toBe("2 need you");
  });
});

describe("D17 Organization never draws an empty Yours", () => {
  it("Teams draws the shipped teams in order and no `Yours 0`", async () => {
    const { orgFetch, orgWire } = await import("./_helpers/org-board-fixtures");
    vi.stubGlobal("fetch", vi.fn(orgFetch(() => orgWire()).fn));
    window.history.replaceState(null, "", "/?area=workforce&view=org");
    await render(<OrgView projectId="p1" live={new Map()} lines={() => <div />} />);
    expect($$(".ag-orgteam").map((z) => z.dataset.team)).toEqual(
      ["leadership", "architecture", "engineering", "quality", "operations", "cost", "media", "thinkers"]);
    expect(document.body.textContent).not.toMatch(/Yours\s*0/);
  });
});

describe("D13 arrows move in two dimensions", () => {
  // Two rows: a band of three faces on top, then two zones side by side below.
  const r = (left: number, top: number) => ({ left, top, width: 72, height: 80 });
  const rects = [r(0, 0), r(80, 0), r(160, 0), r(0, 200), r(80, 200), r(400, 200)];
  it("Left/Right stay on the row; Up/Down go to the nearest face above/below", () => {
    expect(nextInGrid(rects, 0, "ArrowRight")).toBe(1);
    expect(nextInGrid(rects, 2, "ArrowRight")).toBe(2); // the row ends; it does not wrap down
    expect(nextInGrid(rects, 1, "ArrowDown")).toBe(4);
    expect(nextInGrid(rects, 5, "ArrowUp")).toBe(2);
    expect(nextInGrid(rects, 3, "ArrowLeft")).toBe(3);
    expect(nextInGrid(rects, 4, "Home")).toBe(0);
    expect(nextInGrid(rects, 0, "End")).toBe(5);
  });
});

describe("D4 a gate is the operator's in Activity", () => {
  const act1 = (over: Record<string, unknown>) => parseActivity({ items: [{
    act_id: "a1", role_key: null, kind: "approval", status: "complete", how: "approved", edge_label: "",
    label: "Notify · Release notes 2.9", verb: "Approved", started_at: "2026-10-04T10:00:00Z", ...over }] }).items[0];
  it("approved and rejected read as you", () => {
    expect(sentenceOf(act1({})).who.name).toBe("You");
    expect(sentenceOf(act1({})).verb).toBe("approved");
    expect(sentenceOf(act1({ verb: "Rejected", status: "error" })).verb).toBe("rejected");
    expect(riverState("error", act1({ verb: "Rejected" }))).toBe("done");
  });
  it("even when a captain is on the row, approving is yours", () => {
    const s = sentenceOf(act1({ role_key: "release_manager", kind: "step" }));
    expect(s.who.name).toBe("You");
    expect(s.who.roleKey).toBeNull();
  });
  it("a wait on a gate is a wait on you", () => {
    const s = sentenceOf(act1({ kind: "step", verb: "Needs you", status: "awaiting_input" }));
    expect(s.verb).toBe("Waiting on you:");
    expect(s.who.variant).toBe("you");
  });
  it("a run that ended badly is Stopped, never Stuck", () => {
    const run = act1({ kind: "run", verb: "Working", status: "error" });
    const state = riverState("error", run);
    expect(state).not.toBe("stuck");
    expect(riverStatusWord(state, run)).toBe("Stopped");
    // A step that errored FAILED; one left open when its run stopped STOPPED. Never stuck.
    expect(riverState("error", act1({ kind: "step", verb: "Failed" }))).toBe("failed");
    expect(riverState("stale", act1({ kind: "step", verb: "Stopped" }))).toBe("stopped");
  });
});

describe("D14 Has done work counts signed work", () => {
  const a = (over: Partial<V2Agent>): V2Agent => ({ id: "pm", name: "PM", role: "pm", status: "IDLE", task: "",
                                                    registered: true, ...over } as V2Agent);
  it("counts calls and steps when the feed sends them", () => {
    expect(hasDoneWork(a({ calls: 3 }))).toBe(true);
  });
  it("with no counts, a Last: line or live work is work done; No work yet is not", () => {
    expect(hasDoneWork(a({ calls: undefined, steps: undefined, task: "Last: Working · Brief · Release notes 2.9 · 8m" }))).toBe(true);
    expect(hasDoneWork(a({ task: "No work yet" }))).toBe(false);
    expect(hasDoneWork(a({ task: "No work yet" }), { state: "working", line: "Now: Coding" })).toBe(true);
  });
});
