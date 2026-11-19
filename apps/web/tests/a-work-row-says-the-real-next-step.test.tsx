// @vitest-environment happy-dom
/**
 * A WORK ROW SAYS THE REAL NEXT STEP (park-bench-map, 2026-11-19).
 *
 * The row "The bubble present · 2 attempts · Feature · Waiting 18m" showed **Approve deploy**;
 * the drawer it opened said "Ready to merge · Merge change #3". Now one function
 * (lib/work-next-step) names the row's button and the drawer's top card: "Merge change #3",
 * hinted "deploys to staging after" when Ship follows by itself.
 *
 * And the change's own preview sits beside the merge (Open preview / Recreate preview / Start
 * preview, the Your app card's one open), on the row and on the build page's Outputs row.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, click, mount, settle, unmount } from "./_helpers/live-render";
import { stubFetch } from "./_helpers/cockpit-render";
import { cockpitPrOpen } from "./_helpers/factory-fixtures";

const started: Array<Record<string, string>> = [];
vi.mock("../app/actions", async (original) => ({
  ...(await original<Record<string, unknown>>()),
  startPreviewAction: vi.fn(async (_prev: unknown, fd: FormData) => {
    const f: Record<string, string> = {};
    fd.forEach((v, k) => { f[k] = String(v); });
    started.push(f);
    return { starting: true };
  }),
}));
vi.mock("../app/factory/legacy-build-parts", async (orig) => ({ ...(await orig<object>()), DeploymentLine: () => <span /> }));
vi.mock("../app/deploy-ui", () => ({ LiveDeployment: () => <span /> }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => { unmount(); vi.unstubAllGlobals(); started.length = 0; });

const LOCAL = "http://boss-local/local_park-bench-map-e86c6d9e/park-bench-map/pull/3";
const DECISION = "?project_id=park&area=decisions&decision=dec_merge3";
/** The live row, as the engine now sends it (work.py): merge_pr #3 waiting, Ship to come. */
const RAW = {
  id: "wfr_bubble", label: "Bench Name Label on Map Bubble", title: "The bubble present", kind: "feature",
  state: "needs_you", age: "18m", action_label: "Merge", attempts: [], agents: [],
  stages: [
    { key: "plan", label: "P", state: "done" }, { key: "design", label: "D", state: "done" },
    { key: "build", label: "B", state: "done" }, { key: "review", label: "R", state: "done" },
    { key: "pr", label: "PR", state: "waiting" }, { key: "ship", label: "S", state: "todo" },
  ],
  links: {
    decision: DECISION, pr: { number: 3, external_url: LOCAL },
    merge_pr: { number: 3, external_url: LOCAL, follow_up_of: null, failed: null,
                followups: { count: 5, queued: true, words: "5 follow-ups go to the More tests pass next" } },
    merged_pr: null,
  },
  preview_app: { run_id: "crun_20e2", preview_id: "prev_7121", state: "running", words: "Running", recreate: false,
                 href: "/api/previews/prev_7121/open" },
};

async function item(over: Record<string, unknown> = {}) {
  const { parseWorkItem } = await import("../lib/project-experience");
  return parseWorkItem({ ...RAW, ...over });
}

describe("one next step for the row and the drawer", () => {
  it("a merge waiting with an automatic deploy after reads Merge change #3, hinted", async () => {
    const { workNextStep } = await import("../lib/work-next-step");
    const next = workNextStep(await item())!;
    expect(next.kind).toBe("merge");
    expect(next.label).toBe("Merge change #3");
    expect(next.hint).toBe("deploys to staging after");
    expect(next.words).toBe("Ready to merge · 5 follow-ups go to the More tests pass next");
    expect(next.href).toBe(DECISION);
    expect(next.label).not.toMatch(/deploy/i);
  });

  it("a real deploy approval (no merge waiting) keeps its own words", async () => {
    const { workNextStep } = await import("../lib/work-next-step");
    const next = workNextStep(await item({ action_label: "Approve deploy",
      links: { decision: DECISION, pr: null, merge_pr: null, merged_pr: { number: 2, external_url: LOCAL } } }))!;
    expect(next.kind).toBe("decide");
    expect(next.label).toBe("Approve deploy");
    expect(next.hint).toBe("");
  });

  it("no hint when nothing ships after the merge", async () => {
    const { workNextStep } = await import("../lib/work-next-step");
    const next = workNextStep(await item({ stages: RAW.stages.map((s) => (s.key === "ship" ? { ...s, state: "skipped" } : s)) }))!;
    expect(next.hint).toBe("");
  });

  it("work that does not wait on a person has no next step", async () => {
    const { workNextStep } = await import("../lib/work-next-step");
    expect(workNextStep(await item({ state: "running" }))).toBeNull();
  });

  it("the row's button says the drawer's words, with the change's preview beside it", async () => {
    const { WorkRow } = await import("../app/projects/tab-work");
    const ctx = { projectId: "park", actor: "ceo", refreshWork: () => {}, open: () => {} } as unknown as Parameters<typeof WorkRow>[0]["ctx"];
    await mount(<WorkRow w={await item()} ctx={ctx} />);
    const act = $('[data-testid="px-work-act-wfr_bubble"]')!;
    expect(act.textContent).toContain("Merge change #3");
    expect(act.textContent).not.toContain("Approve deploy");
    expect($('[data-testid="px-work-act-hint-wfr_bubble"]')?.textContent).toBe("deploys to staging after");
    expect($('[data-testid="px-work-preview-wfr_bubble"]')?.textContent).toContain("Open preview");
  });

  it("a stopped preview on the row is Recreate preview, started through the one write path", async () => {
    const { WorkRow } = await import("../app/projects/tab-work");
    const ctx = { projectId: "park", actor: "ceo", refreshWork: () => {}, open: () => {} } as unknown as Parameters<typeof WorkRow>[0]["ctx"];
    stubFetch({});
    await mount(<WorkRow w={await item({ preview_app: { ...RAW.preview_app, state: "stopped", recreate: true, href: null } })} ctx={ctx} />);
    const btn = $('[data-testid="px-work-preview-wfr_bubble"]')!;
    expect(btn.textContent).toContain("Recreate preview");
    await click(btn);
    await settle();
    expect(started).toEqual([expect.objectContaining({ run_id: "crun_20e2" })]);
  });
});

describe("the preview's button, from its state", () => {
  it("never had one → Start preview; stopped/expired → Recreate; running → Open", async () => {
    const { buildPreviewApp, previewWord } = await import("../app/projects/work-preview");
    expect(previewWord(buildPreviewApp("crun_1", null), false)).toBe("Start preview");
    expect(previewWord(buildPreviewApp("crun_1", { id: "prev_1", status: "stopped" }), false)).toBe("Recreate preview");
    expect(previewWord(buildPreviewApp("crun_1", { id: "prev_1", status: "expired" }), false)).toBe("Recreate preview");
    expect(previewWord(buildPreviewApp("crun_1", { id: "prev_1", status: "running" }), false)).toBe("Open preview");
  });

  it("the build page's Outputs row offers Start preview inline for a change that never had one", async () => {
    const { Cockpit } = await import("../app/factory/cockpit");
    const base = cockpitPrOpen() as Record<string, unknown>;
    stubFetch({ ...base, outputs: { ...(base.outputs as Record<string, unknown>), preview: null } });
    await mount(<Cockpit projectId="p1" runId="crun_pr" actor="ceo" onClose={() => {}} onOpenBuild={() => {}} />);
    await settle();
    const btn = $('[data-testid="fx-out-preview-act"]')!;
    expect(btn.textContent).toContain("Start preview");
    await click(btn);
    await settle();
    expect(started).toEqual([expect.objectContaining({ run_id: "crun_pr" })]);
  });

  it("a running preview on the build page is Open preview", async () => {
    const { Cockpit } = await import("../app/factory/cockpit");
    stubFetch(cockpitPrOpen());
    await mount(<Cockpit projectId="p1" runId="crun_pr" actor="ceo" onClose={() => {}} onOpenBuild={() => {}} />);
    await settle();
    expect($('[data-testid="fx-out-preview-act"]')?.textContent).toContain("Open preview");
  });
});
