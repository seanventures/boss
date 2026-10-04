// @vitest-environment happy-dom
/**
 * Tooltips, not paragraphs — on the Agents home surfaces this stream owns (agents-home spec
 * §5.1): Live › Agent force, Organization (Zones, the list), Directory and Activity. Each is
 * RENDERED with fixtures, opened, and walked with the Studio's walker: no run over six words
 * outside a tooltip, no <p>, and none of the Agent World banned words plus this spec's own
 * (`%`, ETA, estimated, mission, office, alive). Text an operator or the API wrote is
 * `data-user-text`.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { overBudget } from "./_helpers/copy-budget";
import { SHIP_IT_MISSION, board, stubMatchMedia } from "./_helpers/agents-home-fixtures";
import type { V2Agent } from "../lib/v2/workforce";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
vi.mock("../app/focused-agents-ui", () => ({ FocusedAgents: () => <div /> }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { TeamZones } = await import("../app/agents/team-zones");
const { OrgView } = await import("../app/agents/org-view");
const { ActivityRiver } = await import("../app/agents/activity-river");
const { DirectoryView } = await import("../app/agents/directory-view");

const SURFACES = [".ag-team-force", ".ag-zones", ".ag-org", ".ag-river", ".ag-dir", ".ag-dir-view"];
const SKIP = "[data-tip], [role='tooltip'], [title], textarea, script, style, option, [hidden], "
  + "[aria-hidden='true'], .ag-sr, [data-user-text]";
const BANNED = /(\b(persona|fingerprint|ledger|act|acts|sentient|alive|conscious|impersonate|signed|tokens?|eta|estimated|mission|office)\b|%)/i;

function visibleRuns(root: Element): string[] {
  const out: string[] = [];
  for (const el of [root, ...root.querySelectorAll("*")]) {
    if (el.closest(SKIP)) continue;
    const own = [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent ?? "").join("").trim();
    if (own) out.push(own);
  }
  return out;
}
function audit() {
  const roots = SURFACES.flatMap((s) => [...document.querySelectorAll(s)]);
  return {
    surfaces: roots.length,
    over: [...new Set(roots.flatMap((r) => overBudget(r)))],
    paragraphs: roots.flatMap((r) => [...r.querySelectorAll("p")].map((p) => p.textContent ?? "")),
    banned: [...new Set(roots.flatMap(visibleRuns).filter((t) => BANNED.test(t)))],
  };
}

let root: Root | null = null;
let host: HTMLElement | null = null;
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
const clickAll = async (sel: string) => {
  for (const b of [...document.querySelectorAll<HTMLElement>(sel)]) await act(async () => { b.click(); });
  await settle();
};
const live = { software_engineer: ["working", "Now: Coding · Build · Ship it #182 and a long line"],
               qa: ["needs_you", "Waiting on you: Publish release video"] } as Record<string, [string, string]>;

describe("the Agents home copy budget", () => {
  it("Live › Agent force, every team opened", async () => {
    stubMatchMedia([]);
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({})));
    await render(<TeamZones projectId="p1" board={board(live)} density="compact" onOpenMap={() => {}} />);
    await clickAll(".ag-team-pill");
    const r = audit();
    expect(r.surfaces).toBe(1);
    expect([r.over, r.paragraphs, r.banned]).toEqual([[], [], []]);
  });

  it("Organization › Zones, an agent selected with its menu open, and a run in focus", async () => {
    stubMatchMedia([]);
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ nodes: [], edges: [] })));
    await render(<OrgView projectId="p1" board={board(live, { missions: [SHIP_IT_MISSION] })} live={new Map()}
                          focusRunId="wfr_1" actor="ceo" lines={<div />} />);
    await act(async () => { document.querySelector<HTMLElement>('[data-testid="ag-zones-agent-qa"]')!.click(); });
    await settle();
    await act(async () => { document.querySelector<HTMLElement>('[data-testid="ag-zones-more"]')!.click(); });
    const r = audit();
    expect(r.surfaces).toBeGreaterThanOrEqual(2);
    expect([r.over, r.paragraphs, r.banned]).toEqual([[], [], []]);
  });

  it("Organization under 640px: the list, opened", async () => {
    stubMatchMedia(["max-width: 639px"]);
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({})));
    await render(<OrgView projectId="p1" board={board(live)} live={new Map()} lines={<div />} />);
    await act(async () => { document.querySelector<HTMLElement>('[data-testid="ag-zones-team-engineering"]')!.click(); });
    const r = audit();
    expect([r.over, r.paragraphs, r.banned]).toEqual([[], [], []]);
  });

  it("Directory", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({})));
    const a = (id: string, over: Partial<V2Agent> = {}) => ({
      id, name: id, role: id, status: "IDLE", task: "", model: null, tokens: null, spend_usd: null, calls: 2,
      autonomy_level: 1, trust_score: 80, successful_runs: 3, violations: 0, hidden_failures: 0,
      last_seen_seconds: null, run_id: null, project: null, last_touched: "Last: Reviewing · Build · 2h", registered: true, ...over,
    }) as V2Agent;
    await render(<DirectoryView projectId="p1" actor="ceo" everyone={[a("qa"), a("pm"), a("ghost", { registered: false })]}
                                retired={[a("old")]} live={new Map()} search="" onCreate={() => {}} />);
    const r = audit();
    expect(r.surfaces).toBeGreaterThanOrEqual(2);
    expect([r.over, r.paragraphs, r.banned]).toEqual([[], [], []]);
  });

  it("Activity, folds open, and its empty state", async () => {
    const now = new Date().toISOString();
    vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
      const url = String(input);
      if (url.includes("/tools")) return Response.json({ items: [{ tool_name: "read_file", ok: true, latency_ms: 30 }], more: true });
      if (url.includes("/v1/agents/activity")) {
        return Response.json({ items: [{ act_id: "a1", role_key: "software_engineer", kind: "step", status: "complete",
          how: "model_pick", edge_label: "Picked by BOSS over 3", label: "Build · Ship it #182 with a long label", verb: "Coding",
          links: { workflow_run_id: "wfr_1" }, children: { count: 2 }, turns: { tool_calls: 3 }, cost_usd: 0.42,
          started_at: now, ended_at: now }], next: "c1" });
      }
      return Response.json({});
    }));
    await render(<ActivityRiver projectId="p1" runId="wfr_1" runLabel="Ship it #182" />);
    await clickAll("[data-testid^='ag-river-tools-'], [data-testid^='ag-river-inside-']");
    let r = audit();
    expect(r.surfaces).toBe(1);
    expect([r.over, r.paragraphs, r.banned]).toEqual([[], [], []]);
    act(() => root?.unmount());
    host?.remove();
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ items: [], next: null })));
    await render(<ActivityRiver projectId="p1" />);
    r = audit();
    expect([r.over, r.paragraphs, r.banned]).toEqual([[], [], []]);
  });
});
