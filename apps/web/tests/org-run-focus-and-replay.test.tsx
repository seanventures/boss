// @vitest-environment happy-dom
/**
 * Run focus, replay and the live dot moved from Zones and Swarm to Flow, unchanged in what they
 * promise (org-view spec §4.3, §5):
 *   - `?run=` lands on Flow with a `<run> ×` chip (testid ag-zones-focus) and lights the path
 *     hop by hop, 400ms each; under reduced motion all at once;
 *   - a run the API does not know says `Run not found`, never a guessed path;
 *   - `Replay last run` plays the newest WORKFLOW run's chain; with nothing to replay it says so;
 *   - a live handoff sends one dot along an edge only if that edge is drawn.
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
const animate = vi.fn();
beforeEach(() => {
  toasts.length = 0;
  (Element.prototype as unknown as { animate: unknown }).animate = animate;
  animate.mockClear();
  window.history.replaceState(null, "", "/?area=workforce&view=org&run=wfr_1");
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
const CHAIN = {
  acts: [
    { act_id: "a0", role_key: null, kind: "run", status: "active", label: "Ship it #182", started_at: "2026-10-04T10:00:00Z" },
    { act_id: "a1", role_key: "pm", kind: "step", status: "complete", parent_act_id: "a0", label: "PRD", started_at: "2026-10-04T10:00:01Z" },
    { act_id: "a2", role_key: "software_engineer", kind: "step", status: "complete", parent_act_id: "a1", label: "Build", started_at: "2026-10-04T10:00:05Z" },
    { act_id: "a3", role_key: "qa", kind: "step", status: "active", parent_act_id: "a2", label: "Test", started_at: "2026-10-04T10:00:09Z" },
  ],
};
function api(extra: (url: string) => Response | undefined = () => undefined) {
  return (url: string) => {
    const hit = extra(url);
    if (hit) return hit;
    if (url.startsWith("/v1/workflow-runs/wfr_1/chain")) return Response.json(CHAIN);
    if (url.startsWith("/v1/workflow-runs/")) return Response.json({ detail: "Workflow run not found." }, { status: 404 });
    return undefined;
  };
}
async function render(focusRunId: string | null, live = new Map(), extra?: (url: string) => Response | undefined) {
  stub = orgFetch(() => orgWire(), api(extra));
  vi.stubGlobal("fetch", vi.fn(stub.fn));
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<OrgView projectId="p1" live={live} focusRunId={focusRunId} lines={() => <div />} />));
  await settle();
}
const $ = (sel: string) => document.querySelector<HTMLElement>(sel);
const $$ = (sel: string) => [...document.querySelectorAll<HTMLElement>(sel)];

describe("run focus", () => {
  it("lands on Flow with a clearable chip and lights the path hop by hop", async () => {
    stubMatchMedia([]);
    await render("wfr_1");
    expect($('[data-testid="ag-org"]')!.dataset.layout).toBe("flow");
    expect($('[data-testid="ag-zones-focus"]')!.textContent).toContain("Ship it #182");
    expect($$(".ag-flow-agent[data-lit]").length).toBeLessThan(3);
    await act(async () => { await new Promise((r) => setTimeout(r, 900)); });
    expect($$(".ag-flow-agent[data-lit]").map((a) => a.dataset.role)).toEqual(["pm", "software_engineer", "qa"]);
    expect($$('path.ag-flow-edge[data-level="agent"]').map((p) => p.dataset.edge)).toEqual(
      ["pm>software_engineer", "software_engineer>qa"]);
    // Teams off the path dim.
    expect($('.ag-flow-node[data-team="media"]')!.dataset.dim).toBe("isolate");
  });

  it("under reduced motion the whole path is lit at once, with no animation", async () => {
    stubMatchMedia(["prefers-reduced-motion"]);
    await render("wfr_1");
    expect($$(".ag-flow-agent[data-lit]")).toHaveLength(3);
    expect($$('path.ag-flow-edge[data-level="agent"]')).toHaveLength(2);
    expect(animate).not.toHaveBeenCalled();
    expect(document.querySelector("animateMotion")).toBeNull();
  });

  it("a run the API does not know says so, with no path", async () => {
    stubMatchMedia([]);
    await render("wfr_gone");
    expect($('[data-testid="ag-zones-focus"]')!.textContent).toContain("Run not found");
    expect($$(".ag-flow-agent[data-lit]")).toHaveLength(0);
  });
});

describe("Replay last run", () => {
  it("plays the newest workflow run's chain", async () => {
    stubMatchMedia(["prefers-reduced-motion"]);
    window.history.replaceState(null, "", "/?area=workforce&view=org&layout=flow");
    await render(null, new Map(), (url) => (url.startsWith("/v1/delivery-workflows/runs")
      ? Response.json({ runs: [{ id: "wfr_1" }, { id: "wfr_old" }] }) : undefined));
    await act(async () => { $('[data-testid="ag-replay"]')!.click(); });
    await settle();
    expect(stub.calls.some((c) => c.url === "/v1/delivery-workflows/runs?project_id=p1&limit=5")).toBe(true);
    expect(stub.calls.filter((c) => c.url.includes("/chain")).map((c) => c.url)[0]).toBe("/v1/workflow-runs/wfr_1/chain/load?project_id=p1");
    expect($$(".ag-flow-agent[data-lit]").map((a) => a.dataset.role)).toEqual(["pm", "software_engineer", "qa"]);
  });

  it("with nothing to replay, says so", async () => {
    stubMatchMedia([]);
    window.history.replaceState(null, "", "/?area=workforce&view=org&layout=flow");
    await render(null, new Map(), (url) => (url.startsWith("/v1/delivery-workflows/runs") ? Response.json({ runs: [] }) : undefined));
    await act(async () => { $('[data-testid="ag-replay"]')!.click(); });
    await settle();
    expect(toasts).toContainEqual(["error", "No handoffs to replay"]);
  });
});

describe("the live dot", () => {
  const acts = (url: string) => {
    if (url.startsWith("/v1/acts/act_live")) return Response.json({ act_id: "act_live", role_key: "qa", kind: "step", status: "active", parent_act_id: "act_parent", handed_by_kind: "boss" });
    if (url.startsWith("/v1/acts/act_parent")) return Response.json({ act_id: "act_parent", role_key: "software_engineer", kind: "step" });
    if (url.startsWith("/v1/acts/act_media")) return Response.json({ act_id: "act_media", role_key: "video_editor", kind: "step", status: "active", parent_act_id: "act_pm", handed_by_kind: "boss" });
    if (url.startsWith("/v1/acts/act_pm")) return Response.json({ act_id: "act_pm", role_key: "pm", kind: "step" });
    return undefined;
  };
  it("runs along a drawn edge from the act's real sender, and nowhere else", async () => {
    stubMatchMedia([]);
    window.history.replaceState(null, "", "/?area=workforce&view=org&layout=flow");
    await render(null, new Map(), acts);
    const live = new Map([["qa", { state: "working" as const, actId: "act_live", line: "Testing" }]]);
    await act(async () => root!.render(<OrgView projectId="p1" live={live} lines={() => <div />} />));
    await settle();
    expect($$(".ag-swarm-dot").map((d) => d.getAttribute("data-edge"))).toEqual(["engineering>quality"]);
    // Leadership → Media is not drawn: no dot, only the face's ring changes.
    const live2 = new Map([...live, ["video_editor", { state: "working" as const, actId: "act_media", line: "Editing" }]]);
    await act(async () => root!.render(<OrgView projectId="p1" live={live2} lines={() => <div />} />));
    await settle();
    expect($$(".ag-swarm-dot").map((d) => d.getAttribute("data-edge"))).not.toContain("leadership>media");
  });
});
