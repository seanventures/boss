// @vitest-environment happy-dom
/**
 * A KNOWLEDGE-GRAPH PROJECT, FROM CREATION TO THE GRAPH (BOSSAPP-768 project flow).
 *
 * The operator: "there's no project type… people without context can't assume it is a knowledge
 * graph… we need a proper UI flow to create the knowledge graph and how it is associated with
 * Agents for a particular project." So: New project asks what the project is for; a knowledge-graph
 * project opens on Knowledge Graph › Setup — ① Import code ② Assign the team ③ First scan ④ Review
 * rules → Done — every step's state read from GET /v1/knowledge/setup (resumable); the area is
 * named Knowledge Graph, says what it is on a first visit, and a project without a graph gets a
 * designed way in; the project home carries the lifecycle card. Each state is rendered here.
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useSearchParams: () => null, useRouter: () => ({ refresh: vi.fn(), push: vi.fn() }), usePathname: () => "/" }));

import type { KgSetupResponse } from "@boss/api-client";
import { GraphArea } from "../app/knowledge/graph-area";
import { KgLifecycleCard } from "../app/knowledge/kg-lifecycle";
import { PROJECT_TYPES, knowledgeSetupHref, projectTypeBadge } from "../app/project-menu";
import { blockedBy, hasIndexedCode, setupOpens } from "../lib/kg-setup";
import { calls, mockKgFetch } from "./_helpers/kg-fetch";
import { $, $$, mount, settle, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
beforeEach(() => {
  window.history.replaceState(null, "", "/?org=o1&project_id=fineract-map&area=knowledge");
  try { window.localStorage.clear(); } catch { /* none */ }
});
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const CANDIDATES = [
  { role_key: "chief_architect", display_name: "Chief Architect", job: "scans", does: "Scans an area of the code for business rules", recommended: true, mission: "Decide the structure", trust: 50, runs: 12, autonomy: 1, passport: "283e9d57f6a5" },
  { role_key: "software_engineer", display_name: "Software Engineer", job: "changes", does: "Makes changes to the code", recommended: true, mission: "Implement small changes", trust: 50, runs: 40, autonomy: 1, passport: "c2eaf23001267" },
  { role_key: "sre", display_name: "Site Reliability Engineer", job: "incidents", does: "Investigates incidents with the graph", recommended: true, mission: "Keep software recoverable", trust: 50, runs: 3, autonomy: 1, passport: "165c17b48ee6" },
  { role_key: "qa", display_name: "QA Lead", job: null, does: null, recommended: false, mission: "Prove it works", trust: 50, runs: 9, autonomy: 1, passport: null },
];

type Over = Partial<Omit<KgSetupResponse, "team">> & { team?: Partial<KgSetupResponse["team"]> };

function setupFx(over: Over = {}): KgSetupResponse {
  const base: KgSetupResponse = {
    project_id: "fineract-map", name: "Fineract map", project_type: "knowledge_graph", archived: false, has_graph: false,
    steps: [
      { key: "import", state: "current", done: false, summary: "No repository yet" },
      { key: "team", state: "waiting", done: false, summary: "Not chosen yet: BOSS's default team works until you choose" },
      { key: "scan", state: "waiting", done: false, summary: "No scan yet" },
      { key: "review", state: "waiting", done: false, summary: "0 waiting · 0 approved · 0 rejected" },
    ],
    next: "import", done_count: 0, repositories: [], code_nodes: 0,
    team: { chosen: false, role_keys: ["software_engineer", "qa"], candidates: CANDIDATES },
    scans: { count: 0, running: 0, completed: 0, latest: null },
    scan_defaults: { max_files: 60, max_rules: 25, budget_usd: 2 },
    areas: [], rules: { proposed: 0, approved: 0, rejected: 0 }, learnings: 0,
  };
  return { ...base, ...over, team: { ...base.team, ...(over.team ?? {}) } } as KgSetupResponse;
}

const INDEXED_REPO = { repo: "acme/fineract", branch: "develop", status: "indexed", summary: "51,319 nodes, 234,187 edges from 6,508 files", files: 6508, parsed: 6508, graph: { nodes: 51319, edges: 234187 } };
const AREAS = [
  { key: "area:acme/fineract:fineract-loan", label: "fineract-loan", repo: "acme/fineract", classes: 869, packages: 85 },
  { key: "area:acme/fineract:fineract-core", label: "fineract-core", repo: "acme/fineract", classes: 946, packages: 232 },
];
const PROJECT_REPOS = { project_id: "fineract-map", repo_mode: "single", items: [], registry_in_use: true, not_allowed_count: 2,
  addable: [{ id: "ghr_1", full_name: "acme/fineract", language: "Java" }] };

function routes(setup: KgSetupResponse, extra: Record<string, unknown> = {}) {
  return {
    "/v1/knowledge/setup$": { body: setup },
    "/v1/projects/[^/]+/repos/ghr_1$": { body: { ...PROJECT_REPOS, items: [{ binding_id: "b1", full_name: "acme/fineract", state: "working" }], addable: [] } },
    "/v1/projects/[^/]+/repos$": { body: PROJECT_REPOS },
    "/api/project-agents$": { body: { ok: true, team: ["chief_architect", "sre"] } },
    ...extra,
  } as Parameters<typeof mockKgFetch>[0];
}

const open = async (props: Partial<React.ComponentProps<typeof GraphArea>> = {}) => {
  await mount(<GraphArea projectId="fineract-map" actor="ceo" orgId="o1" projectType="knowledge_graph" {...props} />);
  await settle();
};
const tabs = () => $$('[role="tab"]').filter((t) => t.closest(".inf-head")).map((t) => t.textContent?.replace(/\d+$/, ""));
const state = (key: string) => $(`[data-testid="kg-step-${key}"]`)?.getAttribute("data-state");
const click = async (testId: string) => { await act(async () => ($(`[data-testid="${testId}"]`) as HTMLElement).click()); await settle(); };

describe("New project asks what the project is for", () => {
  it("offers two types, each with one line, and a knowledge-graph project opens on its setup", () => {
    expect(PROJECT_TYPES.map((t) => t.label)).toEqual(["Build an app", "Knowledge Graph for an existing codebase"]);
    expect(PROJECT_TYPES.every((t) => t.line.length > 20 && !t.line.includes("\n"))).toBe(true);
    const href = new URLSearchParams(knowledgeSetupHref("fineract-map", "boss-dogfood").slice(1));
    expect([href.get("project_id"), href.get("area"), href.get("kg_tab"), href.get("org")]).toEqual(["fineract-map", "knowledge", "setup", "o1"]);
    expect(projectTypeBadge("knowledge_graph")).toBe("Knowledge graph");
    expect(projectTypeBadge("app")).toBeNull();
  });
});

describe("Knowledge Graph › Setup", () => {
  it("a knowledge-graph project with no code opens on Setup: ① current, the rest waiting with why", async () => {
    mockKgFetch(routes(setupFx(), { "/knowledge/repositories$": { body: { project_id: "fineract-map", repositories: [] } } }));
    await open();
    expect(tabs()).toEqual(["Setup", "Graph", "Rules review", "Settings"]);
    expect($('[data-testid="kg-tab-setup"]')?.getAttribute("aria-selected")).toBe("true");
    expect($('[data-testid="kg-head"] h2')?.textContent).toBe("Knowledge Graph");
    expect($(".kg-setup-title")?.textContent).toBe("Set up the knowledge graph for Fineract map");
    expect($('[data-testid="kg-setup-progress"]')?.textContent).toBe("0 of 4 steps done");
    expect([state("import"), state("team"), state("scan"), state("review")]).toEqual(["current", "ready", "waiting", "waiting"]);
    expect($('[data-testid="kg-step-scan-why"]')?.textContent).toBe("Needs indexed code: import a repository first.");
    // ① is open: the allowed repositories to add, and where to allow more.
    const pick = $('[data-testid="kg-import-pick"]') as HTMLSelectElement;
    expect([...pick.options].map((o) => o.textContent)).toEqual(["Choose a repository…", "acme/fineract · Java"]);
    expect($('[data-testid="kg-import-registry"]')?.textContent).toBe("Repo and models");
    expect($('[data-testid="kg-import"]')?.textContent).toContain("2 more there are not allowed yet");
    expect(($('[data-testid="kg-setup-open-graph"]') as HTMLButtonElement).disabled).toBe(true);
    expect(setupOpens(setupFx())).toBe(true);
  });

  it("① adds a repository through the project's own Repositories door, and follows its indexing", async () => {
    mockKgFetch(routes(setupFx()));
    await open();
    const pick = $('[data-testid="kg-import-pick"]') as HTMLSelectElement;
    await act(async () => { pick.value = "ghr_1"; pick.dispatchEvent(new Event("change", { bubbles: true })); });
    await click("kg-import-add");
    expect(calls).toContain("PUT /v1/projects/fineract-map/repos/ghr_1");
    expect($('[data-testid="kg-import-msg"]')?.textContent).toBe("Added acme/fineract. Indexing starts within a minute; this list follows it.");
    unmount();
    const indexing = setupFx({
      repositories: [{ repo: "acme/fineract", branch: "develop", status: "running", files: 6508, parsed: 4380, graph: { nodes: 0, edges: 0 } }],
      steps: [
        { key: "import", state: "working", done: false, summary: "0 of 1 repositories indexed · 0 code nodes" },
        { key: "team", state: "current", done: false, summary: "Not chosen yet" },
        { key: "scan", state: "waiting", done: false, summary: "No scan yet" },
        { key: "review", state: "waiting", done: false, summary: "0 waiting · 0 approved · 0 rejected" },
      ],
    });
    mockKgFetch(routes(indexing));
    await open();
    expect(state("import")).toBe("working");
    expect($('[data-testid="kg-import-repos"]')?.textContent).toContain("Indexing 4,380 of 6,508 files");
    expect($('[data-testid="kg-import-repos"] [role="progressbar"]')?.getAttribute("aria-valuenow")).toBe("4380");
    expect($('[data-testid="kg-step-scan-why"]')?.textContent).toBe("Starts when the code is indexed. Indexing is running.");
    // While indexing runs, the team step is open: there is something to do meanwhile.
    expect($('[data-testid="kg-team"]')).not.toBeNull();
  });

  it("② the team: the project's real agents with their jobs and Passports, saved as the project's team", async () => {
    const s = setupFx({
      repositories: [INDEXED_REPO], has_graph: true, code_nodes: 51319, done_count: 1, next: "team",
      steps: [
        { key: "import", state: "done", done: true, summary: "1 of 1 repositories indexed · 51,319 code nodes" },
        { key: "team", state: "current", done: false, summary: "Not chosen yet" },
        { key: "scan", state: "waiting", done: false, summary: "No scan yet" },
        { key: "review", state: "waiting", done: false, summary: "0 waiting · 0 approved · 0 rejected" },
      ],
    });
    mockKgFetch(routes(s));
    await open({ projectType: "knowledge_graph" });
    await click("kg-tab-setup");
    const rec = $$('[data-testid="kg-team-recommended"] .kg-team-row');
    expect(rec.map((r) => r.querySelector("b")?.textContent)).toEqual(["Chief Architect", "Software Engineer", "Site Reliability Engineer"]);
    expect(rec.map((r) => r.querySelector(".kg-team-job")?.textContent)).toEqual(["Scans", "Changes", "Incidents"]);
    // Recommended agents start ticked; others are behind "Show all".
    expect(rec.every((r) => (r.querySelector("input") as HTMLInputElement).checked)).toBe(true);
    expect($('[data-testid="kg-team-qa"]')).toBeNull();
    await click("kg-team-all");
    expect($('[data-testid="kg-team-qa"] b')?.textContent).toBe("QA Lead");
    expect($('[data-testid="kg-team-passport-chief_architect"]')?.getAttribute("title")).toContain("283e9d57f6a5");
    // Untick the engineer, save: one call that STATES the team.
    await act(async () => ($('[data-testid="kg-team-software_engineer"] input') as HTMLInputElement).click());
    expect($('[data-testid="kg-team-save"]')?.textContent).toBe("Save the team (2)");
    await click("kg-team-save");
    const fetchMock = globalThis.fetch as unknown as { mock: { calls: [RequestInfo, RequestInit?][] } };
    const sent = fetchMock.mock.calls.find(([u]) => String(u).includes("/api/project-agents"));
    expect(JSON.parse(String(sent?.[1]?.body))).toMatchObject({ project_id: "fineract-map", app: "*", action: "set", role_keys: ["chief_architect", "sre"] });
    // A Passport link opens the agent.
    await click("kg-team-passport-sre");
    expect(new URLSearchParams(window.location.search).get("role")).toBe("sre");
  });

  it("③ the first scan: an area from the graph, a budget, and the business_rules_scan workflow", async () => {
    const s = setupFx({
      repositories: [INDEXED_REPO], has_graph: true, code_nodes: 51319, done_count: 2, next: "scan", areas: AREAS,
      team: { chosen: true, role_keys: ["chief_architect", "sre"] },
      steps: [
        { key: "import", state: "done", done: true, summary: "1 of 1 repositories indexed · 51,319 code nodes" },
        { key: "team", state: "done", done: true, summary: "2 agents chosen" },
        { key: "scan", state: "current", done: false, summary: "No scan yet" },
        { key: "review", state: "waiting", done: false, summary: "0 waiting · 0 approved · 0 rejected" },
      ],
    });
    mockKgFetch(routes(s));
    window.history.replaceState(null, "", "/?org=o1&project_id=fineract-map&area=knowledge&kg_tab=setup");
    await open();
    expect([state("import"), state("team"), state("scan")]).toEqual(["done", "done", "current"]);
    expect($('[data-testid="kg-step-team-open"]')?.textContent).toBe("Change");
    expect($('[data-testid="kg-scan-step"] .kg-setup-sub')?.textContent).toBe("Choose an area · Chief Architect reads it");
    expect($$('[data-testid="kg-scan-areas"] .kg-area-name').map((n) => n.textContent)).toEqual(["fineract-loan", "fineract-core"]);
    await click("kg-scan");
    expect($('[data-testid="kg-scan-preview"]')?.textContent).toContain("up to 60 files");
    await click("kg-scan-start");
    const fetchMock = globalThis.fetch as unknown as { mock: { calls: [RequestInfo, RequestInit?][] } };
    const sent = fetchMock.mock.calls.find(([u]) => String(u).includes("/api/delivery-workflows"));
    expect(JSON.parse(String(sent?.[1]?.body))).toMatchObject({
      action: "start", workflow_key: "business_rules_scan", input: { scope: ["area:acme/fineract:fineract-loan"], budget_usd: 2 } });
  });

  it("④ review: the queue's count, and Rules review; all done → the Knowledge Graph", async () => {
    const reviewing = setupFx({
      repositories: [INDEXED_REPO], has_graph: true, code_nodes: 51319, done_count: 3, next: "review", areas: AREAS,
      team: { chosen: true, role_keys: ["chief_architect"] }, rules: { proposed: 7, approved: 0, rejected: 0 },
      scans: { count: 1, running: 0, completed: 1, latest: { run_id: "wfr_1", status: "completed", at: null, scope: null } },
      steps: [
        { key: "import", state: "done", done: true, summary: "1 of 1 repositories indexed · 51,319 code nodes" },
        { key: "team", state: "done", done: true, summary: "1 agents chosen" },
        { key: "scan", state: "done", done: true, summary: "1 scan" },
        { key: "review", state: "current", done: false, summary: "7 waiting · 0 approved · 0 rejected" },
      ],
    });
    mockKgFetch(routes(reviewing));
    window.history.replaceState(null, "", "/?org=o1&project_id=fineract-map&area=knowledge&kg_tab=setup");
    await open();
    expect($('[data-testid="kg-review-waiting"]')?.textContent).toBe("7");
    expect($('[data-testid="kg-review-open"]')?.textContent).toBe("Review 7 rules");
    await click("kg-review-open");
    expect($('[data-testid="kg-tab-rules"]')?.getAttribute("aria-selected")).toBe("true");
    unmount();
    const done = setupFx({ ...reviewing, next: null, done_count: 4, rules: { proposed: 0, approved: 7, rejected: 0 }, learnings: 3,
      steps: reviewing.steps.map((st) => ({ ...st, state: "done", done: true })) });
    mockKgFetch(routes(done));
    window.history.replaceState(null, "", "/?org=o1&project_id=fineract-map&area=knowledge&kg_tab=setup");
    await open();
    expect($(".kg-setup-title")?.textContent).toBe("The knowledge graph is set up");
    expect($('[data-testid="kg-step-done"]')?.textContent).toContain("51,319 code nodes · 7 approved rules · 3 learnings");
    const go = $('[data-testid="kg-setup-open-graph"]') as HTMLButtonElement;
    expect(go.className).toBe("ag-btn ag-btn-primary");
    await click("kg-setup-open-graph");
    expect($('[data-testid="kg-tab-graph"]')?.getAttribute("aria-selected")).toBe("true");
    expect($('[data-testid="kg-setup-strip"]')).toBeNull();
  });

  it("with code in but steps left, the graph opens with a Continue setup strip", async () => {
    const s = setupFx({ repositories: [INDEXED_REPO], has_graph: true, code_nodes: 51319, done_count: 1, next: "team",
      steps: [
        { key: "import", state: "done", done: true, summary: "indexed" },
        { key: "team", state: "current", done: false, summary: "Not chosen yet" },
        { key: "scan", state: "waiting", done: false, summary: "No scan yet" },
        { key: "review", state: "waiting", done: false, summary: "—" },
      ] });
    mockKgFetch(routes(s));
    await open();
    expect($('[data-testid="kg-tab-graph"]')?.getAttribute("aria-selected")).toBe("true");
    expect($('[data-testid="kg-setup-strip"]')?.textContent).toContain("Setup · 1 of 4 steps done. Next: Assign the team");
    await click("kg-setup-continue");
    expect($('[data-testid="kg-tab-setup"]')?.getAttribute("aria-selected")).toBe("true");
    expect(setupOpens(s)).toBe(false);
  });
});

describe("the Knowledge Graph says what it is", () => {
  it("a first visit shows the three layers and who added and approved each node; dismissed stays dismissed", async () => {
    mockKgFetch(routes(setupFx({ project_type: "app" })));
    await open({ projectType: "app" });
    expect($('[data-testid="kg-intro"]')?.textContent).toContain("Every node says which agent added it and who approved it.");
    expect($$('[data-testid="kg-intro"] b').map((b) => b.textContent)).toEqual(["Code", "Rules", "Learning"]);
    // An app project with code: no Setup tab, the graph as before.
    expect(tabs()).toEqual(["Graph", "Rules review", "Settings"]);
    await click("kg-intro-dismiss");
    expect($('[data-testid="kg-intro"]')).toBeNull();
    unmount();
    mockKgFetch(routes(setupFx({ project_type: "app" })));
    await open({ projectType: "app" });
    expect($('[data-testid="kg-intro"]')).toBeNull();
  });
});

describe("the project home's lifecycle card", () => {
  it("indexed → rules → learnings with real counts, the team, and the next thing to do", async () => {
    mockKgFetch(routes(setupFx({
      repositories: [INDEXED_REPO], has_graph: true, code_nodes: 51319, done_count: 3, next: "review",
      team: { chosen: true, role_keys: ["chief_architect", "sre"] }, rules: { proposed: 7, approved: 2, rejected: 1 }, learnings: 4,
    })));
    await mount(<KgLifecycleCard projectId="fineract-map" actor="ceo" orgId="o1" />);
    await settle();
    const stages = $$('[data-testid="kg-lifecycle-stages"] .kg-life-stage');
    expect(stages.map((s) => s.querySelector(".kg-life-value")?.textContent)).toEqual(["51,319", "2", "4"]);
    expect(stages.map((s) => s.querySelector(".kg-life-label")?.textContent)).toEqual(["Code indexed", "Rules", "Learnings"]);
    expect(stages[1].textContent).toContain("7 waiting for review");
    expect($$('[data-testid="kg-lifecycle-team"] .kg-life-name').map((s) => s.textContent)).toEqual(["Chief Architect", "Site Reliability Engineer"]);
    expect($('[data-testid="kg-lifecycle-continue"]')?.textContent).toBe("Continue: Review rules");
    const href = new URLSearchParams(($('[data-testid="kg-lifecycle-continue"]') as HTMLAnchorElement).getAttribute("href")!.slice(1));
    expect([href.get("area"), href.get("kg_tab")]).toEqual(["knowledge", "setup"]);
  });
});

describe("one answer to 'is there indexed code?' (tester round 4)", () => {
  const indexedTeamOpen = (scan: KgSetupResponse["steps"][number]) => setupFx({
    repositories: [INDEXED_REPO], has_graph: true, code_nodes: 1625, done_count: 1, next: "team", areas: AREAS,
    steps: [
      { key: "import", state: "done", done: true, summary: "1 of 1 repositories indexed · 1,625 code nodes" },
      { key: "team", state: "current", done: false, summary: "Not chosen yet" },
      scan,
      { key: "review", state: "waiting", done: false, summary: "0 waiting · 0 approved · 0 rejected", blocked_by: "scan" },
    ],
  });

  const cases: [string, KgSetupResponse][] = [
    ["the API's ready scan", { ...indexedTeamOpen({ key: "scan", state: "ready", done: false, summary: "No scan yet", blocked_by: null }), has_code: true }],
    // What the tester saw: Import code DONE beside a scan "waiting" only for its turn.
    ["an older API's waiting scan", indexedTeamOpen({ key: "scan", state: "waiting", done: false, summary: "No scan yet" })],
  ];
  for (const [name, fx] of cases) {
    it(`Import code done never sits beside "Needs indexed code" (${name}); the scan offers its button, cost and cap`, async () => {
      mockKgFetch(routes(fx));
      await open();
      await click("kg-tab-setup");
      expect(state("import")).toBe("done");
      expect(state("scan")).toBe("ready");
      expect($('[data-testid="kg-step-scan"] .kg-setup-state')?.textContent).toBe("Ready");
      expect(document.body.textContent).not.toContain("Needs indexed code");
      expect($('[data-testid="kg-step-scan-why"]')).toBeNull();
      expect($('[data-testid="kg-step-scan"] [data-testid="kg-scan"]')?.textContent).toBe("Scan for rules here");
      expect($('[data-testid="kg-step-scan"] [data-testid="kg-scan-cost"]')?.textContent)
        .toBe("Spends at most $2.00 from the knowledge budget · reads up to 60 files · proposes up to 25 rules");
      // The review still says what it waits for.
      expect($('[data-testid="kg-step-review-why"]')?.textContent).toBe("Rules arrive here from the first scan.");
    });
  }

  it("the shared answer: has_code first, then the graph, then Import code's Done", () => {
    const fx = setupFx();
    expect(hasIndexedCode(fx)).toBe(false);
    expect(hasIndexedCode({ ...fx, has_code: true })).toBe(true);
    expect(hasIndexedCode({ ...fx, has_graph: true })).toBe(true);
    expect(hasIndexedCode({ ...fx, has_graph: undefined as never, steps: [{ ...fx.steps[0], done: true }] })).toBe(true);
    expect(hasIndexedCode({ ...fx, has_code: false, has_graph: true })).toBe(false);
    expect(blockedBy("scan", fx)).toBe("import");
    expect(blockedBy("scan", { ...fx, has_code: true })).toBeNull();
    expect(blockedBy("review", fx)).toBe("scan");
  });
});
