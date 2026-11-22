// @vitest-environment happy-dom
/**
 * THE WORK PANEL READS ONE PIECE OF WORK (approved wireframe, park-bench-map run #2, 2026-11-19).
 *
 * Fixtures modelled on the live rows: change #3 opened by "Retry 1" waits on its merge while
 * Follow-up 1 is still fixing 3 review findings and the failed "Package honest" gate; the
 * acceptance check read 0 met · 4 partly; no production host yet. The panel must:
 *
 *   - never offer Merge as THE action while a follow-up still fixes that change: "BOSS is fixing
 *     3 findings on change #3 — merge now or wait", Watch the fix first, Merge second;
 *   - never say "Ready to merge" over 0 must-haves met — say what is partial;
 *   - draw Who did what with each station's agent, time and the inline notes;
 *   - show the review's comments once each, the must-haves with verdicts, the one-sentence
 *     summary, the attempts on one line and the cost as a running total;
 *   - stack the flow at phone width / in the drawer, and keep the row to ONE action.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { $, $$, mount, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });
// An engine without the change's validation route (the panel then reads the build's typed counts).
beforeEach(() => { vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 404 }))); });

const LOCAL = "http://boss-local/local_park-bench-map-e86c6d9e/park-bench-map/pull/3";
const LOG = "?project_id=park&area=code_tasks&build=crun_3b52&logs=build";
const NOW = Date.parse("2026-11-19T14:44:00Z");
const RAW = {
  id: "wfr_e56d", label: "Bench Name Label on Map Bubble", title: "The bubble present", kind: "feature", workflow: "Minor Feature",
  run_number: 2, state: "needs_you", age: "1h", action_label: "Merge", created_at: "2026-11-19T13:39:51Z", updated_at: "2026-11-19T14:22:38Z",
  cost_usd: 0, list_usd: 10.68, attempt_count: 3, followups: 1, retries: 1,
  detail: "The bubble on the park bench map isn't named with the bench, so it's hard to understand.",
  agents: [{ name: "Juno", role_key: "product_manager", avatar: "" }],
  attempts: [
    { id: "crun_3b52", label: "Follow-up 1 · review findings", kind: "followup", state: "running", created_at: "2026-11-19T14:22:39Z", list_usd: 4.15 },
    { id: "crun_31e1", label: "Retry 1", kind: "retry", state: "needs_you", created_at: "2026-11-19T13:50:32Z", updated_at: "2026-11-19T14:22:38Z", list_usd: 5.52 },
    { id: "crun_20e2", label: "Build", kind: "build", state: "done", created_at: "2026-11-19T13:46:21Z", list_usd: 1.01 },
  ],
  stages: [
    { key: "plan", label: "P", state: "done", agent: { name: "Juno", role_key: "product_manager", avatar: "" }, duration_s: 341 },
    { key: "design", label: "D", state: "done" },
    { key: "build", label: "B", state: "done", agent: { name: "Forge", role_key: "software_engineer", avatar: "" }, duration_s: 2160 },
    { key: "review", label: "R", state: "done" },
    { key: "pr", label: "PR", state: "waiting" }, { key: "ship", label: "S", state: "todo" },
  ],
  links: {
    decision: "?project_id=park&area=decisions&decision=dec_849f", log: LOG, build: "?project_id=park&area=code_tasks&build=crun_3b52",
    workflow_run: "?project_id=park&area=delivery_workflows&wfr=wfr_e56d", pr: { number: 3, external_url: LOCAL },
    merge_pr: { number: 3, external_url: LOCAL, follow_up_of: null, failed: null, build_id: "crun_31e1",
                fixing: { attempt: "crun_3b52", label: "Follow-up 1 · review findings", state: "running", since: "2026-11-19T14:22:39Z", findings: 3 },
                acceptance: { met: 0, partial: 4, missing: 0, total: 4 } },
    merged_pr: null,
  },
  preview_app: { run_id: "crun_31e1", preview_id: null, state: "none", words: "Not started", recreate: false, href: null },
};
const MUSTS = ["Named bench shows its name on the bubble", "No name → “Bench #<id>”", "Special characters shown safely", "Existing map behaviour unchanged"];
const STORY = {
  id: "wfr_e56d", number: 2, minutes: 66, cost_usd: 0, cost_parts: [], preview: "", pr: { number: 3, external_url: LOCAL },
  stages: [
    { key: "plan", name: "Plan", state: "done", sub: "PRD agreed" }, { key: "design", name: "You approved", state: "done", sub: "Paramaguru", at: "2026-11-19T13:45:48Z", person: true },
    { key: "pr", name: "Merge", state: "waiting", sub: "Only a person merges", person: true }, { key: "ship", name: "Staging", state: "todo", sub: "Auto after merge" },
  ],
  explained: {
    discussed: { decisions: ["Fallback is “Bench #<id>”", "No feature flag", "No editable names"], open_questions: ["Bench-name provenance unconfirmed"], seats: 5,
                 href: "?project_id=park&area=meetings&meeting=mrun_1" },
    built: { build_id: "crun_31e1", status: "completed", bullets: ["Changed 5 files"], files_changed: 5, follow_ups: ["a", "b"],
             summary: "Show the bench's name as the first line of the bubble. Tests pass.",
             groups: [{ purpose: "Screens", files: ["public/app.js"], count: 1 }, { purpose: "Tests", files: ["tests/popup-browser.test.js", "tests/notes-api-edge-cases.test.js"], count: 2 }],
             tests: { files: ["tests/popup-browser.test.js"], passed: true, count: 31 }, review: { rounds: 1, raised: 4 },
             acceptance: { items: MUSTS.map((t, i) => ({ n: i + 1, text: t, verdict: "partial", evidence: "public/app.js" })) } },
    later: [],
  },
  review_comments: { raw: 10, items: [
    { key: "gate:web_package_honest", text: "“Package honest” check failed when the change opened", severity: "gate", source: "web_package_honest", path: null, line: null, merged: 2 },
    { key: "at:tests/popup-browser.test.js:34", text: "Test markers sit outside the 420 px map", severity: "P1", source: "review", path: "tests/popup-browser.test.js", line: 34, merged: 2 },
    { key: "at:tests/popup-browser.test.js:54", text: "Browser launches before the cleanup block", severity: "P2", source: "review", path: "tests/popup-browser.test.js", line: 54, merged: 1 },
    { key: "said:x", text: "Edge-case coverage incomplete", severity: "P2", source: "consistency_check", path: null, line: null, merged: 1 },
  ] },
};
const DEPLOYMENTS = {
  environments: [{ key: "staging", label: "Staging", host: "203.0.113.10:8015", health: "degraded", p95_ms: 542,
                   last_deploy: { commit: "97256c8aa", at: "2026-11-19T08:00:00Z", by: "", label: "", href: "", pr: 1, pr_url: "" } }],
  production: { state: "needs_host", action: { label: "Pick host", href: "?project_id=park&drawer=needs:dec_prodhost" } },
};

async function load(over: Record<string, unknown> = {}) {
  const px = await import("../lib/project-experience");
  return { w: px.parseWorkItem({ ...RAW, ...over }), story: px.parseWorkStory(STORY), deployments: px.parseDeployments(DEPLOYMENTS) };
}
const ctx = { projectId: "park", actor: "ceo", overview: { captain: null, project: { name: "Park bench map" } },
              refreshWork: () => {}, open: () => {}, ask: () => {} };

describe("the honest merge (workNextStep)", () => {
  it("a follow-up still fixing the change: merge now or wait, Watch the fix first, Merge second", async () => {
    const { workNextStep } = await import("../lib/work-next-step");
    const next = workNextStep((await load()).w)!;
    expect(next.words).toBe("BOSS is fixing 3 findings on change #3 — merge now or wait");
    expect(next.words).not.toContain("Ready to merge");
    expect(next.secondary).toBe(true);
    expect(next.wait).toEqual({ label: "Watch the fix", href: LOG });
    expect(next.label).toBe("Merge change #3");
  });

  it("no must-have fully met: never Ready to merge, it says what is partial", async () => {
    const { workNextStep } = await import("../lib/work-next-step");
    const { w } = await load({ links: { ...RAW.links, merge_pr: { ...RAW.links.merge_pr, fixing: null } } });
    const next = workNextStep(w)!;
    expect(next.words).toBe("Change #3 is open · 0 of 4 must-haves met, 4 partly");
    expect(next.secondary).toBeUndefined();
    const all = await load({ links: { ...RAW.links, merge_pr: { ...RAW.links.merge_pr, fixing: null, acceptance: { met: 4, partial: 0, missing: 0, total: 4 } } } });
    expect(workNextStep(all.w)!.words).toBe("Ready to merge");
  });
});

describe("who did what (flowNodes)", () => {
  it("six nodes with agents, times and the inline notes", async () => {
    const { flowNodes } = await import("../lib/work-panel");
    const { workNextStep } = await import("../lib/work-next-step");
    const { w, story, deployments } = await load();
    const nodes = flowNodes({ w, story, next: workNextStep(w), deployments, nowMs: NOW, hhmm: () => "5:45 AM" });
    expect(nodes.map((n) => n.name)).toEqual(["Spec", "Build", "Review & tests", "Merge", "Staging", "Production"]);
    const [spec, build, review, merge, staging, prod] = nodes;
    expect(spec.sub).toBe("PRD agreed · 6 m");
    expect(spec.who).toBe("Juno + panel of 5 · Paramaguru approved 5:45 AM");
    expect(build.sub).toBe("5 files · 36 m");
    expect(build.who).toBe("Forge · 3 tries");
    expect(review.state).toBe("live");
    expect(review.sub).toBe("31/31 pass · round 2");
    expect(review.note).toMatchObject({ tone: "boss", title: "BOSS is fixing" });
    expect(review.note!.text).toBe("3 review findings + gate “Package honest” · Follow-up 1 · review findings · 21 m in");
    expect(merge).toMatchObject({ state: "wait", you: true, who: "Only a person merges" });
    expect(merge.note!.text).toBe("Merge now, or wait for the follow-up to land its fixes");
    expect(staging.who).toBe("Harbor · 203.0.113.10:8015");
    expect(prod.sub).toBe("no host yet");
    expect(prod.note).toMatchObject({ tone: "you", text: "Pick a prod host", action: { label: "Pick host" } });
  });

  it("the header sentence, Helm's three questions and the cost's running total", async () => {
    const { stateSentence, helmSuggestions, costPoints, attemptsLine, durationWords } = await import("../lib/work-panel");
    const { workNextStep } = await import("../lib/work-next-step");
    const { w } = await load();
    expect(stateSentence(w, workNextStep(w))).toEqual({ lead: "Merge now, or wait.", tone: "amber",
      rest: "Change #3 is built and reviewed; BOSS is still fixing 3 review findings in Follow-up 1 · review findings." });
    expect(helmSuggestions(w, { noProdHost: true })).toEqual(["Is it safe to merge now?", "Why did the build retry?", "What's left for production?"]);
    expect(costPoints(w).map((p) => +p.total.toFixed(2))).toEqual([1.01, 6.53, 10.68]);
    expect(attemptsLine(w)).toBe("build → retry → follow-up (running)");
    expect(durationWords(3960)).toBe("1 h 6 m");
  });
});

describe("the panel", () => {
  it("draws the header, the flow, Asked/Discussed/Built, the comments and the rail", async () => {
    const { WorkPanel } = await import("../app/projects/work-panel");
    const { w, story, deployments } = await load();
    await mount(<WorkPanel ctx={ctx as never} w={w} story={story} deployments={deployments} testid="wp" />);
    // ONE primary action: while BOSS fixes the change, Watch the fix leads and Merge is not amber.
    expect($('[data-testid="wp-wait"]')!.textContent).toContain("Watch the fix");
    expect($('[data-testid="wp-merge"]')!.textContent).toContain("Merge change #3");
    expect($('[data-testid="wp-merge"]')!.getAttribute("data-kind")).toBeNull();
    expect($$('[data-kind="primary"]').length).toBe(0);
    expect($('[data-testid="wp-sentence"]')!.textContent).not.toContain("Ready to merge");
    expect($('[data-testid="wp-kpis"]')!.textContent).toContain("1 h 6 m");
    expect($('[data-testid="wp-kpis"]')!.textContent).toContain("≈ $10.68");
    expect($('[data-testid="wp-node-review"]')!.getAttribute("data-s")).toBe("live");
    expect($('[data-testid="wp-note-review"]')!.textContent).toContain("BOSS is fixing");
    expect($('[data-testid="wp-note-merge"]')!.textContent).toContain("Your move");
    expect($('[data-testid="wp-log"]')!.getAttribute("href")).toContain("logs=build");
    // Asked: the must-haves with verdicts; Built: the one sentence.
    expect($('[data-testid="wp-must-count"]')!.textContent).toBe("0 met · 4 partly");
    expect($$('[data-testid="wp-musts"] li').length).toBe(4);
    // Built: typed counts, never the coder's free-text summary (validation-review SPEC §1.4 rule 5).
    expect($('[data-testid="wp-summary"]')).toBeNull();
    expect($('[data-testid="wp-built"]')!.textContent).not.toContain("first line of the bubble");
    expect($('[data-testid="wp-built"]')!.textContent).toContain("31/31 pass");
    expect($('[data-testid="wp-review-counts"]')!.textContent).toMatch(/P1\s*1/);
    // Run · Build · Change under the title, without opening ⋯ (SPEC §5.1); the build is the one
    // that opened the change, not the follow-up still fixing it.
    expect($('[data-testid="wp-link-run"]')!.textContent).toContain("Workflow run #2");
    expect($('[data-testid="wp-link-build"]')!.getAttribute("href")).toContain("build=crun_31e1");
    expect($('[data-testid="wp-link-change"]')!.textContent).toContain("Change #3");
    expect($('[data-testid="wp-left"]')!.textContent).toContain("provenance");
    // Comments: once each, 3 shown and the rest behind "1 more"; BOSS is already on them.
    expect($('[data-testid="wp-comments"]')!.textContent).toContain("4 · deduped from 10");
    expect($$('[data-testid="wp-comments"] > .wp-cmts > li').length).toBe(3);
    expect($('[data-testid="wp-watch-fix"]')).not.toBeNull();
    expect($('[data-testid="wp-let-fix"]')).toBeNull();
    // Rail: where it runs says staging runs an older change; attempts collapsed; cost.
    expect($('[data-testid="wp-env-staging"]')!.textContent).toContain("runs change #1");
    expect($('[data-testid="wp-env-production"]')!.textContent).toContain("Pick host");
    expect($('[data-testid="wp-attempts"] details')!.hasAttribute("open")).toBe(false);
    expect($('[data-testid="wp-cost"]')!.textContent).toContain("included in plan");
    expect($$('[data-testid="wp-helm-chip"]').map((c) => c.textContent)).toContain("Is it safe to merge now?");
  });

  it("with nothing fixing it, the comments can be handed to BOSS", async () => {
    const { WorkPanel } = await import("../app/projects/work-panel");
    const { w, story, deployments } = await load({ links: { ...RAW.links, merge_pr: { ...RAW.links.merge_pr, fixing: null } } });
    await mount(<WorkPanel ctx={ctx as never} w={w} story={story} deployments={deployments} testid="wp" />);
    expect($('[data-testid="wp-let-fix"]')!.textContent).toContain("Let BOSS fix these");
    expect($$('[data-testid="wp-comments"] input[type="checkbox"]').length).toBe(4); // every comment, picked by default
    // Merge is THE amber action again, but the sentence still says what is partial.
    expect($('[data-testid="wp-merge"]')!.getAttribute("data-kind")).toBe("primary");
    expect($('[data-testid="wp-sentence"]')!.textContent).toContain("0 of 4 must-haves met, 4 partly");
  });

  it("the drawer's short preview stacks the flow and opens the whole page", async () => {
    const { WorkPanelShort } = await import("../app/projects/work-panel");
    const { w, story, deployments } = await load();
    let opened = false;
    await mount(<WorkPanelShort ctx={ctx as never} w={w} story={story} deployments={deployments} onOpen={() => { opened = true; }} testid="wp" />);
    expect($('[data-testid="wp-flow"]')!.hasAttribute("data-vertical")).toBe(true);
    expect($('[data-testid="wp-comments"]')).toBeNull();
    ($('[data-testid="wp-open"]') as HTMLButtonElement).click();
    expect(opened).toBe(true);
  });
});

describe("the row", () => {
  it("is the title, one line of state, a compact flow and ONE action", async () => {
    const { WorkRow } = await import("../app/projects/tab-work");
    const { w } = await load();
    await mount(<WorkRow w={w} ctx={{ ...ctx, overview: { ...ctx.overview, project: { repos: [] } } } as never} />);
    const row = $('[data-testid="px-work-wfr_e56d"]')!;
    expect($('[data-testid="px-work-open-wfr_e56d"]')!.textContent).toBe("Bench Name Label on Map Bubble");
    expect($('[data-testid="px-work-line-wfr_e56d"]')!.textContent).toContain("Merge now, or wait.");
    expect($$('[data-testid="px-work-flow-wfr_e56d"] i').map((i) => i.getAttribute("data-s"))).toEqual(["done", "done", "live", "wait", "todo"]);
    // ONE action (+ the preview): never two amber buttons, never the run/build/change chips.
    const act = $('[data-testid="px-work-act-wfr_e56d"]')!;
    expect(act.textContent).toBe("Merge change #3");
    expect(act.getAttribute("data-kind")).toBeNull();
    expect(row.querySelectorAll("button.px-btn, a.px-btn").length).toBeLessThanOrEqual(2);
    expect($('[data-testid="px-work-run-wfr_e56d"]')).toBeNull();
    expect($('[data-testid="px-work-attempts-wfr_e56d"]')).toBeNull();
  });
});
