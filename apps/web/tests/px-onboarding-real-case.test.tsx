// @vitest-environment happy-dom
/**
 * Screen 10 (with 00b's live parts) on the REAL case: `_helpers/onboarding-helpdesk-real.json` is
 * what the engine served for helpdesk-ai-demo after the fix (apps/api/tests/
 * test_onboarding_reads_the_real_helpdesk_repo.py replays it from the repo's own files).
 *
 * The onboarding is a full page (`tab=onboarding`: Projects / HelpDesk AI Demo / Onboarding), not
 * a thin drawer: the stage strip, the Now line, the read-only workspace row, "Helm is reading
 * your repo", Discovered · Ready for work · Backlog proposed, Only you can and First work,
 * suggested. Live while it runs (a 3 s read and a ticking timer); a re-read says when it read.
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));

import { ProjectPage } from "../app/projects/project-page";
import { crumbsFor } from "../app/shell-crumbs";
import { FX_DEPLOYMENTS, FX_HEALTH, FX_KNOWLEDGE, FX_OVERVIEW, FX_SETTINGS, FX_WORK } from "../lib/project-experience-fixtures";
import { parseOnboarding } from "../lib/project-experience";
import { projectPageHref } from "../lib/start-project";
import REAL from "./_helpers/onboarding-helpdesk-real.json";
import { $$, mountAs, stubFetch, tid, unmount, type Call } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const P = "helpdesk-ai-demo";
const settle = async (n = 8) => { for (let i = 0; i < n; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };
const press = async (el: Element | null) => { if (!el) throw new Error("nothing to press"); await act(async () => { (el as HTMLElement).click(); }); await settle(); };
const at = (q: string) => window.history.replaceState(null, "", `/?org=org_1&${q}`);
const params = () => new URLSearchParams(window.location.search);

/** The real record, as it was while Helm was still mapping the code. */
const RUNNING = {
  ...REAL, state: "running", finished_at: null, read_ago_s: null, now: "Helm is mapping the code · 118 / 214 files",
  started_at: new Date(Date.now() - 112_000).toISOString(), elapsed_s: 112,
  discover: { ...REAL.discover, live: [
    { key: "stack", label: "Stack found", state: "done", sub: "Vite · JavaScript" },
    { key: "mapping", label: "Mapping code", state: "now", sub: "118 / 214 files" },
    { key: "tests", label: "Tests run", state: "todo", sub: "" },
    { key: "ready", label: "Ready check", state: "todo", sub: "" }] },
};
/** The real record with its staging host set (the laptop's case): Ready is 6/6. */
const READY = {
  ...REAL, state: "ready", read_ago_s: 38, runs: 2, trigger: "manual",
  ready: REAL.ready.map((r) => (r.key === "deploy_target" ? { ...r, state: "done", owner: "agent", sub: "Staging ready", action: null } : r)),
  checklist: REAL.checklist.map((r) => (r.key === "deploy_target" ? { ...r, state: "done", owner: "agent", sub: "Staging ready", action: null } : r)),
  only_you: [],
  // The engine's one state machine once Ready is 6/6: Work it is current, Deploy ahead.
  stages: REAL.stages.map((st) => ({ ...st, state: st.key === "first_work" ? "now" : st.key === "deploy" ? "todo" : "done" })),
};

function engine(onboarding: unknown) {
  return stubFetch((c: Call) => {
    if (c.method !== "GET") {
      if (c.path.endsWith("/onboarding/backlog/accept")) return { body: { accepted: (c.body as { ids: string[] }).ids, already: [], started: [], lane: "next" } };
      if (c.path.endsWith("/onboarding/start")) return { body: RUNNING };
      return { body: { ok: true } };
    }
    const m = c.path.match(/^\/v1\/projects\/([^/]+)\/(overview|work|knowledge|deployments|onboarding|settings|health|captain|plan)$/);
    if (m) {
      const table: Record<string, unknown> = {
        overview: { ...FX_OVERVIEW, project: { ...FX_OVERVIEW.project, id: P, stage: "ready" } }, work: FX_WORK, knowledge: FX_KNOWLEDGE,
        deployments: FX_DEPLOYMENTS, onboarding, settings: FX_SETTINGS, health: FX_HEALTH, plan: { now: [], next: [], later: [] },
      };
      return { body: table[m[2]] };
    }
    return undefined;
  });
}

beforeEach(() => { window.history.replaceState(null, "", "/"); });
afterEach(() => { unmount(); vi.useRealTimers(); });

async function open(q = "&tab=onboarding") {
  at(`project_id=${P}&area=project${q}`);
  await mountAs(<ProjectPage projectId={P} actor="ceo" orgId="org_1" />, "owner");
  await settle(10);
}

describe("Onboarding is its own page (screen 10)", () => {
  it("has the breadcrumb Projects / <project> / Onboarding and no tabs or Needs-you bar", async () => {
    engine(READY);
    await open();
    expect(crumbsFor({ active: "project", areaLabel: "", routeMode: false, hasProject: true, tab: "onboarding" }).map((c) => c.label))
      .toEqual(["Projects", "", "Onboarding"]);
    expect(tid("px-onboarding")).not.toBeNull();
    expect(tid("px-tabs")).toBeNull();
    expect(tid("px-needs-bar")).toBeNull();
  });

  it("draws every piece of the wireframe from the real record", async () => {
    engine(READY);
    await open();
    expect($$("[data-testid='px-ob-stages'] .px-ob-cap").map((x) => x.textContent)).toEqual(["Discover", "Ready for work", "Propose backlog", "Work it", "Deploy"]);
    expect(tid("px-ob-stage-discover")!.textContent).toContain("done · 5s");
    // Workspace, read-only (§15): GitHub and the model link to Setup, no picker anywhere.
    expect(tid("px-ob-ws-github")!.textContent).toContain("GitHub ✓");
    expect(tid("px-ob-ws-model")!.textContent).toContain("Model ✓");
    expect(new URLSearchParams(tid("px-ob-ws-model")!.getAttribute("href")!.split("?")[1]).get("focus")).toBe("models");
    expect($$("[data-testid='px-onboarding'] select, [data-testid='px-onboarding'] [role=listbox]")).toEqual([]);
    // Helm read the repo: Stack found · Mapping code · Tests run, with the real values.
    expect(tid("px-ob-livecard")!.textContent).toContain("Helm read your repo");
    expect(tid("px-ob-live")!.textContent).toContain("Vite · JavaScript");
    expect(tid("px-ob-live")!.textContent).toContain("96 files mapped");
    expect(tid("px-ob-live")!.textContent).toContain("71 / 190 pass");
    // Discovered: the findings themselves.
    expect(tid("px-discover-label-files")!.textContent).toBe("109 files");
    expect(tid("px-discover-label-tests")!.textContent).toBe("71/190 tests");
    // Ready for work: six rows, the agent on each, the tests row sends its failures to the backlog.
    // The counter says what it counts; the skipped build is a muted clock "on first build", never
    // an empty box beside the ticks.
    expect(tid("px-ob-ready-count")!.textContent).toBe("6/6 ready · 1 on first build");
    const skipped = $$("[data-testid^='px-ob-item-'][data-s='skipped']");
    expect(skipped.length).toBe(1);
    expect(skipped[0].textContent).toContain("on first build");
    expect(skipped[0].querySelector(".px-ck-b svg")).not.toBeNull();
    expect(tid("px-ob-item-tests")!.textContent).toContain("119 failing → backlog");
    expect(tid("px-ob-item-secrets")!.textContent).toContain("22 read · 2 generated");
    // Backlog: Marshal's order with each item's why.
    expect(tid("px-ob-backlog-0")!.textContent).toContain("Fix the 119 failing tests");
    expect(tid("px-ob-backlog-why-0")!.textContent).toContain("71/190 pass · a blocked build");
    expect(tid("px-ob-backlog-1")!.textContent).toContain("Add CI");
    // First work, suggested: Marshal's pick and why, the estimate, Start (enabled at 6/6), Other ideas.
    expect(tid("px-ob-first-label")!.textContent).toBe("Fix the 119 failing tests");
    expect(tid("px-ob-first-why")!.textContent).toContain("A red suite blocks every later change");
    expect(tid("px-ob-first-min")!.textContent).toBe("≈ 85 min");
    expect((tid("px-ob-first-start") as HTMLButtonElement).disabled).toBe(false);
    expect(tid("px-ob-first-others")).not.toBeNull();
    expect(tid("px-ob-you-none")).not.toBeNull();
    // A manual re-read says when it read.
    expect(tid("px-ob-read-ago")!.textContent).toBe("re-read 38s ago");
    // One primary per view: Accept backlog while there is a backlog to accept.
    expect($$("[data-testid='px-onboarding'] .px-btn[data-kind='primary']").map((b) => b.getAttribute("data-testid"))).toEqual(["px-ob-accept"]);
  });

  it("Accept backlog sends the proposals onto Next and starts nothing", async () => {
    const calls = engine(READY);
    await open();
    await press(tid("px-ob-accept"));
    const posted = calls.find((c) => c.path === `/v1/projects/${P}/onboarding/backlog/accept`);
    expect(posted?.body).toEqual({ ids: REAL.backlog.map((b) => b.id), titles: {} });
    expect(tid("px-ob-said")!.textContent).toContain("Nothing has started");
    expect(calls.some((c) => c.path.endsWith("/work") && c.method === "POST")).toBe(false);
  });

  it("while it runs it is live: the 00b diagram, the Now line, a ticking timer and a 3 s read", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const calls = engine(RUNNING);
    await open();
    expect(tid("px-ob-livecard")!.textContent).toContain("Helm is reading your repo");
    expect(tid("px-ob-live-mapping")!.getAttribute("data-s")).toBe("now");
    expect(tid("px-ob-live")!.textContent).toContain("118 / 214 files");
    expect(tid("px-ob-live-ready")!.getAttribute("data-s")).toBe("todo");
    expect(tid("px-ob-now")!.textContent).toContain("Helm is mapping the code");
    const t0 = tid("px-ob-clock")!.textContent;
    expect(t0).toMatch(/^1m 5\ds$/);
    await act(async () => { vi.advanceTimersByTime(4_000); });
    await settle();
    expect(tid("px-ob-clock")!.textContent).not.toBe(t0);
    const reads = calls.filter((c) => c.method === "GET" && c.path === `/v1/projects/${P}/onboarding`).length;
    await act(async () => { vi.advanceTimersByTime(7_000); });
    await settle();
    expect(calls.filter((c) => c.method === "GET" && c.path === `/v1/projects/${P}/onboarding`).length).toBeGreaterThan(reads);
    expect(tid("px-ob-restart")).toBeNull();
    expect((tid("px-ob-first-start") as HTMLButtonElement).disabled).toBe(true);
  });

  it("Read again re-reads the repo on the same page", async () => {
    const calls = engine(READY);
    await open();
    await press(tid("px-ob-restart"));
    expect(calls.some((c) => c.method === "POST" && c.path === `/v1/projects/${P}/onboarding/start`)).toBe(true);
    expect(tid("px-ob-said")!.textContent).toContain("reading the repository again");
    expect(params().get("tab")).toBe("onboarding");
  });

  it("parses the engine's why, Marshal's reason, the test baseline and the re-read", () => {
    const ob = parseOnboarding(READY);
    expect(ob.backlog[0].why).toBe("71/190 pass · a blocked build");
    expect(ob.backlog[0].rec_id).toMatch(/^wi_/);
    expect(ob.first_work?.first_by).toBe("marshal");
    expect(ob.tests).toMatchObject({ passed: 71, total: 190, failing: 119, source: "the last full run, a blocked build, 9h ago" });
    expect(ob.read_ago_s).toBe(38);
  });

  it("a project added from a repository lands on this view (onboarding starts by itself)", () => {
    at("area=projects&new_project=import");
    const href = new URLSearchParams(projectPageHref("hd-2", "home", "project", "onboarding").split("?")[1]);
    expect([href.get("project_id"), href.get("area"), href.get("tab"), href.get("new_project")]).toEqual(["hd-2", "project", "onboarding", null]);
  });
});

describe("one state machine: the strip, the cards, the live card and the guide never contradict (10-08, PX-15, 00b-13/15)", () => {
  it("a finished read is not live anywhere: done tags, no live badge, a Now line that names whose move it is, and Watch opens the log", async () => {
    engine({ ...READY, live: false, title: "Helm read your repo", now: "Marshal suggests Add CI first · start it when you are ready", now_agent: "chief_of_staff", now_live: false,
             log: [{ at: "2026-11-07T05:37:30Z", agent: "engineering_captain", label: "Started", state: "done", text: "Helm started reading the repository." },
                   { at: "2026-11-07T05:37:34Z", agent: "sre", label: "Staging host", state: "done", text: "Staging deploys to staging." }] });
    await open();
    expect(tid("px-ob-live-tag")).toBeNull();
    expect(tid("px-ob-discovered-tag")!.textContent).toBe("done");
    expect(tid("px-ob-livecard")!.textContent).toContain("done");
    expect(tid("px-ob-now")!.textContent).toContain("Marshal suggests Add CI first");
    expect(tid("px-ob-now")!.getAttribute("data-live")).toBeNull();
    expect(tid("px-ob-log")).toBeNull();
    await press(tid("px-ob-watch"));
    expect(tid("px-ob-log")!.textContent).toContain("Staging deploys to staging.");
    expect(params().get("area")).toBe("project");          // Watch stays on the page
  });

  it("the stage strip shows exactly one current stage and nothing after it is ticked", async () => {
    engine(READY);
    await open();
    const states = $$("[data-testid='px-ob-stages'] .px-ob-s").map((x) => x.getAttribute("data-s"));
    const first = states.findIndex((s) => s !== "done" && s !== "skipped");
    if (first >= 0) expect(states.slice(first + 1).every((s) => s === "todo")).toBe(true);
    // Discover done shows its tick, never the failed cross.
    expect(tid("px-ob-stage-discover")!.getAttribute("data-s")).toBe("done");
  });

  it("the Discovered tag follows the run: live only while it moves, not started before", async () => {
    const { discoveredTag } = await import("../app/projects/onboarding");
    expect(discoveredTag("running", "now").word).toBe("live");
    expect(discoveredTag("ready", "done").word).toBe("done");
    expect(discoveredTag("needs_you", "now").word).toBe("in progress");
    expect(discoveredTag("not_started", "todo").word).toBe("not started");
    expect(discoveredTag("failed", "failed").word).toBe("failed");
  });

  it("the first-run guide reads the same machine: before the start nothing is current, no live badge, no clock", async () => {
    const { guideSteps } = await import("../app/projects/first-run");
    const notStarted = parseOnboarding({ state: "not_started", stages: [{ key: "discover", label: "Discover", state: "todo", sub: "" }] });
    const before = guideSteps({ name: "HelpDesk", repo: "acme/helpdesk" }, notStarted);
    expect(before.map((s) => s.state)).toEqual(["done", "todo", "todo", "todo"]);
    expect(before[1].sub).toBe("not started yet");
    expect(notStarted.live).toBe(false);
    expect(notStarted.title).toBe("Helm hasn't started");
    const mid = parseOnboarding({ ...READY, stages: [
      { key: "discover", label: "Discover", state: "done", sub: "done · 3m" }, { key: "ready", label: "Ready for work", state: "now", sub: "4 of 6" },
      { key: "backlog", label: "Propose backlog", state: "todo", sub: "drafting" }, { key: "first_work", label: "Work it", state: "todo", sub: "" },
      { key: "deploy", label: "Deploy", state: "todo", sub: "" }] });
    const steps = guideSteps({ name: "HelpDesk", repo: "acme/helpdesk" }, mid);
    expect(steps.map((s) => s.state)).toEqual(["done", "done", "now", "todo"]);
    expect(steps.filter((s) => s.state === "now")).toHaveLength(1);
    expect(steps.every((s) => !/\b(todo|now|done)$/.test(s.sub))).toBe(true);
  });

  it("10-11: every Discovered label sits outside its node, away from Helm, so none crosses an edge", async () => {
    engine(READY);
    await open();
    for (const key of ["files", "stack", "tests", "hotspots", "docs"]) {
      const g = tid(`px-discover-${key}`)!;
      const c = g.querySelector("circle")!; const t = tid(`px-discover-label-${key}`)!;
      const cx = Number(c.getAttribute("cx")); const tx = Number(t.getAttribute("x"));
      const anchor = t.getAttribute("text-anchor");
      // Left of Helm: the label ends left of the node; right (or centre-bottom): it starts right of it.
      if (cx < 200) { expect(anchor).toBe("end"); expect(tx).toBeLessThan(cx - 14); }
      else { expect(anchor).toBe("start"); expect(tx).toBeGreaterThan(cx + 14); }
      expect(g.querySelector("title")!.textContent).toContain(t.textContent!);
    }
  });
});
