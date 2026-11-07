// @vitest-environment happy-dom
/**
 * REPO & KNOWLEDGE reads like screen 06 (BOSSAPP-913/914, GAPS #14): the access strip with Merge: you
 * and Staging, Re-check runs the real checks, the dossier's commands / tests ring / code map / heat
 * bars, the graph snippet whose labels never overlap (at any label length: the SVG scales as one),
 * rule and learned counts, docs that open their drawer, learned rows with run and reuse, and — with
 * no dossier — "Build dossier now" posting the onboarding pass and showing its progress.
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));

import { ProjectPage } from "../app/projects/project-page";
import { circleBox, docKey, layoutGraph, overlaps, stackIcon } from "../app/projects/tab-knowledge";
import { FX_KNOWLEDGE, FX_OVERVIEW, FX_SETTINGS, FX_WORK, FX_ONBOARDING, FX_DEPLOYMENTS, FX_HEALTH, FX_SUMMARY } from "../lib/project-experience-fixtures";
import { parseKnowledge } from "../lib/project-experience";
import { $$, mountAs, stubFetch, tid, unmount, type Call } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const P = "helpdesk-ai-demo";
const settle = async (n = 8) => { for (let i = 0; i < n; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };
const press = async (el: Element | null) => { if (!el) throw new Error("nothing to press"); await act(async () => { (el as HTMLElement).click(); }); await settle(); };
const params = () => new URLSearchParams(window.location.search);

function engine(knowledge: unknown) {
  return stubFetch((c: Call) => {
    if (c.method !== "GET") return { body: { ok: true } };
    if (c.path === "/v1/projects/summary") return { body: FX_SUMMARY };
    const m = c.path.match(/^\/v1\/projects\/([^/]+)\/(overview|work|knowledge|deployments|onboarding|settings|health|captain)$/);
    if (m) {
      const table: Record<string, unknown> = { overview: FX_OVERVIEW, work: FX_WORK, knowledge, deployments: FX_DEPLOYMENTS,
                                               onboarding: { ...FX_ONBOARDING, state: "ready" }, settings: FX_SETTINGS, health: FX_HEALTH };
      return { body: table[m[2]] };
    }
    return undefined;
  });
}
async function openKnowledge(knowledge: unknown = FX_KNOWLEDGE, q = "") {
  window.history.replaceState(null, "", `/?org=org_1&project_id=${P}&area=project&tab=knowledge${q}`);
  const calls = engine(knowledge);
  await mountAs(<ProjectPage projectId={P} actor="ceo" orgId="org_1" />, "owner");
  await settle();
  return calls;
}

beforeEach(() => { window.history.replaceState(null, "", "/"); });
afterEach(() => { unmount(); });

describe("the graph snippet's layout", () => {
  const labels = ["Ticket", "Customer", "SLA policy", "sla.py", "Escalation", "Org", "tests/demo_seed.test", "GET /api/tickets/:id",
                  "server/ai/suggest", "user_identities", "POST /api/tickets", "Workaround: npm test", "Make npm run build pass"];
  it("puts no label on another label or on a node, inside the frame, for 2 to 8 nodes of any length", () => {
    for (let n = 2; n <= 8; n++) {
      for (let shift = 0; shift < labels.length; shift++) {
        const nodes = Array.from({ length: n }, (_, i) => ({ id: `n${i}`, label: labels[(i + shift) % labels.length] }));
        const laid = layoutGraph(nodes);
        const boxes = [...laid.labels.entries()];
        for (const [id, l] of boxes) {
          expect(l.box.x).toBeGreaterThanOrEqual(0);
          expect(l.box.x + l.box.w).toBeLessThanOrEqual(420);
          for (const [other, o] of boxes) if (other !== id) expect(overlaps(l.box, o.box), `${id} over ${other} (${n} nodes)`).toBe(false);
          for (const [nid, p] of laid.pos) if (nid !== id && !(l.inside && nid === id)) expect(overlaps(l.box, circleBox(p)), `${id} over node ${nid}`).toBe(false);
        }
      }
    }
  });
  it("06-17/18 on the real HelpDesk graph: no label on a label, a node or an edge", () => {
    const nodes = [{ id: "hub", label: "tickets.mjs" }, { id: "l1", label: "The app has no test script" }, { id: "sla", label: "sla.mjs" },
                   { id: "fix", label: "Make npm test pass" }, { id: "met", label: "metrics.mjs" }, { id: "seed", label: "seedDemoTickets" },
                   { id: "t", label: "sla.test.mjs" }, { id: "api", label: "api.js" }];
    const edges = [["l1", "met"], ["l1", "hub"], ["l1", "api"], ["fix", "met"], ["fix", "hub"], ["fix", "api"], ["l1", "fix"], ["seed", "sla"], ["hub", "sla"], ["t", "sla"]]
      .map(([from, to]) => ({ from, to }));
    const laid = layoutGraph(nodes, 420, 240, edges);
    const crosses = (box: { x: number; y: number; w: number; h: number }, a: { x: number; y: number; r: number }, b: { x: number; y: number; r: number }) => {
      for (let i = 0; i <= 60; i++) {
        const x = a.x + ((b.x - a.x) * i) / 60, y = a.y + ((b.y - a.y) * i) / 60;
        if (Math.hypot(x - a.x, y - a.y) <= a.r || Math.hypot(x - b.x, y - b.y) <= b.r) continue;
        if (x > box.x && x < box.x + box.w && y > box.y && y < box.y + box.h) return true;
      }
      return false;
    };
    for (const [id, l] of laid.labels) {
      if (l.inside) continue;
      for (const [other, o] of laid.labels) if (other !== id) expect(overlaps(l.box, o.box), `${id} over ${other}`).toBe(false);
      for (const [nid, p] of laid.pos) if (nid !== id) expect(overlaps(l.box, circleBox(p)), `${id} over node ${nid}`).toBe(false);
      for (const e of edges) expect(crosses(l.box, laid.pos.get(e.from)!, laid.pos.get(e.to)!), `${id} on edge ${e.from}→${e.to}`).toBe(false);
    }
  });
  it("centres the first node (the hub) and keeps a short hub label inside it", () => {
    const laid = layoutGraph([{ id: "hub", label: "Ticket" }, { id: "a", label: "Customer" }]);
    expect(laid.pos.get("hub")).toMatchObject({ x: 210, y: 120 });
    expect(laid.labels.get("hub")!.inside).toBe(true);
  });
});

describe("Repo & knowledge (screen 06)", () => {
  it("draws the repo meta, the access strip with Merge: you and Staging, and Re-check runs the checks", async () => {
    const calls = await openKnowledge();
    const repo = tid(`px-kn-repo-paramg/helpdesk-ai-demo`)!;
    expect(repo.textContent).toContain("last push 3h");
    expect(repo.textContent).toContain("via GitHub App · paramg");
    expect($$("[data-testid='px-kn-repos'] .px-fl-n").map((n) => [n.textContent, n.getAttribute("data-t")]))
      .toEqual([["Read", "done"], ["Branch", "done"], ["Open PR", "done"], ["Merge: you", "you"], ["Staging", "done"]]);
    await press(tid("px-kn-recheck-paramg/helpdesk-ai-demo"));
    const post = calls.find((c) => c.method === "POST" && c.path === "/v1/setup/checks");
    expect(post?.body).toMatchObject({ project_id: P, target: "repository:ghb_demo" });
  });

  it("shows the dossier: stack chips with icons, copyable commands, the tests ring, the code map and heat bars", async () => {
    await openKnowledge();
    const d = tid("px-kn-dossier")!;
    expect(d.textContent).toContain("by Helm · re-read 3h ago");
    expect($$("[data-testid='px-kn-stack'] .px-tag")).toHaveLength(6);
    expect($$("[data-testid='px-kn-stack'] svg")).toHaveLength(6);
    expect($$("[data-testid='px-kn-commands'] dt").map((x) => x.textContent)).toEqual(["Install", "Test", "Build", "Run"]);
    expect(tid("px-kn-tests")!.textContent).toContain("142 / 148");
    expect(tid("px-kn-tests")!.textContent).toContain("6 failing");
    await press(tid("px-kn-tests-work"));
    expect(params().get("tab")).toBe("work");
  });

  it("a suite BOSS could not run says so — never a pass rate or \"N failing\" (crun_de96bf12)", async () => {
    const k = FX_KNOWLEDGE as unknown as { dossier: { tests: Record<string, unknown> } };
    const env = { sentence: "BOSS couldn't run the full suite: the test database refused the login BOSS gave it.",
                  reason: "the test database refused the login BOSS gave it", owner: "boss" };
    await openKnowledge({ ...k, dossier: { ...k.dossier, tests: { ...k.dossier.tests, passed: 71, total: 190, failing: 0, not_run: 119, env } } });
    const t = tid("px-kn-tests-env")!;
    expect(t.textContent).toContain("Couldn't run the full suite");
    expect(t.textContent).toContain("the test database refused the login BOSS gave it");
    expect(t.textContent).toContain("re-runs it on the next build");
    expect(tid("px-kn-tests")).toBeNull();
    expect(tid("px-kn-dossier")!.textContent).not.toMatch(/failing|all pass/);
  });

  it("the code map is four real parts with tooltips and the file count", async () => {
    await openKnowledge();
    const parts = $$("[data-testid='px-kn-codemap'] .px-kn-map span");
    expect(parts.map((p) => p.getAttribute("title"))).toEqual(["app · 90 files (42%)", "web · 75 files (35%)", "tests · 32 files (15%)", "infra · 17 files (8%)"]);
    expect(tid("px-kn-codemap")!.textContent).toContain("app · web · tests · infra · 214 files");
    expect(tid("px-kn-hotspots")!.textContent).toContain("9×");
  });

  it("draws the graph centred on the hub, coloured by kind, with rules / learned counts", async () => {
    await openKnowledge();
    expect(tid("px-kn-node-0")!.getAttribute("data-group")).toBe("hub");
    expect(tid("px-kn-node-0")!.textContent).toContain("Ticket");
    const groups = $$("[data-testid^='px-kn-node-']").map((g) => g.getAttribute("data-group"));
    expect(groups).toEqual(expect.arrayContaining(["rule", "learned", "code", "data"]));
    expect($$(".px-kg .e-rule")).toHaveLength(1);
    const counts = tid("px-kn-graph-counts")!.textContent!;
    expect(counts).toContain("412 nodes"); expect(counts).toContain("9 rules"); expect(counts).toContain("14 learned");
  });

  it("docs and decisions open their drawer; Full dossier opens the dossier drawer; Add doc opens the upload", async () => {
    await openKnowledge();
    expect(tid("px-kn-doc-3")!.textContent).toContain("Policy · you, Nov 2");
    await press(tid("px-kn-doc-0"));
    expect(params().get("drawer")).toBe("doc:prd:m1");
    expect(tid("px-drawer-doc")!.textContent).toContain("PRD · SLA timers");
    await press(tid("px-kn-full-dossier"));
    expect(params().get("drawer")).toBe("dossier");
    expect(tid("px-drawer-dossier")).not.toBeNull();
    await press(tid("px-kn-add-doc"));
    expect(params().get("drawer")).toBe("doc:new");
    expect(tid("px-drawer-add-doc")).not.toBeNull();
  });

  it("learned rows show the agent, the lesson, the run and reuse, used / new", async () => {
    await openKnowledge();
    const rows = $$("[data-testid='px-kn-learned'] .px-li");
    expect(rows.map((r) => r.querySelector("b")!.textContent)).toEqual(["Tests need TZ=UTC", "Migrate before restart", "UI tokens in theme.ts"]);
    expect(rows[0].textContent).toContain("Run #38 · reused 2×");
    expect(rows.map((r) => r.querySelector(".px-tag")!.textContent)).toEqual(["used", "used", "new"]);
  });

  it("with no dossier: an illustrated 'Build dossier now' that posts the onboarding pass", async () => {
    const none = { ...FX_KNOWLEDGE, dossier: null, learned: [],
                   build: { label: "Build dossier now", href: `/v1/projects/${P}/onboarding/start`, method: "POST", state: "not_started", progress: "", nodes: [] } };
    const calls = await openKnowledge(none);
    expect(tid("px-kn-dossier-empty")!.querySelector("svg.px-illus")).not.toBeNull();
    expect(tid("px-kn-learned")!.querySelector("svg.px-illus")).not.toBeNull();
    await press(tid("px-kn-build-now"));
    expect(calls.some((c) => c.method === "POST" && c.path === `/v1/projects/${P}/onboarding/start`)).toBe(true);
  });

  it("while it builds: the progress, and no second button", async () => {
    const running = { ...FX_KNOWLEDGE, dossier: null,
                      build: { label: "Build dossier now", href: "", state: "running", progress: "118 / 214 files",
                               nodes: [{ label: "Repo access", state: "done", sub: "" }, { label: "Mapping code", state: "now", sub: "118 / 214 files" }] } };
    await openKnowledge(running);
    expect(tid("px-kn-build")!.textContent).toContain("Helm is reading the repository");
    expect(tid("px-kn-build-now")).toBeNull();
    expect(tid("px-kn-build-progress")!.textContent).toContain("Mapping code · 118 / 214 files");
  });
});

describe("the knowledge client", () => {
  it("parses tri-state access, the commands in Install · Test · Build · Run order, and doc keys", () => {
    const k = parseKnowledge({ repos: [{ full_name: "a/b", access: { read: true, write: null, pr: false } }],
                               dossier: { commands: { run: "x", build: "y", install: "z", test: "t" } } });
    expect(k.repos[0].checked).toEqual({ read: true, write: null, pr: false });
    expect(Object.keys(k.dossier!.commands)).toEqual(["install", "test", "build", "run"]);
    expect(docKey({ kind: "prd", href: "?area=meetings&meeting=m9" })).toBe("prd:m9");
    expect(docKey({ kind: "adr", href: "", path: "docs/adr-1.md" })).toBe("docs/adr-1.md");
    expect(stackIcon("Postgres 16")).not.toBe(stackIcon("React 18"));
  });
});

describe("06-01 / 13-23: Add repo opens today's Import-a-repo picker on the project page", () => {
  async function open(tab: "knowledge" | "settings") {
    const { registryFixture, projectReposFixture } = await import("./_helpers/multirepo-fixtures");
    const reg = registryFixture();
    reg.items.push({ ...reg.items[1], id: "ghr_billing", full_name: "acme/billing", used_by: [], pushed_at: new Date(Date.now() - 3 * 3600_000).toISOString() });
    window.history.replaceState(null, "", `/?org=org_1&project_id=${P}&area=project&tab=${tab}`);
    const calls = stubFetch((c: Call) => {
      if (c.path === "/v1/github/repos") return { body: reg };
      if (c.path === `/v1/projects/${P}/repos` && c.method === "GET") return { body: projectReposFixture() };
      if (c.method === "PUT" && c.path === `/v1/projects/${P}/repos/ghr_billing`) return { body: projectReposFixture() };
      if (c.method !== "GET") return { body: { ok: true } };
      if (c.path === "/v1/projects/summary") return { body: FX_SUMMARY };
      const m = c.path.match(/^\/v1\/projects\/([^/]+)\/(overview|work|knowledge|deployments|onboarding|settings|health|captain)$/);
      if (m) {
        const table: Record<string, unknown> = { overview: FX_OVERVIEW, work: FX_WORK, knowledge: FX_KNOWLEDGE, deployments: FX_DEPLOYMENTS,
                                                 onboarding: { ...FX_ONBOARDING, state: "ready" }, settings: FX_SETTINGS, health: FX_HEALTH };
        return { body: table[m[2]] };
      }
      return undefined;
    });
    await mountAs(<ProjectPage projectId={P} actor="ceo" orgId="org_1" />, "owner");
    await settle();
    return calls;
  }

  it("Repo & knowledge › + Add repo: the picker (accounts, search, rows with last push), never the Add-account sheet, and Add binds it here", async () => {
    const calls = await open("knowledge");
    await press(tid("px-kn-add-repo"));
    expect(params().get("area")).toBe("project");                  // stays on the page
    expect(tid("px-add-repo")).not.toBeNull();
    expect(tid("px-add-repo-search")).not.toBeNull();
    expect(tid("px-add-repo-age-ghr_billing")!.textContent).toBe("3h");
    // Already in this project: tagged, not pickable.
    expect(tid("px-add-repo-here-ghr_web")!.textContent).toBe("in this project");
    expect((tid("px-add-repo-repo-ghr_web") as HTMLButtonElement).disabled).toBe(true);
    await press(tid("px-add-repo-repo-ghr_billing"));
    await press(tid("px-add-repo-go"));
    expect(calls.some((c) => c.method === "PUT" && c.path === `/v1/projects/${P}/repos/ghr_billing`)).toBe(true);
    expect(tid("px-add-repo-said")!.textContent).toContain("Added acme/billing");
  });

  it("Settings › Repos › + Add opens the same picker", async () => {
    await open("settings");
    await press(tid("px-set-repos-add"));
    expect(tid("px-add-repo")).not.toBeNull();
    expect(params().get("area")).toBe("project");
  });
});
