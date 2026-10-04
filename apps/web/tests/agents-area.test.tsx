// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AgentStudioFeed, StudioAgent } from "../lib/agent-studio";
import type { OrgChart, V2Agent, Workforce } from "../lib/v2/workforce";

/**
 * Agents: one area for every agent, keyed by role_key (Workflow Studio spec 2.1).
 *
 * Roster | Team | Panels, one + New agent, and an agent's page that says where it is used —
 * each link opening the workflow editor on that step — and carries its Studio Profile.
 */
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
}));
// On-duty automations load through server actions; this file is about where they are mounted.
vi.mock("../app/focused-agents-ui", () => ({
  FocusedAgents: () => <div data-testid="focused-agents-stub" />,
}));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { WorkforceArea, readAgentsView } = await import("../app/workforce-ui");
const { usedIn } = await import("../app/agent-detail-ui");
const { orphanProfiles, profilesFor } = await import("../app/agent-profile-ui");

const agent = (id: string, over: Partial<V2Agent> = {}): V2Agent => ({
  id, name: id.replace(/_/g, " "), role: id, status: "IDLE", task: "No work in this window.",
  model: null, tokens: null, spend_usd: null, calls: 0, autonomy_level: 1, trust_score: 50,
  successful_runs: 0, violations: 0, hidden_failures: 0, last_seen_seconds: null, run_id: null,
  project: null, last_touched: null, registered: true, ...over,
} as V2Agent);

const workforce: Workforce = {
  project_id: "p1", window_hours: 24, apiReachable: true, handoffs: [],
  counts: { WORKING: 0, STUCK: 0, IDLE: 14, total: 14 },
  agents: Array.from({ length: 14 }, (_, i) => agent(`role_${i}`, i === 0 ? { calls: 3 } : {})),
};
const chart: OrgChart = {
  project_id: "p1", window_hours: 168, apiReachable: true, derived_from: "plan", note: "",
  root_inferred: false, nodes: [], tree: [], roots: [],
} as unknown as OrgChart;

const definition = (key: string, role: string): StudioAgent => ({
  id: `def_${key}`, agent_key: key, name: `${key} profile`, owner: "ceo", status: "active",
  active_run_count: 0,
  active_version: { id: `ver_${key}`, version: 2, role_key: role,
                    model_policy: { execution_mode: "deterministic_brief", selection_mode: "router" },
                    requested_skills: ["model.generate_text"], scopes: { data_boundary: "internal" },
                    validation: { valid: true } },
});
const studio = (over: Partial<AgentStudioFeed> = {}): AgentStudioFeed => ({
  projectId: "p1", apiReachable: true, agents: [definition("qa", "role_1"), definition("orphan", "nobody")],
  panels: [], agentRuns: [], panelRuns: [], tests: [], ...over,
});

let root: Root | null = null;
let host: HTMLElement | null = null;

beforeEach(() => {
  window.history.replaceState(null, "", "/?area=workforce&org=o1");
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
    const url = String(input);
    if (url.startsWith("/api/agents/role_1")) {
      return Response.json({
        role_key: "role_1", name: "QA Lead", registered: false, autonomy_level: null,
        trust_score: null, successful_runs: null, violations: null, hidden_failures: null,
        reports_to: null, spec: {}, owns_stages: [], apps: [], panels: [], assigned_to: [],
        window_hours: 168,
        named_on_steps: [
          { workflow_key: "spd", workflow: "Standard Product Delivery", version: 3, step_id: "review",
            step: "Review", how: "captain", because: "older" },
          { workflow_key: "spd", workflow: "Standard Product Delivery", version: 4, step_id: "review",
            step: "Review", how: "captain", because: "active" },
          { workflow_key: "video", workflow: "Family video", version: 1, step_id: "edit",
            step: "Edit", how: "captain", because: "active" },
        ],
      });
    }
    if (url.includes("/v1/agents/grantable")) return Response.json({ skills: [], mcp_servers: [], agents: [] });
    if (url.includes("/v1/agent-studio/templates")) return Response.json({ items: [] });
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

async function mount(feed: AgentStudioFeed = studio()) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(
    <WorkforceArea workforce={workforce} chart={chart} studio={feed} actor="ceo" />));
  await settle();
}

const $ = (sel: string) => document.querySelector<HTMLElement>(sel);
const click = async (el: HTMLElement | null) => {
  expect(el).not.toBeNull();
  await act(async () => { el!.click(); });
  await settle();
};

describe("the roster", () => {
  it("counts every role, the same number the roles table has", async () => {
    await mount();
    expect($('[data-testid="wf-roster-count"]')?.textContent).toBe("(14)");
    expect($('[data-testid="wf-filter-all"]')?.textContent).toBe("All 14");
    expect($('[data-testid="wf-filter-worked"]')?.textContent).toBe("Has done work 1");
  });

  it("On duty shows the always-on automations in place of the grid", async () => {
    await mount();
    await click($('[data-testid="wf-filter-duty"]'));
    expect($('[data-testid="focused-agents-stub"]')).not.toBeNull();
    expect($('[data-testid="roster-grid"]')).toBeNull();
  });

  it("has no Definition button: the profile is on the agent's page", async () => {
    await mount();
    expect(document.body.innerHTML).not.toContain("wf-definition-");
  });
});

describe("the roster is every agent the step picker offers", () => {
  // The lead's walk: "All 5" on the roster, ~34 in a step's agent picker. The feed's `roster`
  // is the picker's list plus unregistered agents doing work; `agents` is only who worked.
  const worked = agent("cto", { calls: 4, task: "last: review" });
  const idle = agent("architect", { task: "No work in 30 days" });
  const stray = agent("product_manager", { name: "Product Manager", registered: false, calls: 2 });
  const feed: Workforce = { ...workforce, agents: [worked, stray], roster: [worked, stray, idle] };

  async function mountFeed() {
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => root!.render(
      <WorkforceArea workforce={feed} chart={chart} studio={studio()} actor="ceo" />));
    await settle();
  }

  it("lists and counts agents with no work, and says they have none", async () => {
    await mountFeed();
    expect($('[data-testid="wf-filter-all"]')?.textContent).toBe("All 3");
    expect($('[data-testid="wf-agent-architect"]')?.textContent).toContain("No work in 30 days");
    expect($('[data-testid="wf-filter-worked"]')?.textContent).toBe("Has done work 2");
  });

  it("says what unregistered means and shows those agents, each with Register", async () => {
    await mountFeed();
    const note = $('[data-testid="roster-ungoverned"]');
    expect(note?.textContent).toBe("1 unregistered");
    expect(note?.getAttribute("title")).toMatch(/nothing governs it/);
    await click(note);
    expect($('[data-testid="wf-agent-product_manager"]')).not.toBeNull();
    expect($('[data-testid="wf-agent-cto"]')).toBeNull();
    expect($('[data-testid="wf-register-cto"]')).toBeNull();
    await click($('[data-testid="wf-register-product_manager"]'));
    const name = document.querySelector<HTMLInputElement>('input[value="Product Manager"]');
    expect(name).not.toBeNull();
  });
});

describe("Register on a shipped agent registers the shipped agent", () => {
  // The review: Register made a blank operator agent through POST /v1/agents, so a shipped
  // software_engineer came back with no persona, remit or grants. Untouched, the dialog now asks
  // that same endpoint for the shipped definition; anything written makes it the operator's.
  const stray = agent("software_engineer", { name: "Software Engineer", registered: false, calls: 2 });
  const feed: Workforce = { ...workforce, agents: [stray], roster: [stray] };
  const agentPosts = () => (vi.mocked(fetch).mock.calls as [unknown, RequestInit | undefined][])
    .filter(([u, init]) => String(u).endsWith("/v1/agents") && init?.method === "POST")
    .map(([, init]) => JSON.parse(String(init!.body)) as Record<string, unknown>);

  async function register() {
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => root!.render(
      <WorkforceArea workforce={feed} chart={chart} studio={studio()} actor="ceo" />));
    await settle();
    await click($('[data-testid="roster-ungoverned"]'));
    await click($('[data-testid="wf-register-software_engineer"]'));
    expect($('#aq-title')?.textContent).toBe("Register agent");
  }

  it("untouched, it asks POST /v1/agents for the shipped definition", async () => {
    await register();
    await click($('[data-testid="aq-create"]'));
    const sent = agentPosts();
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({ project_id: "p1", name: "Software Engineer",
                                    born_from: { kind: "shipped" } });
    expect(sent[0].persona).toBeUndefined();
  });

  it("with a purpose written, it is the operator's own blank agent", async () => {
    await register();
    const purpose = $('[data-testid="aq-purpose"]') as HTMLTextAreaElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(purpose, "Ours.");
      purpose.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await click($('[data-testid="aq-create"]'));
    expect(agentPosts()[0]).toMatchObject({ purpose: "Ours.", born_from: { kind: "blank" } });
  });
});

describe("Roster | Team | Panels", () => {
  it("reads the view from the URL, and only its own views", () => {
    expect(readAgentsView("?view=team")).toBe("team");
    expect(readAgentsView("?view=panels")).toBe("panels");
    // `view` is shared with the workflow editor (visual|json).
    expect(readAgentsView("?view=json")).toBe("roster");
    expect(readAgentsView("")).toBe("roster");
  });

  it("Team is a view of this area, synced to the URL", async () => {
    await mount();
    await click($('[data-testid="agents-view-team"]'));
    expect(new URLSearchParams(window.location.search).get("view")).toBe("team");
    expect(document.body.innerHTML).toContain("Planned roles");
  });

  it("Panels is hidden until there is a panel", async () => {
    await mount();
    expect($('[data-testid="agents-view-panels"]')).toBeNull();
    act(() => root!.unmount());
    host!.remove();
    await mount(studio({ panels: [{
      id: "pan_1", panel_key: "prd_review", name: "PRD review", purpose: "Decide on a PRD",
      status: "active", active_run_count: 0,
      active_version: { id: "pv1", version: 1, topology: "parallel", members: [], evidence_policy: {},
                        consolidation_policy: {}, dissent_policy: {} },
    }] }));
    await click($('[data-testid="agents-view-panels"]'));
    expect($('[data-testid="agents-panel-prd_review"]')).not.toBeNull();
  });

  it("one + New agent, offering a definition no agent answers to as a template", async () => {
    await mount();
    await click($('[data-testid="wf-new-agent"]'));
    expect($('[data-testid="agent-quick-create"]')).not.toBeNull();
    await click($('[data-testid="aq-tab-template"]'));
    expect($('[data-testid="aq-orphan-orphan"]')).not.toBeNull();
    // A definition whose role has an agent is that agent's Profile, not a template.
    expect($('[data-testid="aq-orphan-qa"]')).toBeNull();
  });
});

describe("an agent's page", () => {
  it("Used in lists each step once, newest version first, linking to the editor on it", () => {
    expect(usedIn([
      { workflow_key: "spd", workflow: "SPD", version: 3, step_id: "review", step: "Review",
        how: "captain", because: "older" },
      { workflow_key: "spd", workflow: "SPD", version: 4, step_id: "review", step: "Review",
        how: "captain", because: "active" },
    ])).toEqual([{ workflow_key: "spd", workflow: "SPD", step_id: "review", step: "Review",
                   href: "?area=delivery_workflows&wf=spd&step=review" }]);
    expect(usedIn(undefined)).toEqual([]);
  });

  it("draws Used in and ONE Profile: one Test (the real one), the definition's compile is a Check", async () => {
    window.history.replaceState(null, "", "/?area=workforce&role=role_1");
    await mount();
    const links = [...document.querySelectorAll<HTMLAnchorElement>('[data-testid="agent-used-in"] a')];
    expect(links.map((a) => a.getAttribute("href"))).toEqual([
      "?area=delivery_workflows&wf=spd&step=review",
      "?area=delivery_workflows&wf=video&step=edit",
    ]);
    const profile = $('[data-testid="agent-profile"]');
    expect(profile?.textContent).toContain("qa profile");
    expect(profile?.textContent).toContain("v2");
    expect($('[data-testid="agent-profile-check-qa"]')?.textContent).toContain("Check");
    // Not two Profiles, not two Tests: the Studio rows sit inside the Passport's Profile section.
    const titles = [...document.querySelectorAll('.section-label[data-level="1"]')].map((t) => t.firstChild?.textContent?.trim());
    expect(titles.filter((t) => t === "Profile")).toHaveLength(1);
    expect(document.querySelectorAll('[data-testid="ag-test"]')).toHaveLength(1);
    expect($("#ag-sec-profile")?.contains(profile!)).toBe(true);
    expect([...document.querySelectorAll("button")].filter((b) => b.textContent?.trim() === "Test")).toHaveLength(0);
  });
});

describe("profiles are keyed by role", () => {
  it("a role's profile is its own; an unclaimed one is a template", () => {
    const feed = studio();
    expect(profilesFor(feed, "role_1").map((a) => a.agent_key)).toEqual(["qa"]);
    expect(orphanProfiles(feed, ["role_1"]).map((a) => a.agent_key)).toEqual(["orphan"]);
  });
});
