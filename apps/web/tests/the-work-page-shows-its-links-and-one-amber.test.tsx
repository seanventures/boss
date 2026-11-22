// @vitest-environment happy-dom
/**
 * THE WORK PAGE SHOWS ITS LINKS, IN ONE AMBER (operator 2026-11-21, HelpDesk › Work):
 *
 *   "The page is full of yellow color. I dont see any run, build, preview etc in the UI … at least
 *    before I could see the run, build etc.. and we need to have individual run for every run
 *    that's displayed in the work task.. and overall run, build and change should be displayed at
 *    the top whatever is latest."
 *
 * The row's chips went in the Work panel rebuild (#246, a2d9d5866). Now:
 *   - every row shows its own Run #N · Build · PR/Change #N · Preview as short muted chips going
 *     where the Work panel's header goes; a raw `crun_` id is only ever the tooltip;
 *   - a "Latest" strip at the top names the project's newest Run, Build, Change and Preview;
 *   - the header's status is one short line with a "Why?" toggle, never a paragraph of engine
 *     prose with raw ids;
 *   - amber is the Needs-you signal only: no amber button, tag or rail on a row.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { $, $$, mount, unmount } from "./_helpers/live-render";
import { act } from "react";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
beforeEach(() => { vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 404 }))); });
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const NOW = Date.parse("2026-11-21T12:00:00Z");
const GH = "https://github.com/acme-demo/helpdesk/pull/5";
const BASE = {
  kind: "feature", age: "5m", attempts: [], agents: [], stages: [], cost_usd: 0,
  links: { decision: "", workflow_run: "", build: "", pr: null, merge_pr: null, merged_pr: null, deploy: "", preview: "" },
};
const RAW = [
  { ...BASE, id: "wfr_merge5", label: "Bump minimatch", state: "needs_you", action_label: "Merge", run_number: 7,
    created_at: "2026-11-21T10:00:00Z", updated_at: "2026-11-21T11:50:00Z",
    links: { ...BASE.links, decision: "?project_id=hd&area=decisions&decision=dec_1",
             workflow_run: "?project_id=hd&area=delivery_workflows&wfr=wfr_merge5",
             build: "?project_id=hd&area=code_tasks&build=crun_ad50ce9f01e14534b230",
             pr: { number: 5, external_url: GH }, merge_pr: { number: 5, external_url: GH, failed: null } } },
  { ...BASE, id: "wfr_older", label: "Queue routing", state: "done", run_number: 3,
    created_at: "2026-11-07T10:00:00Z", updated_at: "2026-11-07T12:00:00Z",
    links: { ...BASE.links, workflow_run: "?project_id=hd&area=delivery_workflows&wfr=wfr_older",
             build: "?project_id=hd&area=code_tasks&build=crun_0011aabbccdd",
             merged_pr: { number: 1, external_url: "https://github.com/acme-demo/helpdesk/pull/1" },
             preview: "/api/previews/prev_9/open" } },
  { ...BASE, id: "wi_plan", label: "Add CI", state: "planned", created_at: "2026-11-20T10:00:00Z", updated_at: "2026-11-20T10:00:00Z" },
];

async function items() {
  const { parseWorkItem } = await import("../lib/project-experience");
  return RAW.map(parseWorkItem);
}
const ctx = { projectId: "hd", actor: "ceo", refreshWork: () => {}, open: () => {}, overview: { project: { repos: [] } } };

describe("every work row shows its own links", () => {
  it("Run #N · Build · PR #N as short chips, the panel header's targets, ids only on hover", async () => {
    const { WorkRow } = await import("../app/projects/tab-work");
    const { workLinksOf } = await import("../app/projects/work-panel");
    const [w] = await items();
    await mount(<WorkRow w={w} ctx={ctx as never} />);
    const chips = $$('[data-testid="px-work-links-wfr_merge5"] a');
    expect(chips.map((a) => a.textContent)).toEqual(["Run #7", "Build", "PR #5"]);
    const panel = workLinksOf(w, null, null, "hd");
    expect($('[data-testid="px-work-run-wfr_merge5"]')!.getAttribute("href")).toContain("wfr=wfr_merge5");
    expect(panel.run).toContain("wfr=wfr_merge5");
    expect($('[data-testid="px-work-build-wfr_merge5"]')!.getAttribute("href")).toContain(`build=${panel.build!.id}`);
    expect($('[data-testid="px-work-build-wfr_merge5"]')!.getAttribute("title")).toContain("crun_ad50ce9f01e14534b230");
    expect($('[data-testid="px-work-change-wfr_merge5"]')!.getAttribute("href")).toBe("/changes/github/acme-demo/helpdesk/5");
    // Never a raw id as words.
    expect($('[data-testid="px-work-links-wfr_merge5"]')!.textContent).not.toMatch(/crun_|wfr_/);
    // The primary action stays on the right, after the chips.
    const row = $('[data-testid="px-work-wfr_merge5"]')!;
    const kids = Array.from(row.children);
    expect(kids.indexOf(row.querySelector(".wr-links")!)).toBeLessThan(kids.indexOf(row.querySelector(".wr-act")!));
  });

  it("shows only the links that exist: a merged row says its merged PR and its preview; a planned row none", async () => {
    const { WorkRow } = await import("../app/projects/tab-work");
    const [, done, planned] = await items();
    await mount(<><WorkRow w={done} ctx={ctx as never} /><WorkRow w={planned} ctx={ctx as never} /></>);
    expect($$('[data-testid="px-work-links-wfr_older"] a').map((a) => a.textContent)).toEqual(["Run #3", "Build", "PR #1", "Preview"]);
    expect($('[data-testid="px-work-links-wi_plan"]')).toBeNull();
  });
});

describe("the Latest strip", () => {
  it("names the newest Run, Build, Change and Preview, each a link with a status dot and time ago", async () => {
    const { LatestStrip } = await import("../app/projects/tab-work");
    await mount(<LatestStrip items={await items()} now={NOW} />);
    const strip = $('[data-testid="px-work-latest"]')!;
    expect(strip).not.toBeNull();
    const run = $('[data-testid="px-work-latest-run"]')!;
    expect(run.textContent).toContain("Run #7");
    expect(run.textContent).toContain("10m ago");
    expect(run.textContent).not.toMatch(/Run Run/);
    expect(run.getAttribute("href")).toContain("wfr=wfr_merge5");
    expect(run.querySelector(".px-dot")!.getAttribute("data-s")).toBe("you");
    expect($('[data-testid="px-work-latest-build"]')!.getAttribute("href")).toContain("crun_ad50ce9f01e14534b230");
    expect($('[data-testid="px-work-latest-change"]')!.textContent).toContain("PR #5");
    // Only the older work has a preview: Latest still finds it, with its own (done) dot.
    const prev = $('[data-testid="px-work-latest-preview"]')!;
    expect(prev.getAttribute("href")).toContain("prev_9");
    expect(prev.querySelector(".px-dot")!.getAttribute("data-s")).toBe("done");
    expect(prev.textContent).toContain("14d ago");
    expect(strip.textContent).not.toMatch(/crun_|wfr_/);
  });

  it("falls back to the project's own app preview when no work carries one; stalled work is never amber", async () => {
    const { LatestStrip } = await import("../app/projects/tab-work");
    const [merge] = await items();
    const app = { run_id: "wfr_x", name: "HelpDesk", title: "", version: "main", version_label: "Main", version_words: "", pr_number: null, pr_url: "",
                  preview_id: "prev_1", state: "running", words: "", href: "/api/previews/prev_1/open", error: "", recreate: false, live_url: "" };
    await mount(<LatestStrip items={[{ ...merge, state: "stalled" }]} now={NOW} app={app as never} />);
    const prev = $('[data-testid="px-work-latest-preview"]')!;
    expect(prev.getAttribute("href")).toContain("prev_1");
    expect(prev.querySelector(".px-dot")!.getAttribute("data-s")).toBe("done");
    expect($('[data-testid="px-work-latest-run"] .px-dot')!.getAttribute("data-s")).toBeNull();
  });

  it("is absent when no work has a link yet", async () => {
    const { LatestStrip } = await import("../app/projects/tab-work");
    const [, , planned] = await items();
    await mount(<LatestStrip items={[planned]} now={NOW} />);
    expect($('[data-testid="px-work-latest"]')).toBeNull();
  });
});

const REASON = "the deploy failed (acme-demo/helpdesk has no boss.deploy.yaml at 7dd457ab5876), and BOSS's repair build "
  + "crun_ad50ce9f01e14534b230 did not pass: finished as 'blocked' — BOSS did not fix it by itself: The failure is an "
  + "authority/permission gap, not an app or BOSS code bug: the build this deploy continues (crun_de5396b5a6b04224a158) "
  + "and its entire lineage were started by BOSS or a key with no person's grant";

function headerCtx(over: Record<string, unknown> = {}) {
  return {
    projectId: "hd", demo: true, onboarding: null, tab: vi.fn(), open: vi.fn(), ask: vi.fn(), newWork: vi.fn(),
    overview: {
      captain: null, tiles: { staging: null }, needs_you: [],
      project: {
        name: "HelpDesk", initials: "HD", repos: [{ full_name: "acme-demo/helpdesk" }], stage: "staging", status_label: "Working",
        live_url: "http://10.0.0.5:8005", health: "healthy", health_reason: "",
        deploy: { state: "failed", word: "", reason: REASON, app: "helpdesk", environment: "staging", target_id: "t1", index: null, total: null, deployment_id: "d1" },
        build_failed: { id: "wfr_x", label: "Deploy", build_id: "crun_de5396b5a6b04224a158", reason: REASON, word: "Deploy failed",
                        retry: null, fix: { label: "Needs your grant", detail: "", href: "" }, see_href: "", work_href: "?tab=work&drawer=work:wfr_x", beside: true },
        ...over,
      },
    },
  };
}

describe("the project header's status", () => {
  it("is ONE short line at body size: chip, a few words, a link to the build and Why? — no raw ids", async () => {
    const { Header } = await import("../app/projects/project-page");
    await mount(<Header ctx={headerCtx() as never} onboarding={false} />);
    const line = $('[data-testid="px-head-statusline"]')!;
    const words = (line.textContent ?? "").split(/\s+/).filter(Boolean);
    expect(line.textContent).not.toMatch(/crun_/);
    expect(line.textContent).not.toContain("authority/permission gap");
    expect(words.length).toBeLessThanOrEqual(24); // repo · History · staging url · health · the status words
    expect($('[data-testid="px-head-deploy"]')!.textContent).toContain("Last deploy failed");
    // The next step is said inside the failure's Why, never as more words on the row.
    expect($('[data-testid="px-head-hint"]')).toBeNull();
    expect($('[data-testid="px-head-failed"]')!.getAttribute("href")).toContain("wfr_x");
    // Each chip and its Why? are one unit: they wrap together.
    expect($('[data-testid="px-head-deploy-why"]')!.parentElement).toBe($('[data-testid="px-head-deploy"]')!.parentElement);
    expect($('[data-testid="px-head-failed-why"]')!.parentElement!.className).toBe("px-hgroup");
    await act(async () => { $('[data-testid="px-head-failed-why"]')!.click(); });
    expect($('[data-testid="px-head-hint"]')!.textContent).toBe("Needs your grant");
    expect($('[data-testid="px-head-why-block"]')!.textContent).toContain("Next: Needs your grant");
    await act(async () => { $('[data-testid="px-head-failed-why"]')!.click(); });
    // No paragraph of engine prose anywhere in the header until Why? is pressed.
    expect($$('[data-testid="px-head"] p').length).toBe(0);
    expect($('[data-testid="px-head-why-block"]')).toBeNull();
    const why = $('[data-testid="px-head-deploy-why"]')!;
    expect(why.textContent).toBe("Why?");
    expect(why.getAttribute("aria-expanded")).toBe("false");
    await act(async () => { why.click(); });
    const block = $('[data-testid="px-head-why-block"]')!;
    expect(block.textContent).toContain("authority/permission gap");
    expect(block.textContent).toContain("build ad50ce9f");
    expect(block.textContent).not.toMatch(/crun_/);
    expect(block.textContent).not.toContain("7dd457ab5876");
    expect($('[data-testid="px-head-deploy-why"]')!.getAttribute("aria-expanded")).toBe("true");
    await act(async () => { $('[data-testid="px-head-deploy-why"]')!.click(); });
    expect($('[data-testid="px-head-why-block"]')).toBeNull();
  });

  it("every status with a reason gets the same Why? (health too), and the hint is at most six words", async () => {
    const { Header, statusHint } = await import("../app/projects/project-page");
    await mount(<Header ctx={headerCtx({ deploy: null, health: "degraded", health_reason: REASON }) as never} onboarding={false} />);
    expect($('[data-testid="px-head-health-why"]')!.textContent).toBe("Why?");
    expect($('[data-testid="px-head-failed-why"]')!.textContent).toBe("Why?");
    expect($('[data-testid="px-head-statusline"]')!.textContent).not.toMatch(/crun_/);
    expect(statusHint("Grant crun_ad50ce9f01e14534b230 the deploy authority now please")).toBe("Grant build ad50ce9f the deploy authority…");
    expect(statusHint("")).toBe("");
  });
});

describe("one amber per view: the Needs-you signal only", () => {
  it("a Needs-you row has no amber button, tag, tile or rail — its action is the normal primary", async () => {
    const { WorkRow } = await import("../app/projects/tab-work");
    const [w] = await items();
    await mount(<WorkRow w={w} ctx={ctx as never} />);
    const row = $('[data-testid="px-work-wfr_merge5"]')!;
    expect(row.querySelectorAll('[data-kind="amber"], [data-tone="amber"], [data-c="amber"]').length).toBe(0);
    expect($('[data-testid="px-work-act-wfr_merge5"]')!.getAttribute("data-kind")).toBe("primary");
    // The only amber kind left on a row is the flow's "waiting on you" dot.
    const amberKinds = new Set(Array.from(row.querySelectorAll("[data-s]"))
      .filter((el) => el !== row && el.getAttribute("data-s") === "wait").map((el) => el.tagName));
    expect(amberKinds.size).toBeLessThanOrEqual(1);
  });

  it("the stylesheets: no amber rail or tint on a row, no Needs-you bar left to style", async () => {
    const { readFileSync } = await import("node:fs");
    const { join } = await import("node:path");
    const work = readFileSync(join(__dirname, "../app/projects/work.css"), "utf8");
    const px = readFileSync(join(__dirname, "../app/projects/px.css"), "utf8");
    expect(work).toMatch(/\[data-testid="px-work-rows"\] \.px-wk\[data-s="needs_you"\][^{]*\{[^}]*box-shadow: none/);
    expect(work).not.toMatch(/\.px-list-g\[data-g="needs_you"\][^{]*\{[^}]*--amber/);
    expect(px).not.toContain(".px-needs-bar");
    expect(px).not.toContain(".px-needs-item");
  });

  it("the Needs-you rows never say Only you (the group heading does, once), and no amber kind is set in the Work tab", async () => {
    const src = (await import("node:fs")).readFileSync((await import("node:path")).join(__dirname, "../app/projects/tab-work.tsx"), "utf8");
    expect(src).not.toMatch(/data-kind="amber"|data-tone="amber"|data-c="amber"/);
    expect(src).not.toContain(">Only you<");
    expect(src).toContain("only you can do these");
  });
});

describe("Needs you once per screen (operator 2026-11-21)", () => {
  it("the header never carries a Needs-you chip: the engine's word becomes what happened", async () => {
    const { failedWord } = await import("../app/projects/project-page");
    expect(failedWord("Needs you")).toBe("Build failed");
    expect(failedWord("Needs your OK")).toBe("Waiting for your OK");
    expect(failedWord("Deploy failed")).toBe("Deploy failed");
    expect(failedWord("")).toBe("Build failed");
    const { Header } = await import("../app/projects/project-page");
    const c = headerCtx();
    (c.overview.project.build_failed as { word: string }).word = "Needs you";
    await mount(<Header ctx={c as never} onboarding={false} />);
    expect($('[data-testid="px-head"]')!.textContent).not.toMatch(/needs you\b/i);
  });

  it("the project page draws no Needs-you bar; the Overview's card is a count and one link, never the list", async () => {
    const fs = await import("node:fs"); const path = await import("node:path");
    const page = fs.readFileSync(path.join(__dirname, "../app/projects/project-page.tsx"), "utf8");
    expect(page).not.toContain("<NeedsBar");
    expect(page).toContain('data-testid="px-tab-work-needs"');
    const ov = fs.readFileSync(path.join(__dirname, "../app/projects/tab-overview.tsx"), "utf8");
    expect(ov).not.toContain("ov.needs_you.map(");
    expect(ov).toContain('data-testid="px-ov-needs-open"');
  });
});
