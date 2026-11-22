// @vitest-environment happy-dom
/**
 * WORK PANEL V2 (approved wireframe 2026-11-22; operator: "let's build this quickly but without
 * quality issues.. make sure all the links work"):
 *
 *   - four stages GROUP the workflow's own steps (Plan · Build · Ship · Live), Merge is Build's
 *     last step, a stage the workflow lacks is grey "Not in this workflow";
 *   - each started stage links the screen its work lives on: a panel meeting, the Build and test
 *     page, the Workflow run (merge, approvals), Deployments, Observability; a stage not started
 *     has no link; Watch live goes to the screen of the stage BOSS is on;
 *   - one "what next" line, amber only when it is the person's move;
 *   - the Work list is one row per task (re-runs fold into it), on tabs by who acts, latest first,
 *     and the Needs-you count is one number everywhere.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act } from "react";

import { $, $$, mount, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
beforeEach(() => { vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 404 }))); });
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const P = "northgate-gateway";
const LOCAL = "http://boss-local/local_northgate-gateway/northgate-gateway/pull/24";
const q = (h: string) => new URLSearchParams(h.split("?")[1] ?? "");
const step = (id: string, capability: string | null, stage: string, state: string, extra: Record<string, unknown> = {}) =>
  ({ id, name: id.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()), kind: capability ? "capability" : "decision", capability, stage, state,
     started_at: "", completed_at: "", sub: "", refs: {}, ...extra });

/** Workflow #27 "Lumen Pay tenant onboarding" as the engine serves it (Onboard a config file). */
const LUMEN = {
  id: "wfr_c6d43bb861484e81aa3a", source: "workflow_run", source_label: "Workflow", run_number: 27,
  label: "Lumen Pay tenant onboarding", title: "Lumen Pay tenant onboarding", kind: "feature", state: "needs_you", status: "waiting",
  shape: "code", flow: [], workflow: "Onboard a config file", input_file: "lumen-pay.yaml", action_label: "Merge",
  created_at: "2026-11-22T16:12:42Z", updated_at: "2026-11-22T18:13:34Z", cost_usd: 0, list_usd: 1.43, agents: [], attempts: [], attempt_count: 2,
  stages: [],
  steps: [
    step("build", "build", "build", "done", { completed_at: "2026-11-22T16:16:17Z", refs: { build: `?project_id=${P}&area=code_tasks&view=apps&build=crun_eb81d51b7a34441d8fc9` } }),
    step("review", "review", "build", "done", { completed_at: "2026-11-22T16:16:36Z" }),
    step("merge", "merge", "build", "now", { started_at: "2026-11-22T16:16:37Z" }),
    step("staging", "config_deploy", "ship", "todo"),
    step("contract_tests", "contract_test", "ship", "todo"),
  ],
  links: {
    workflow_run: `?project_id=${P}&area=delivery_workflows&wfr=wfr_c6d43bb861484e81aa3a`,
    build: `?project_id=${P}&area=code_tasks&view=apps&build=crun_28474bef25d04e1990e4`,
    pr: { number: 24, external_url: LOCAL }, deploy: null, preview: null,
    decision: `?project_id=${P}&area=decisions&decision=dec_599a1d759dc249ab947d`,
    merge_pr: { number: 24, external_url: LOCAL, build_id: "crun_eb81d51b7a34441d8fc9" }, merged_pr: null,
  },
  detail: "Fix the file this run started from so the repository's own checkers pass.",
  href: `?project_id=${P}&area=project&tab=work&drawer=run%3Awfr_c6d43bb861484e81aa3a`,
};

async function item(over: Record<string, unknown> = {}) {
  const { parseWorkItem } = await import("../lib/project-experience");
  return parseWorkItem({ ...LUMEN, ...over });
}

describe("the four stages group the workflow's own steps", () => {
  it("Workflow #27 waiting on its merge: Plan is the input file, Build is yours (Merge last), Ship waits, Live is not in this workflow", async () => {
    const { panelStages } = await import("../lib/work-stages");
    const s = panelStages(await item(), { checks: ["gwlint 9 → 0 open"] });
    expect(s.map((x) => [x.key, x.state])).toEqual([["plan", "done"], ["build", "you"], ["ship", "todo"], ["live", "absent"]]);
    expect(s[0].steps.map((x) => x.name)).toEqual(["Input file"]);
    expect(s[0].reason).toBe("lumen-pay.yaml taken in");
    expect(s[1].steps.map((x) => x.name)).toEqual(["Build", "Review", "Merge"]);
    expect(s[1].steps[2].you).toBe(true);
    expect(s[1].reason).toBe("gwlint 9 → 0 open");
    expect(s[2].reason).toBe("Starts after the merge");
    expect(s[3].reason).toBe("Not in this workflow");
    // Links: a started stage only, and to the screen its work lives on.
    expect(s[0].link?.screen).toBe("workflow");
    expect(s[1].link?.screen).toBe("workflow"); // the merge has no screen of its own
    expect(q(s[1].link!.href).get("wfr")).toBe("wfr_c6d43bb861484e81aa3a");
    expect(s[2].link).toBeNull();
    expect(s[3].link).toBeNull();
  });

  it("no step appears twice and every step lands in exactly one stage", async () => {
    const { panelStages } = await import("../lib/work-stages");
    const s = panelStages(await item());
    const names = s.flatMap((x) => x.steps.map((y) => y.name)).filter((n) => n !== "Input file");
    expect(names).toEqual(["Build", "Review", "Merge", "Staging", "Contract tests"]);
  });

  it("a failing build BOSS is retrying: Build red with its reason, chip 'Build failing', Watch live opens Build and test", async () => {
    const { panelStages, panelChip, watchLive, nextLine } = await import("../lib/work-stages");
    const w = await item({
      state: "running", status: "running", action_label: "", attempt_count: 2, agents: [{ name: "Helm", role_key: "engineering_captain", avatar: "" }],
      links: { ...LUMEN.links, merge_pr: null, decision: "" },
      steps: [step("build", "build", "build", "failed", { sub: "gwlint: 2 P0 still open after the agent's fix",
                    refs: { build: `?project_id=${P}&area=code_tasks&view=apps&build=crun_eb81d51b7a34441d8fc9` } }),
              step("merge", "merge", "build", "todo"), step("staging", "config_deploy", "ship", "todo")],
    });
    const s = panelStages(w);
    expect(s[1]).toMatchObject({ state: "fail", reason: "gwlint: 2 P0 still open after the agent's fix" });
    expect(panelChip(w, s)).toEqual({ word: "Build failing", tone: "red" });
    const watch = watchLive(w, s)!;
    expect(watch.screen).toBe("build");
    expect(q(watch.href).get("build")).toBe("crun_eb81d51b7a34441d8fc9");
    const next = nextLine(w, s);
    expect(next.tone).toBe("boss");
    expect(next.say).toMatch(/^BOSS is retrying build \(attempt 2\)$/);
    expect(next.buttons.map((b) => [b.label, b.kind])).toEqual([["Watch live", "primary"]]);
  });

  it("a panel meeting links the Convene panel screen; Watch live goes there while it runs", async () => {
    const { panelStages, watchLive } = await import("../lib/work-stages");
    const w = await item({
      state: "running", input_file: null, links: { ...LUMEN.links, merge_pr: null, pr: null, decision: "" },
      steps: [step("panel", "meeting", "plan", "now", { refs: { meeting: `?project_id=${P}&area=meetings&meeting=mrun_1` } }),
              step("approve_prd", null, "plan", "todo"), step("build", "build", "build", "todo")],
    });
    const s = panelStages(w);
    expect(s[0]).toMatchObject({ state: "live", link: { screen: "meeting", label: "Panel meeting" } });
    expect(q(watchLive(w, s)!.href).get("meeting")).toBe("mrun_1");
    expect(s[1]).toMatchObject({ state: "todo", reason: "Waits for Plan", link: null });
  });

  it("Ship links its deploy (or the project's Deployments) and Live links Observability, once started", async () => {
    const { panelStages } = await import("../lib/work-stages");
    const w = await item({
      state: "running", links: { ...LUMEN.links, merge_pr: null, decision: "" },
      steps: [step("build", "build", "build", "done"), step("deploy", "deploy", "ship", "now", { refs: { deploy: `?project_id=${P}&area=infra&view=deploys&deploy=sdep_7` } }),
              step("monitor", "monitor", "live", "done")],
    });
    const s = panelStages(w);
    expect([q(s[2].link!.href).get("area"), q(s[2].link!.href).get("deploy")]).toEqual(["infra", "sdep_7"]);
    expect([q(s[3].link!.href).get("area"), q(s[3].link!.href).get("project_id")]).toEqual(["observability", P]);
    const bare = panelStages(await item({ state: "running", steps: [step("deploy", "deploy", "ship", "now")], links: { ...LUMEN.links, merge_pr: null } }));
    expect([q(bare[2].link!.href).get("area"), q(bare[2].link!.href).get("tab")]).toEqual(["project", "deployments"]);
  });

  it("a merge the workflow has no step for is still Build's: the deploy that waits for it starts after the merge", async () => {
    const { panelStages } = await import("../lib/work-stages");
    const s = panelStages(await item({
      input_file: null, workflow: "Fast Fix / Hotfix",
      steps: [step("build", "build", "build", "done"), step("report", "report", "build", "done", { sub: "4 of 10 steps, 14.9 min working" }),
              step("deploy", "deploy", "ship", "waiting"), step("monitoring", "monitoring_pack", "live", "todo")],
    }));
    expect(s.map((x) => [x.key, x.state])).toEqual([["plan", "absent"], ["build", "you"], ["ship", "todo"], ["live", "todo"]]);
    expect(s[1].steps.map((x) => x.name)).toEqual(["Build", "Report", "Merge"]);
    expect(s[2].reason).toBe("Starts after the merge");
    expect(s[2].link).toBeNull();
  });

  it("a merged task never reads Build 'not reached', and a step that failed on the way is not red", async () => {
    const { panelStages } = await import("../lib/work-stages");
    const st = (key: string, state: string) => ({ key, label: key, state });
    const s = panelStages(await item({
      state: "done", status: "merged", input_file: null, action_label: "",
      links: { ...LUMEN.links, merge_pr: null, decision: "", merged_pr: { number: 18, external_url: LOCAL.replace("24", "18") } },
      stages: [st("plan", "done"), st("build", "done"), st("review", "done"), st("pr", "done"), st("ship", "skipped")],
      steps: [step("spec_review", "spec_review", "plan", "failed"), step("build", "build", "build", "todo"), step("deploy", "deploy", "ship", "todo")],
    }));
    expect(s.map((x) => [x.key, x.state])).toEqual([["plan", "done"], ["build", "done"], ["ship", "todo"], ["live", "absent"]]);
    expect(s[0].reason).toBe("Spec review failed; the work went on");
    expect(s[1].reason).toBe("Merged as Change #18");
    expect(s[1].link?.screen).toBe("build");
    expect(s[2].reason).toBe("Not reached");
  });

  it("a build with no workflow draws its stations as the stages", async () => {
    const { panelStages } = await import("../lib/work-stages");
    const st = (key: string, state: string) => ({ key, label: key, state });
    const w = await item({ source: "code_task", steps: undefined, input_file: null, state: "running",
                           links: { ...LUMEN.links, workflow_run: "", merge_pr: null, decision: "" },
                           stages: [st("plan", "skipped"), st("design", "skipped"), st("build", "now"), st("review", "todo"), st("pr", "todo"), st("ship", "skipped")] });
    const s = panelStages(w);
    expect(s.map((x) => x.state)).toEqual(["absent", "live", "absent", "absent"]);
    expect(s[1].link?.screen).toBe("build");
  });
});

describe("one 'what next' line", () => {
  it("ready to merge: amber Approve and merge goes to BOSS's merge decision, See Checks to the change's Checks", async () => {
    const { panelStages, nextLine, checksHrefOf } = await import("../lib/work-stages");
    const w = await item();
    const n = nextLine(w, panelStages(w), { checksHref: checksHrefOf(w) });
    expect(n.say).toBe("Ready to merge, your call");
    expect(n.tone).toBe("you");
    expect(n.buttons.map((b) => [b.label, b.kind])).toEqual([["See Checks", "plain"], ["Approve and merge", "amber"]]);
    expect(q(n.buttons[1].href).get("decision")).toBe("dec_599a1d759dc249ab947d");
    expect(q(n.buttons[0].href).get("tab")).toBe("checks");
  });

  it("done work has no buttons and nothing amber", async () => {
    const { panelStages, nextLine } = await import("../lib/work-stages");
    const w = await item({ state: "done", status: "merged", links: { ...LUMEN.links, merge_pr: null, merged_pr: { number: 24, external_url: LOCAL } } });
    const n = nextLine(w, panelStages(w));
    expect(n.buttons).toEqual([]);
    expect(n.say).toBe("Merged");
  });
});

describe("the Work list: one row per task, on tabs by who acts", () => {
  const NOW = Date.parse("2026-11-22T19:00:00Z");
  async function list() {
    const { parseWorkItem } = await import("../lib/project-experience");
    const mk = (o: Record<string, unknown>) => parseWorkItem({ ...LUMEN, steps: [], ...o });
    return [
      mk({}), // #27 needs you
      mk({ id: "wfr_26", run_number: 26, created_at: "2026-11-22T10:57:00Z", updated_at: "2026-11-22T11:10:00Z" }),
      mk({ id: "wfr_25", run_number: 25, state: "done", status: "cancelled", created_at: "2026-11-22T09:43:00Z", updated_at: "2026-11-22T09:50:00Z" }),
      mk({ id: "wfr_run", label: "Contract tests", title: "Contract tests", run_number: 23, state: "running", created_at: "2026-11-22T18:00:00Z", updated_at: "2026-11-22T18:30:00Z" }),
      mk({ id: "wfr_retry", label: "Clock API", title: "Clock API", run_number: 22, state: "failed", why: { code: "retrying", who: "boss", words: "Retrying", detail: "", since: null, etaMinutes: null },
           created_at: "2026-11-22T17:00:00Z", updated_at: "2026-11-22T17:40:00Z" }),
      mk({ id: "wfr_gave", label: "Fixture Clock API", title: "Fixture Clock API", run_number: 21, state: "failed", created_at: "2026-11-22T07:00:00Z", updated_at: "2026-11-22T07:40:00Z" }),
      mk({ id: "wfr_old", label: "Old ask", title: "Old ask", run_number: 9, state: "needs_you", created_at: "2026-11-10T07:00:00Z", updated_at: "2026-11-10T07:40:00Z" }),
      mk({ id: "wfr_done", label: "Demo clean-up", title: "Demo clean-up", run_number: 20, state: "done", status: "merged", created_at: "2026-11-22T06:00:00Z", updated_at: "2026-11-22T06:30:00Z" }),
      mk({ id: "wfr_child", label: "Something else", title: "Something else", run_number: 28, rerun_of: "wfr_done", state: "done", status: "merged",
           created_at: "2026-11-22T06:40:00Z", updated_at: "2026-11-22T06:50:00Z" }),
    ];
  }

  it("re-runs fold into one task (same title, or rerun_of), latest first", async () => {
    const { groupTasks } = await import("../lib/work-tasks");
    const tasks = groupTasks(await list());
    const lumen = tasks.find((t) => t.latest.label === "Lumen Pay tenant onboarding")!;
    expect([lumen.id, ...lumen.earlier.map((e) => e.id)]).toEqual(["wfr_c6d43bb861484e81aa3a", "wfr_26", "wfr_25"]);
    const cleanup = tasks.find((t) => t.earlier.some((e) => e.id === "wfr_done"))!;
    expect(cleanup.id).toBe("wfr_child");
    expect(tasks.length).toBe(6);
  });

  it("tabs: Needs you (incl. given up) · BOSS working · Done · Stopped (cancelled, older than 7 days)", async () => {
    const { bucketTasks, groupTasks } = await import("../lib/work-tasks");
    const b = bucketTasks(groupTasks(await list()), NOW);
    const ids = (k: keyof typeof b) => b[k].map((t) => t.id);
    // A failed run is BOSS giving up (Needs you); a failure BOSS still retries is a running item.
    expect(ids("needs_you")).toEqual(["wfr_c6d43bb861484e81aa3a", "wfr_retry", "wfr_gave"]);
    expect(ids("working")).toEqual(["wfr_run"]);
    expect(ids("done")).toEqual(["wfr_child"]);
    expect(ids("stopped")).toEqual(["wfr_old"]);
  });

  it("an unanswered ask counts its 7 days from when it became ready, not from the engine's last look", async () => {
    const { tabOf } = await import("../lib/work-tasks");
    const { parseWorkItem } = await import("../lib/project-experience");
    const w = parseWorkItem({ ...LUMEN, updated_at: "2026-11-22T18:00:00Z",
                              steps: [step("build", "build", "build", "done", { completed_at: "2026-11-08T17:25:00Z" }), step("merge", "merge", "build", "waiting")] });
    expect(tabOf({ id: w.id, latest: w, earlier: [] }, NOW)).toBe("stopped");
  });

  it("the Needs-you count is one number: a failed build's card stands for its row, a loose card counts once", async () => {
    const { needsYouTab } = await import("../lib/work-tasks");
    const items = await list();
    const cards = [
      { id: "failed:wfr_gave", kind: "failed", label: "Build failed", detail: "", mini: [], work_id: "wfr_gave", action: null },
      { id: "dec_merge", kind: "merge", label: "Merge", detail: "", mini: [], work_id: "wfr_c6d43bb861484e81aa3a", action: null },
      { id: "dec_prodhost", kind: "setup", label: "Pick a prod host", detail: "", mini: [], action: null },
    ] as never;
    const n = needsYouTab(items, cards, NOW);
    expect(n.tasks.map((t) => t.id)).toEqual(["wfr_c6d43bb861484e81aa3a", "wfr_retry"]);
    expect(n.loose.map((c) => c.id)).toEqual(["failed:wfr_gave", "dec_prodhost"]);
    expect(n.count).toBe(4);
    // The tabs come from the same read: Needs you is exactly these rows.
    expect(n.buckets.needs_you.map((t) => t.id)).toEqual(n.tasks.map((t) => t.id));
  });
});

describe("review findings (independent reviewer, 2026-11-22)", () => {
  it("See Checks keeps the change page's path: /changes/<project>/<n>?tab=checks, never the console root", async () => {
    const { pxHref } = await import("../lib/project-experience");
    const { checksHrefOf } = await import("../lib/work-stages");
    const h = checksHrefOf(await item());
    expect(h).toBe("/changes/northgate-gateway/24?tab=checks");
    expect(pxHref(h, "?org=org_boss&project_id=northgate-gateway")).toBe(h);
  });

  it("a running run held at a person's gate (a deploy approval) is the person's move, never BOSS working", async () => {
    const { panelStages, panelChip, nextLine, watchLive } = await import("../lib/work-stages");
    const { tabOf } = await import("../lib/work-tasks");
    const w = await item({ state: "running", action_label: "", links: { ...LUMEN.links, merge_pr: null, decision: `?project_id=${P}&area=decisions&decision=dec_dep` },
                           steps: [step("build", "build", "build", "done", { completed_at: "2026-11-22T17:00:00Z" }), step("deploy", "deploy", "ship", "waiting")] });
    const s = panelStages(w);
    expect(s[2].state).toBe("you");
    expect(panelChip(w, s).tone).toBe("amber");
    expect(watchLive(w, s)).toBeNull();
    expect(nextLine(w, s).buttons.map((b) => [b.label, b.kind])).toEqual([["Decide", "amber"]]);
    expect(tabOf({ id: w.id, latest: w, earlier: [] }, Date.parse("2026-11-22T19:00:00Z"))).toBe("needs_you");
  });

  it("an earlier attempt still waiting on a merge keeps its task on Needs you; a failed build is never 'working'", async () => {
    const { tabOf } = await import("../lib/work-tasks");
    const NOW2 = Date.parse("2026-11-22T19:00:00Z");
    const running = await item({ state: "running", steps: [step("build", "build", "build", "now")] });
    const older = await item({ id: "wfr_26", steps: [step("build", "build", "build", "done", { completed_at: "2026-11-22T11:00:00Z" }), step("merge", "merge", "build", "waiting")] });
    expect(tabOf({ id: running.id, latest: running, earlier: [older] }, NOW2)).toBe("needs_you");
    const failed = await item({ state: "failed", updated_at: "2026-11-22T18:00:00Z", steps: [], why: { code: "failed", who: "boss", words: "Failed", detail: "", since: null, etaMinutes: null } });
    expect(tabOf({ id: failed.id, latest: failed, earlier: [] }, NOW2)).toBe("needs_you");
  });

  it("an incident's investigation is its Plan, so its stages read in order", async () => {
    // engine STAGE_OF_CAPABILITY maps investigate_incident to plan (engine test covers the map)
    const { panelStages } = await import("../lib/work-stages");
    const s = panelStages(await item({ state: "running", input_file: null, links: { ...LUMEN.links, merge_pr: null, decision: "" },
      steps: [step("investigate", "investigate_incident", "plan", "done"), step("build", "build", "build", "now"), step("verify", "verify_recovery", "live", "todo")] }));
    expect(s.map((x) => x.state)).toEqual(["done", "live", "absent", "todo"]);
  });
});

describe("the panel itself", () => {
  it("header: title, the workflow's name, Workflow #27 · Build eb81d5 · Change #24, one chip, real date-times; earlier attempts latest first", async () => {
    const { WorkPanel } = await import("../app/projects/work-panel");
    const { groupTasks } = await import("../lib/work-tasks");
    const w = await item();
    const older = await item({ id: "wfr_26", run_number: 26, created_at: "2026-11-22T10:57:00Z", updated_at: "2026-11-22T11:10:00Z",
                               links: { ...LUMEN.links, workflow_run: `?project_id=${P}&area=delivery_workflows&wfr=wfr_26`, pr: { number: 23, external_url: LOCAL.replace("24", "23") },
                                        merge_pr: { number: 23, external_url: LOCAL.replace("24", "23") } } });
    const task = groupTasks([w, older])[0];
    const ctx = { projectId: P, actor: "ceo", demo: false, open: () => {}, overview: { project: { repos: [] } } };
    await mount(<WorkPanel ctx={ctx as never} w={w} task={task} />);
    expect($('[data-testid="wp-title"]')!.textContent).toBe("Lumen Pay tenant onboarding");
    expect($('[data-testid="wp-workflow"]')!.textContent).toBe("Onboard a config file");
    expect($('[data-testid="wp-link-run"]')!.textContent).toBe("Workflow #27");
    expect($('[data-testid="wp-link-build"]')!.textContent).toBe("Build eb81d5");
    expect(q($('[data-testid="wp-link-build"]')!.getAttribute("href")!).get("build")).toBe("crun_eb81d51b7a34441d8fc9");
    expect($('[data-testid="wp-link-change"]')!.textContent).toBe("Change #24");
    expect($('[data-testid="wp-chip"]')!.textContent).toBe("Needs you");
    expect($('[data-testid="wp-times"]')!.textContent).toMatch(/Kicked off .*Nov 22.*Ready .*Waiting on you .*Cost \$1\.43.*Attempt 2/);
    // Stage cards: a link only where the stage started.
    expect($$('[data-testid^="wp-stage-"][data-testid$="-link"]').map((a) => a.getAttribute("data-testid"))).toEqual(["wp-stage-plan-link", "wp-stage-build-link"]);
    expect($('[data-testid="wp-stage-live-why"]')!.textContent).toBe("Not in this workflow");
    // One amber button, the person's.
    expect($$('[data-testid="wp-next"] .px-btn[data-kind="amber"]').map((b) => b.textContent)).toEqual(["Approve and merge"]);
    // Iterations, latest first, each its own links.
    expect($$('[data-testid="wp-iterations"] .wp2-iter-name').map((b) => b.textContent)).toEqual(["Workflow #27", "Workflow #26"]);
    expect(q($('[data-testid="wp-iter-wfr_26-run"]')!.getAttribute("href")!).get("wfr")).toBe("wfr_26");
    expect($('[data-testid="wp-iter-wfr_26-change"]')!.textContent).toBe("Change #23");
    await act(async () => {});
  });
});
