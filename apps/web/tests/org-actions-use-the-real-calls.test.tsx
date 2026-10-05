// @vitest-environment happy-dom
/**
 * Every Organization action goes through the call the rest of the console makes (org-view spec
 * §4.6), as the signed-in actor:
 *   - `Give` → the Kit's holders, set semantics: the current holders plus this agent;
 *   - `Give recommended` → a confirm, then the Kit's recommended grant for the team's agents;
 *   - `Open room` → at most 5 seats, live agents first;
 *   - `Make agent` (a Missing seat) → + New agent, named and placed on that team;
 *   - Lines › `Plan it` → PUT /api/org; a refusal shows the API's own sentence.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { stubMatchMedia } from "./_helpers/agents-home-fixtures";
import { orgFetch, orgWire } from "./_helpers/org-board-fixtures";
import type { OrgChart } from "../lib/v2/workforce";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const toasts: [string, string][] = [];
vi.mock("../lib/agent-world", async (orig) => {
  const real = await orig<typeof import("../lib/agent-world")>();
  return { ...real, toast: (type: string, msg: string) => { toasts.push([type, msg]); } };
});
const rooms: unknown[] = [];
vi.mock("../lib/converse-doors", async (orig) => {
  const real = await orig<typeof import("../lib/converse-doors")>();
  return { ...real, openRoom: (o: unknown) => { rooms.push(o); } };
});

const { OrgView } = await import("../app/agents/org-view");
const { OrgChartArea } = await import("../app/workforce-ui");
const { parseOrgBoard } = await import("../lib/agents-org-board");

let root: Root | null = null;
let host: HTMLElement | null = null;
let stub: ReturnType<typeof orgFetch>;
beforeEach(() => {
  toasts.length = 0;
  rooms.length = 0;
  stubMatchMedia([]);
  window.history.replaceState(null, "", "/?area=workforce&view=org");
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
async function mount(node: React.ReactElement) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(node));
  await settle();
}
const $ = (sel: string) => document.querySelector<HTMLElement>(sel);
const click = async (sel: string) => { await act(async () => { $(sel)!.click(); }); await settle(); };
const body = (c: { init?: RequestInit }) => JSON.parse(String(c.init?.body ?? "{}"));

const GAPS = { teams: { engineering: { gaps: {
  abilities: [{ ability: "tests_ci", label: "Run tests", roles: ["software_engineer", "backend_engineer"] },
              { ability: "deploy", label: "Deploy", roles: ["engineering_captain"], needs_setup: true }],
  step_gaps: [], missing_roles: 0, kit_ok: true, kit_reason: null } } } };

describe("Organization's actions", () => {
  it("Give adds the agent to the ability's current holders and says so", async () => {
    stub = orgFetch(() => orgWire(GAPS), (url, init) => {
      if (url.startsWith("/api/kit?") && url.includes("ability=tests_ci")) return Response.json({ key: "tests_ci", holders: ["qa"] });
      if (url === "/api/kit" && init?.method === "POST") return Response.json({ key: "tests_ci", holders: ["qa", "backend_engineer"] });
      return undefined;
    });
    vi.stubGlobal("fetch", vi.fn(stub.fn));
    await mount(<OrgView projectId="p1" live={new Map()} actor="paramg" lines={() => <div />} />);
    await click('[data-testid="ag-orgteam-give-engineering"]');
    await click('[data-testid="ag-orgteam-give-tests_ci-backend_engineer"]');
    const post = stub.calls.find((c) => c.url === "/api/kit" && c.init?.method === "POST")!;
    expect(body(post)).toMatchObject({ action: "holders", project_id: "p1", actor: "paramg", ability: "tests_ci",
                                       roles: ["qa", "backend_engineer"] });
    expect(toasts[0][0]).toBe("success");
    expect(toasts[0][1]).toMatch(/^Gave Run tests to /);
  });

  it("Give recommended confirms, then grants for the team's agents only", async () => {
    stub = orgFetch(() => orgWire(GAPS), (url, init) => (url === "/api/kit" && init?.method === "POST"
      ? Response.json({ granted: [{ role_key: "software_engineer", ability: "tests_ci" }, { role_key: "backend_engineer", ability: "tests_ci" }], skipped: [] })
      : undefined));
    vi.stubGlobal("fetch", vi.fn(stub.fn));
    await mount(<OrgView projectId="p1" live={new Map()} actor="paramg" lines={() => <div />} />);
    await click('[data-testid="ag-orgteam-more-engineering"]');
    await click('[data-testid="ag-orgteam-give-recommended-engineering"]');
    expect($('[role="menu"]')!.textContent).toContain("Give 1 ability to 2 agents");
    expect(stub.calls.some((c) => c.url === "/api/kit")).toBe(false);
    await click('[data-testid="ag-orgteam-give-recommended-yes-engineering"]');
    const post = stub.calls.find((c) => c.url === "/api/kit")!;
    // Only what will actually be given: Deploy must be set up first, so its agent is not asked.
    expect(body(post)).toMatchObject({ action: "recommended", actor: "paramg",
      roles: ["software_engineer", "backend_engineer"] });
    expect(toasts).toContainEqual(["success", "Gave 2 abilities"]);
  });

  it("Open room seats at most 5, live agents first", async () => {
    stub = orgFetch(() => orgWire());
    vi.stubGlobal("fetch", vi.fn(stub.fn));
    const live = new Map([["release_manager", { state: "working" as const, line: "Now: Notify" }]]);
    await mount(<OrgView projectId="p1" live={live} actor="paramg" lines={() => <div />} />);
    await click('[data-testid="ag-orgteam-more-operations"]');
    await click('[data-testid="ag-orgteam-room-operations"]');
    const seats = (rooms[0] as { seats: string[] }).seats;
    expect(seats).toHaveLength(5);
    expect(seats[0]).toBe("release_manager");
  });

  it("Make agent opens + New agent named for the missing role, on its team", async () => {
    stub = orgFetch(() => orgWire({ missing_roles: [{ role_key: "data_engineer", team: "engineering",
      steps: [{ workflow_key: "ship_it", workflow_name: "Ship it", step_id: "load", step_title: "Load" }] }] }));
    vi.stubGlobal("fetch", vi.fn(stub.fn));
    const onNewAgent = vi.fn();
    await mount(<OrgView projectId="p1" live={new Map()} actor="paramg" onNewAgent={onNewAgent} lines={() => <div />} />);
    await click('[data-testid="ag-orgteam-missing-data_engineer"]');
    expect($('[role="menu"] a')!.getAttribute("href")).toContain("wf=ship_it");
    await click('[data-testid="ag-orgteam-make-data_engineer"]');
    expect(onNewAgent).toHaveBeenCalledWith({ name: "Data engineer", team: "engineering" });
  });

  it("Plan it saves through Lines and shows the API's sentence on a refusal", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, init });
      if (url === "/api/org" && init?.method === "PUT") {
        return Response.json({ detail: "Helm already sits above Forge, so this would make a loop." }, { status: 409 });
      }
      return Response.json({});
    }));
    const drift = parseOrgBoard(orgWire({ declared_vs_observed: { lines: 2, planned_unused: [{ agent: "qa", reports_to: "cto" }],
      unplanned_pairs: [{ a: "pm", b: "software_engineer", handoffs: 31, parent: "pm", child: "software_engineer", parent_is: "manager" }] } })).drift;
    const chart = { project_id: "p1", window_hours: 168, apiReachable: true, derived_from: "", note: "", root_inferred: false,
                    nodes: [], tree: [], roots: [], declared: { qa: { reports_to: "cto" }, cto: { reports_to: null } } } as unknown as OrgChart;
    await mount(<OrgChartArea projectId="p1" actor="paramg" chart={chart} drift={drift} />);
    const strip = $('[data-testid="org-drift"]')!;
    expect(strip.textContent).toContain("Planned, not used: 1");
    expect(strip.textContent).toContain("no handoffs in 30d");
    expect(strip.textContent).toContain("Works together, not planned: 1");
    expect(strip.textContent).toContain("not on the plan");
    expect(($('[data-testid="org-drift-parent-pm-software_engineer"]') as HTMLSelectElement).value).toBe("pm");
    await click('[data-testid="org-drift-plan-pm-software_engineer"]');
    const put = calls.find((c) => c.url === "/api/org" && c.init?.method === "PUT")!;
    expect(body(put)).toMatchObject({ agent_id: "software_engineer", reports_to: "pm", actor: "paramg", project_id: "p1" });
    expect($('[data-testid="org-note"]')!.textContent).toBe("Helm already sits above Forge, so this would make a loop.");
  });
});
