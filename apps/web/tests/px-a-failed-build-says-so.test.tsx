// @vitest-environment happy-dom
/**
 * A FAILED BUILD SAYS SO (recipe-box, 2026-11-18). The Easy Mode build crun_6883ec7cbd67401a9ce8
 * failed ("git add failed: … Permission denied") and the project page still said "Working",
 * "Nothing running", a Recent-work row with ticks, and Needs you without it; the Builds picker
 * counted a failed build as "Building". Every surface reads the engine's one answer
 * (work.failed_build): red "Build failed", the reason in words, Retry (the build's resume) and
 * See what happened (its page).
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const retry = vi.fn(async (_form: FormData) => ({ ok: true, runId: "crun_retry1", attempt: 2 }));
vi.mock("../app/factory/floor-actions", () => ({
  waiveChecksAction: vi.fn(), startDirectChangeAction: vi.fn(),
  retryBuildAction: (form: FormData) => retry(form),
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { ProjectPage } = await import("../app/projects/project-page");
const { ProjectStrip } = await import("../app/project-strip");
const { AttentionCard, kindWord } = await import("../app/agents/attention-card");
const { parseAttention } = await import("../lib/agents-live-board");
const { FX_ONBOARDING, FX_OVERVIEW, FX_SUMMARY, FX_WORK } = await import("../lib/project-experience-fixtures");
const { mountAs, stubFetch, tid, unmount } = await import("./_helpers/multirepo-fixtures");

const P = "recipe-box-fd3ab4a3";
const RUN = "crun_6883ec7cbd67401a9ce8";
const REASON = "It couldn't save its changes: git was not allowed to write to the build's copy of the repo (permission denied).";
const SEE = `?project_id=${P}&area=code_tasks&build=${RUN}`;
const WORK_HREF = `?project_id=${P}&area=project&tab=work&drawer=build%3A${RUN}`;
const settle = async (n = 8) => { for (let i = 0; i < n; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };
const press = async (el: Element | null) => { if (!el) throw new Error("nothing to press"); await act(async () => { (el as HTMLElement).click(); }); await settle(); };
const params = () => new URLSearchParams(window.location.search);

/** The engine's answer for the real run (GET /v1/projects/recipe-box-…/overview, 2026-11-18). */
const FAILED = { id: RUN, label: "Recipe box", build_id: RUN, reason: REASON, at: "2026-11-18T15:51:16.904643+00:00", word: "Build failed",
                 retry: { run_id: RUN, href: `/v1/code-tasks/${RUN}/retry`, method: "POST" }, fix: null, see_href: SEE, work_href: WORK_HREF, beside: false };
const ROW = {
  ...FX_WORK.items[1], id: RUN, label: "Recipe box", kind: "feature", state: "failed", status: "failed", source: "code_task", age: "12m",
  stages: [{ key: "plan", label: "P", name: "Plan", state: "skipped" }, { key: "design", label: "D", name: "Decide", state: "skipped" },
           { key: "build", label: "B", name: "Build", state: "done" }, { key: "review", label: "R", name: "Review", state: "done" },
           { key: "pr", label: "PR", name: "PR", state: "failed" }, { key: "ship", label: "S", name: "Ship", state: "skipped" }],
  links: { workflow_run: null, build: SEE, pr: null, deploy: null, preview: null }, cost_usd: 2.28,
  detail: "git add failed: fatal: .git/index: index file open failed: Permission denied [substrate=local_subprocess]",
  build_id: RUN, href: WORK_HREF,
};
const OVERVIEW = {
  ...FX_OVERVIEW,
  project: { ...FX_OVERVIEW.project, id: P, name: "Recipe box", initials: "RB", status_label: "Build failed", live_url: "", health: "unknown", build_failed: FAILED },
  journey: FX_OVERVIEW.journey.map((n) => (n.key === "working" ? { ...n, state: "failed", sub: "Build failed", detail: `Build failed: ${REASON}` } : n)),
  needs_you: [
    { id: `failed:${RUN}`, kind: "failed", label: "Build failed: Recipe box", detail: REASON, mini: [], work_id: RUN,
      retry: FAILED.retry, see_href: SEE, action: null },
    { id: "onb:deploy_target", kind: "setup", label: "Pick a staging host", detail: "Use the verified shared host for staging.", mini: [],
      action: { label: "Use for staging", href: `?project_id=${P}&area=infra&add=target`, decision_id: null } },
  ],
  tiles: { ...FX_OVERVIEW.tiles, running: { count: 0, current: null, href: "", failed: { id: RUN, label: "Recipe box", reason: REASON, href: WORK_HREF } } },
  recent_work: [ROW],
};

function engine(overview: unknown = OVERVIEW) {
  return stubFetch((c) => {
    if (c.method !== "GET") return { body: { ok: true } };
    if (c.path === "/v1/projects/summary") return { body: FX_SUMMARY };
    const m = c.path.match(/^\/v1\/projects\/([^/]+)\/(overview|work|onboarding)$/);
    if (m) return { body: ({ overview, work: { items: [ROW] }, onboarding: { ...FX_ONBOARDING, state: "ready" } } as Record<string, unknown>)[m[2]] };
    return undefined;
  });
}

async function openProject(tab = "") {
  window.history.replaceState(null, "", `/?org=org_1&project_id=${P}&area=project${tab ? `&tab=${tab}` : ""}`);
  await mountAs(<ProjectPage projectId={P} actor="ceo" orgId="org_1" />, "owner");
  await settle(10);
}

beforeEach(() => { window.history.replaceState(null, "", "/"); retry.mockClear(); });
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

describe("the project page says a build failed", () => {
  it("the header chip is red Build failed with its reason, never Working, and opens the build", async () => {
    engine();
    await openProject();
    const chip = tid("px-head-status")!;
    expect(chip.textContent?.trim()).toBe("Build failed");
    expect(chip.getAttribute("data-tone")).toBe("red");
    expect(chip.getAttribute("title")).toContain(REASON);
    expect(chip.textContent).not.toContain("Working");
    await press(chip);
    // It opens the build's work in the Work panel (v2), whose Build stage links the build.
    expect(params().get("drawer")).toMatch(/^work:crun_/);
    expect(tid("px-drawer-work")).not.toBeNull();
  });

  it("the journey's Working node is failed and the Running tile names the failure", async () => {
    engine();
    await openProject();
    expect(tid("px-journey-working")!.getAttribute("data-s")).toBe("failed");
    const tile = tid("px-tile-running")!;
    expect(tile.textContent).toContain("Last build failed");
    expect(tile.textContent).toContain(REASON);
    expect(tile.textContent).not.toContain("Nothing running");
  });

  it("the Recent-work row says Failed in red, its dot bad, never a quiet Feature row", async () => {
    engine();
    await openProject();
    const row = tid(`px-ov-work-${RUN}`)!;
    expect(row.getAttribute("data-s")).toBe("failed");
    expect(row.querySelector(".px-dot")!.getAttribute("data-s")).toBe("bad");
    expect(tid(`px-ov-failed-${RUN}`)!.textContent).toBe("Failed");
  });

  it("Work's Needs-you group carries it: the reason, Retry (the build's resume) and See what happened", async () => {
    engine();
    // The Overview counts it and links to the group (operator 2026-11-21: Needs you once per screen).
    await openProject();
    expect(tid("px-ov-needs-open")!.textContent).toContain("2 items wait on you");
    expect(tid("px-needs-bar")).toBeNull();
    expect(tid("px-tab-work-needs")!.textContent).toBe("2");
    await press(tid("px-ov-needs-open")!);
    expect(params().get("tab")).toBe("work");
    const card = tid(`px-work-needs-failed:${RUN}`)!;
    expect(card.hasAttribute("data-failed")).toBe(true);
    expect(tid(`px-work-needs-failed:${RUN}-actions`)!.parentElement).toBe(card);
    expect(card.textContent).toContain("Build failed: Recipe box");
    expect(tid(`px-work-needs-failed:${RUN}-why`)!.textContent).toBe(REASON);
    expect(card.textContent).not.toMatch(/substrate|fatal:/);
    const see = tid(`px-work-needs-failed:${RUN}-see`)!;
    expect(see.getAttribute("href")).toContain(`area=code_tasks&build=${RUN}`);
    await press(tid(`px-work-needs-failed:${RUN}-retry`));
    expect(retry).toHaveBeenCalledTimes(1);
    expect(retry.mock.calls[0][0].get("run_id")).toBe(RUN);
    expect(tid(`px-work-needs-failed:${RUN}-retry-said`)!.textContent).toBe("Attempt 2 started");
    // Its title opens the build's row, not an empty needs drawer.
    await press(card.querySelector("button.px-wk-t")!);
    // It opens the build's work in the Work panel (v2), whose Build stage links the build.
    expect(params().get("drawer")).toMatch(/^work:crun_/);
    expect(tid("px-drawer-work")).not.toBeNull();
  });
});

const LOGIN_FIX = { label: "Sign in again in Setup › Models", detail: "Reconnect the Claude Code subscription in Repo and models, then retry.",
                    href: `?project_id=${P}&area=setup&focus=models` };
const NEEDS_HUMAN = {
  ...OVERVIEW,
  project: { ...OVERVIEW.project, build_failed: { ...FAILED, fix: LOGIN_FIX,
                                                  reason: "BOSS couldn't run claude_code: Not logged in · Please run /login." } },
  needs_you: [{ ...OVERVIEW.needs_you[0], fix: LOGIN_FIX }, OVERVIEW.needs_you[1]],
};

describe("a failure only a person can fix", () => {
  it("names the fix first with its link, and keeps Retry beside it, secondary, for after", async () => {
    engine(NEEDS_HUMAN);
    await openProject("work");
    const fix = tid(`px-work-needs-failed:${RUN}-fix`)!;
    expect(fix.textContent).toBe("Sign in again in Setup › Models");
    expect(fix.getAttribute("href")).toContain("area=setup");
    expect(fix.getAttribute("data-kind")).toBe("primary");
    const again = tid(`px-work-needs-failed:${RUN}-retry`)!;
    expect(again.getAttribute("data-kind")).toBeNull();
    // The fix comes first in the card's answers.
    const answers = tid(`px-work-needs-failed:${RUN}-actions`)!;
    expect(answers.firstElementChild).toBe(fix);
    expect(tid(`px-work-needs-failed:${RUN}-fixwhy`)!.textContent).toContain("Reconnect");
  });

  it("the needs drawer draws the same answers, never a link to the API's retry URL", async () => {
    engine();
    window.history.replaceState(null, "", `/?org=org_1&project_id=${P}&area=project&drawer=needs:failed:${RUN}`);
    const { ProjectPage: Page } = await import("../app/projects/project-page");
    await mountAs(<Page projectId={P} actor="ceo" orgId="org_1" />, "owner");
    await settle(10);
    const drawer = tid("px-drawer-needs")!;
    expect(drawer.textContent).toContain(REASON);
    expect(drawer.querySelector('a[href*="/v1/code-tasks"]')).toBeNull();
    await press(tid("px-drawer-failed-retry"));
    expect(retry).toHaveBeenCalledTimes(1);
  });
});

describe("on a live project", () => {
  it("the failure chip sits beside the stage chip, never in its place", async () => {
    engine({ ...OVERVIEW, project: { ...OVERVIEW.project, status_label: "On staging", stage: "staging", build_failed: { ...FAILED, beside: true, word: "Follow-up failed" } } });
    await openProject();
    expect(tid("px-head-status")!.textContent).toContain("On staging");
    expect(tid("px-head-failed")!.textContent?.trim()).toBe("Follow-up failed");
  });
});

/** The laptop's lineage (2026-11-18): the build crun_d8b… COMPLETED (change #1 open, its merge
 *  pending) and its automatic follow-up crun_47c3… FAILED; the first build crun_6883… failed
 *  and was replaced by that relaunch. */
const ROOT = "crun_d8b751312b2b4f43af36";
const FOLLOW = "crun_47c3aea2add242b3ad4a";
const FOLLOW_SEE = `?project_id=${P}&area=code_tasks&build=${FOLLOW}`;
const LINEAGE_FAILED = { id: ROOT, label: "Recipe box", build_id: FOLLOW, word: "Follow-up failed", beside: false,
  reason: "BOSS could not collect this build's changes.", fix: null, see_href: FOLLOW_SEE,
  retry: { run_id: FOLLOW, href: `/v1/code-tasks/${FOLLOW}/retry`, method: "POST" },
  work_href: `?project_id=${P}&area=project&tab=work&drawer=build%3A${ROOT}` };
const LINEAGE_ROW = { ...ROW, id: ROOT, build_id: FOLLOW, href: LINEAGE_FAILED.work_href,
  stages: ROW.stages.map((st) => (st.key === "pr" ? { ...st, state: "waiting" } : st)) };
const REPLACED_ROW = { ...ROW, state: "done", replaced_by: { id: ROOT, label: "Recipe box", href: LINEAGE_FAILED.work_href } };
const LINEAGE = {
  ...OVERVIEW,
  project: { ...OVERVIEW.project, status_label: "Follow-up failed", build_failed: LINEAGE_FAILED },
  needs_you: [{ id: `failed:${ROOT}`, kind: "failed", label: "Follow-up failed: Recipe box", detail: LINEAGE_FAILED.reason, mini: [],
                work_id: ROOT, retry: LINEAGE_FAILED.retry, see_href: FOLLOW_SEE, action: null }],
  recent_work: [LINEAGE_ROW, REPLACED_ROW],
};
function lineageEngine() {
  return stubFetch((c) => {
    if (c.method !== "GET") return { body: { ok: true } };
    if (c.path === "/v1/projects/summary") return { body: FX_SUMMARY };
    if (c.path.endsWith("/story")) return { body: { id: ROOT, stages: [{ key: "pr", name: "Merge", state: "waiting", sub: "Only a person merges" }] } };
    const m = c.path.match(/^\/v1\/projects\/([^/]+)\/(overview|work|onboarding)$/);
    if (m) return { body: ({ overview: LINEAGE, work: { items: [LINEAGE_ROW, REPLACED_ROW] }, onboarding: { ...FX_ONBOARDING, state: "ready" } } as Record<string, unknown>)[m[2]] };
    return undefined;
  });
}

describe("a completed build with a failed follow-up (the laptop's lineage)", () => {
  it("the card's Retry resumes the FAILED follow-up, never the completed build", async () => {
    lineageEngine();
    await openProject("work");
    const r = tid(`px-work-needs-failed:${ROOT}-retry`)!;
    expect(r.getAttribute("data-run-id")).toBe(FOLLOW);
    await press(r);
    expect(retry).toHaveBeenCalledTimes(1);
    expect(retry.mock.calls[0][0].get("run_id")).toBe(FOLLOW);
  });

  it("the card's title opens the failed work itself, and Retry stays on the card", async () => {
    lineageEngine();
    await openProject("work");
    expect(tid(`px-work-needs-failed:${ROOT}-see`)!.getAttribute("href")).toContain(`build=${FOLLOW}`);
    await press(tid(`px-work-needs-failed:${ROOT}`)!.querySelector("button.px-wk-t")!);
    expect(params().get("drawer")).toMatch(/^work:crun_/);
    expect(tid("px-drawer-work")).not.toBeNull();
  });

  it("a refusal whose cause is gone reads its words, faint, never a red Failed", async () => {
    const REFUSED = "crun_0459f6c96d7f41adac27";
    const done = { ...ROW, id: REFUSED, state: "done", resolved: { words: "Not needed: change #1 was merged." } };
    stubFetch((c) => {
      if (c.method !== "GET") return { body: { ok: true } };
      if (c.path === "/v1/projects/summary") return { body: FX_SUMMARY };
      const m = c.path.match(/^\/v1\/projects\/([^/]+)\/(overview|work|onboarding)$/);
      const ov = { ...OVERVIEW, project: { ...OVERVIEW.project, status_label: "Working", build_failed: null }, needs_you: [], recent_work: [done] };
      if (m) return { body: ({ overview: ov, work: { items: [done] }, onboarding: { ...FX_ONBOARDING, state: "ready" } } as Record<string, unknown>)[m[2]] };
      return undefined;
    });
    await openProject();
    expect(tid(`px-ov-resolved-${REFUSED}`)!.textContent).toBe("Not needed: change #1 was merged.");
    expect(tid(`px-ov-failed-${REFUSED}`)).toBeNull();
    expect(tid("px-head-status")!.textContent).not.toContain("failed");
  });

  it("a failed build a later one replaced reads Replaced, faint, never a red Failed", async () => {
    lineageEngine();
    await openProject();
    const row = tid(`px-ov-work-${RUN}`)!;
    expect(row.hasAttribute("data-replaced")).toBe(true);
    expect(tid(`px-ov-replaced-${RUN}`)!.textContent).toBe("Replaced by a later build");
    expect(tid(`px-ov-failed-${RUN}`)).toBeNull();
    expect(row.querySelector(".px-dot")!.getAttribute("data-s")).toBeNull();
  });
});

describe("the Builds picker never counts a failed build as building", () => {
  it("a STUCK (failed) app is under Failed, not Building, and its row says Failed", async () => {
    stubFetch((c) => c.path === "/api/projects-strip" ? { body: { total: 2, projects: [
      { id: "app_rb", title: "Recipe box", status: "STUCK", stage: "BUILD", pct: 64, latest_run_id: RUN },
      { id: "app_ok", title: "Lead inbox", status: "RUNNING", stage: "BUILD", pct: 40, latest_run_id: "crun_ok" },
    ] } } : undefined);
    await mountAs(<ProjectStrip projectId={P} chip />, "owner");
    await settle(10);
    await press(tid("bx-strip-trigger"));
    const count = (f: string) => tid(`bx-strip-filter-${f}`)!.querySelector(".bx-strip-filter-n")!.textContent;
    expect(count("running")).toBe("1");
    expect(count("failed")).toBe("1");
    expect(tid("bx-strip-filter-failed")!.textContent).toContain("Failed");
    expect(tid(`bx-strip-failed-app_rb`)!.textContent).toBe("Failed");
    expect(tid("bx-strip-app_rb")!.textContent).not.toContain("64%");
  });
});

describe("the Command Center's card for a failed build", () => {
  const item = parseAttention({
    id: `build:${RUN}:failed`, kind: "build_failed", title: "Build failed: Recipe box", detail: REASON,
    answers: [{ key: "retry", label: "Retry", enabled: true }],
    build: { run_id: RUN, app: "Recipe box", where: "Build failed", failing: [], retryable: true },
  });

  it("a stack BOSS can't build shows what it needs, and Retry disabled with its reason", async () => {
    const { mount } = await import("./_helpers/live-render");
    const human = parseAttention({ ...JSON.parse(JSON.stringify({ id: `build:${RUN}:failed`, kind: "build_failed", title: "Build failed: Recipe box", detail: "x" })),
      answers: [], build: { run_id: RUN, app: "Recipe box", where: "Build failed", failing: [], retryable: false,
                            retry_reason: "BOSS can't build Ruby or PHP: retrying fails the same way",
                            fix: { label: "See what it needs", detail: "BOSS can't build Ruby or PHP.", href: "", command: "" } } });
    await mount(<AttentionCard item={human} projectId={P} actor="ceo" />);
    expect(tid("ag-att-fix")!.textContent).toContain("See what it needs");
    const r = tid("ag-att-retry") as HTMLButtonElement;
    expect(r.disabled).toBe(true);
    expect(r.getAttribute("title")).toContain("fails the same way");
  });

  it("is called Failed and offers Retry and See what happened", async () => {
    const { mount } = await import("./_helpers/live-render");
    await mount(<AttentionCard item={item} projectId={P} actor="ceo" />);
    expect(kindWord(item)).toBe("Failed");
    expect(tid("ag-att-see")!.getAttribute("href")).toContain(`build=${RUN}`);
    await press(tid("ag-att-retry"));
    expect(retry).toHaveBeenCalledTimes(1);
    expect(retry.mock.calls[0][0].get("run_id")).toBe(RUN);
  });
});
