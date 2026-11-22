// @vitest-environment happy-dom
/**
 * THE WORK LIST OPENS ITS WORKFLOW (design #287, screens 1 and 3; R1 + R2 of the onboarding demo):
 *
 *   - a Work row is title · status chip · Kicked off · Finished · Workflow #N · Build · Change #N;
 *   - ▸ lists the task's iterations, latest first, each with its own times and links;
 *   - a row click opens the task's latest Workflow run, full page, with Work as the way back; the
 *     Work panel is gone and an old drawer link lands on the same run;
 *   - the run screen carries ONE header line: Change · Preview · Staging · Prod · Your move, and a
 *     checker line "gwlint 9 → 0 open · Change #N › Checks" when the change has its packet.
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

const LOCAL = "http://boss-local/local_ng/gateway/pull/12";
const BASE = {
  kind: "feature", age: "5m", agents: [], stages: [], cost_usd: 0, attempts: [],
  links: { decision: "", workflow_run: "", build: "", pr: null, merge_pr: null, merged_pr: null, deploy: "", preview: "" },
};
const RAW = {
  ...BASE, id: "wfr_lumen", label: "Onboard lumen-pay.yaml", state: "needs_you", action_label: "Merge", run_number: 16,
  created_at: "2026-11-21T10:05:00Z", updated_at: "2026-11-21T10:19:00Z", attempt_count: 3, followups: 1, retries: 1,
  links: { ...BASE.links, decision: "?project_id=ng&area=decisions&decision=dec_7",
           workflow_run: "?project_id=ng&area=delivery_workflows&wfr=wfr_lumen",
           build: "?project_id=ng&area=code_tasks&view=apps&build=crun_root0001",
           pr: { number: 12, external_url: LOCAL }, merge_pr: { number: 12, external_url: LOCAL } },
  attempts: [
    { id: "crun_f2", label: "Follow-up 1 · checks failing", kind: "followup", title: "Follow-up: lockfile drift", state: "running",
      status: "running", created_at: "2026-11-21T10:12:00Z", updated_at: "2026-11-21T10:19:00Z", stalled_since: "", cost_usd: 0,
      pr: { number: 12, external_url: LOCAL }, build: "?project_id=ng&area=code_tasks&build=crun_f2", href: "" },
    { id: "crun_r1", label: "Retry 1", kind: "retry", title: "", state: "done", status: "completed",
      created_at: "2026-11-21T09:44:00Z", updated_at: "2026-11-21T09:58:00Z", stalled_since: "", cost_usd: 0,
      pr: { number: 12, external_url: LOCAL }, build: "?project_id=ng&area=code_tasks&build=crun_r1", href: "" },
    { id: "crun_root0001", label: "Build", kind: "build", title: "", state: "failed", status: "failed",
      created_at: "2026-11-21T09:12:00Z", updated_at: "2026-11-21T09:41:00Z", stalled_since: "", cost_usd: 0,
      pr: null, build: "?project_id=ng&area=code_tasks&build=crun_root0001", href: "" },
  ],
};

async function item(over: Record<string, unknown> = {}) {
  const { parseWorkItem } = await import("../lib/project-experience");
  return parseWorkItem({ ...RAW, ...over });
}
const ctx = { projectId: "ng", actor: "ceo", refreshWork: () => {}, open: () => {}, overview: { project: { repos: [] } } };

/** Links navigateTo follows (a hidden anchor it clicks): recorded, never navigated. */
function follow(): { hrefs: string[]; stop: () => void } {
  const hrefs: string[] = [];
  const spy = (e: Event) => {
    const a = (e.target as HTMLElement).closest?.("a");
    if (a && a.style.display === "none") { hrefs.push(a.getAttribute("href") ?? ""); e.preventDefault(); }
  };
  document.addEventListener("click", spy, true);
  return { hrefs, stop: () => document.removeEventListener("click", spy, true) };
}

describe("the row", () => {
  it("is title · status · Kicked off · Finished · Workflow #N · Build · Change #N, never amber", async () => {
    const { WorkRow, dateTime } = await import("../app/projects/tab-work");
    const w = await item();
    await mount(<WorkRow w={w} ctx={ctx as never} />);
    const row = $('[data-testid="px-work-wfr_lumen"]')!;
    expect($('[data-testid="px-work-open-wfr_lumen"]')!.textContent).toBe("Onboard lumen-pay.yaml");
    expect($('[data-testid="px-work-status-wfr_lumen"]')!.textContent).toBe("Merge change #12");
    expect($('[data-testid="px-work-kicked-wfr_lumen"]')!.textContent).toBe(dateTime("2026-11-21T10:05:00Z"));
    expect($('[data-testid="px-work-finished-wfr_lumen"]')!.textContent).toBe("—");
    expect($$('[data-testid="px-work-links-wfr_lumen"] a').map((a) => a.textContent)).toEqual(["Workflow #16", "Build", "Change #12"]);
    expect(row.textContent).not.toMatch(/\bRun\b|Needs you|crun_|wfr_/);
    expect(row.querySelectorAll('[data-tone="amber"], [data-kind="amber"], [data-c="amber"]').length).toBe(0);
  });

  it("a finished row says when it finished; a done one is green", async () => {
    const { WorkRow, dateTime } = await import("../app/projects/tab-work");
    const w = await item({ state: "done", attempts: [], attempt_count: 1 });
    await mount(<WorkRow w={w} ctx={ctx as never} />);
    expect($('[data-testid="px-work-finished-wfr_lumen"]')!.textContent).toBe(dateTime("2026-11-21T10:19:00Z"));
    expect($('[data-testid="px-work-status-wfr_lumen"]')!.getAttribute("data-tone")).toBe("green");
    expect($('[data-testid="px-work-caret-wfr_lumen"]')).toBeNull();
  });

  it("a click opens the latest Workflow run, full page, with Work as the way back", async () => {
    const { WorkRow } = await import("../app/projects/tab-work");
    const w = await item();
    await mount(<WorkRow w={w} ctx={ctx as never} />);
    const f = follow();
    try {
      await act(async () => { ($('[data-testid="px-work-wfr_lumen"] .wl-time') as HTMLElement).click(); });
      expect(f.hrefs.length).toBe(1);
      const q = new URLSearchParams(f.hrefs[0].split("?")[1]);
      expect([q.get("area"), q.get("wfr"), q.get("page"), q.get("from"), q.get("tab")]).toEqual(["delivery_workflows", "wfr_lumen", "full", "work", "runs"]);
    } finally { f.stop(); }
  });
});

describe("▸ the iterations", () => {
  it("lists every iteration latest first, numbered, each with its own status, times and links", async () => {
    const { WorkRow, dateTime } = await import("../app/projects/tab-work");
    const w = await item();
    await mount(<WorkRow w={w} ctx={ctx as never} />);
    expect($('[data-testid="px-work-iterations-wfr_lumen"]')).toBeNull();
    const caret = $('[data-testid="px-work-caret-wfr_lumen"]') as HTMLButtonElement;
    expect(caret.getAttribute("aria-expanded")).toBe("false");
    await act(async () => { caret.click(); });
    expect(caret.getAttribute("aria-expanded")).toBe("true");
    const rows = $$('[data-testid="px-work-iterations-wfr_lumen"] [role="listitem"]');
    expect(rows.map((r) => r.querySelector(".wl-n")!.textContent)).toEqual(["3", "2", "1"]);
    expect(rows.map((r) => r.querySelector(".wl-status")!.textContent)).toEqual(["Running", "Done", "Failed"]);
    const second = $('[data-testid="px-work-iter-crun_r1"]')!;
    const times = Array.from(second.querySelectorAll(".wl-time")).map((t) => t.textContent);
    expect(times).toEqual([dateTime("2026-11-21T09:44:00Z"), dateTime("2026-11-21T09:58:00Z")]);
    expect($$('[data-testid="px-work-links-crun_r1"] a').map((a) => a.textContent)).toEqual(["Build", "Change #12"]);
    // The first build opened no change yet: Build only. A running iteration has not finished.
    expect($$('[data-testid="px-work-links-crun_root0001"] a').map((a) => a.textContent)).toEqual(["Build"]);
    expect($('[data-testid="px-work-iter-crun_f2"]')!.querySelectorAll(".wl-time")[1].textContent).toBe("—");
  });
});

describe("where a row opens (lib/work-links)", () => {
  it("the workflow run full page; a build on its own, its Build page; the run a work item is", async () => {
    const { workOpenHref, workForRun } = await import("../lib/work-links");
    const w = await item();
    expect(workOpenHref(w)).toContain("page=full");
    const lone = await item({ links: { ...BASE.links, build: "?project_id=ng&area=code_tasks&build=crun_x" } });
    expect(workOpenHref(lone)).toBe("?project_id=ng&area=code_tasks&build=crun_x");
    expect(workOpenHref(await item({ links: BASE.links }))).toBe("");
    // A decision on its own (no run, no build) opens that decision, never a dead row.
    expect(workOpenHref(await item({ links: { ...BASE.links, decision: "?project_id=ng&area=decisions&decision=dec_9" } }))).toContain("dec_9");
    const other = await item({ id: "wi_9" });
    expect(workForRun([other], "wfr_lumen")?.id).toBe("wi_9");
    expect(workForRun([other], "wfr_none")).toBeNull();
  });
});

describe("the run screen's header line", () => {
  const DEPLOYS = { environments: [{ key: "staging", health: "healthy" }, { key: "production", health: "down" }] };

  it("Change #N · Preview · Staging · Prod · Your move: one primary button", async () => {
    const { RunWorkLineView } = await import("../app/workflows/run-work-line");
    const w = await item({ links: { ...RAW.links, preview: "/api/previews/prev_3/open" } });
    await mount(<RunWorkLineView w={w} deployments={DEPLOYS as never} />);
    expect($('[data-testid="rwl-change"]')!.textContent).toBe("Change #12");
    expect($('[data-testid="rwl-preview"]')!.getAttribute("href")).toContain("prev_3");
    expect($('[data-testid="rwl-staging"]')!.textContent).toBe("Staging healthy");
    expect($('[data-testid="rwl-staging"]')!.getAttribute("data-s")).toBe("ok");
    expect($('[data-testid="rwl-prod"]')!.textContent).toBe("Prod down");
    const btn = $('[data-testid="rwl-move-btn"]')!;
    expect($('[data-testid="rwl-move"]')!.textContent).toContain("Your move:");
    expect(btn.textContent).toBe("Merge change #12");
    expect(new URLSearchParams(btn.getAttribute("href")!.split("?")[1]).get("decision")).toBe("dec_7");
    expect($$('[data-testid="run-work-line"] .wf-btn-primary').length).toBe(1);
    expect($('[data-testid="rwl-checks"]')).toBeNull();
  });

  it("a change's preview keeps its Start / Recreate button (the Work panel's), not only a link", async () => {
    const { RunWorkLineView } = await import("../app/workflows/run-work-line");
    const app = { run_id: "wfr_lumen", name: "", title: "", version: "change", version_label: "", version_words: "", pr_number: 12, pr_url: "",
                  preview_id: "prev_3", state: "stopped", words: "", href: "", error: "", recreate: true, live_url: "" };
    const w = await item({ preview_app: app });
    await mount(<RunWorkLineView w={{ ...w, preview_app: app as never }} deployments={null} projectId="ng" actor="ceo" />);
    expect($('[data-testid="rwl-preview-btn"]')!.textContent).toContain("Recreate preview");
  });

  it("no move when nobody must act; Prod — with no host", async () => {
    const { RunWorkLineView } = await import("../app/workflows/run-work-line");
    const w = await item({ state: "running" });
    await mount(<RunWorkLineView w={w} deployments={{ environments: [] } as never} />);
    expect($('[data-testid="rwl-move"]')).toBeNull();
    expect($('[data-testid="rwl-prod"]')!.textContent).toBe("Prod —");
  });

  it("the checker line, when the change has its packet: gwlint 9 → 0 open · Change #12 › Checks", async () => {
    const { RunWorkLineView, checkerWords } = await import("../app/workflows/run-work-line");
    const w = await item();
    const v = { packet: { validators: [{ id: "gwlint", ruleset: "r", before: 9, after_open: 0, after_waived: 0, exit: 0, evidence: "" }] } };
    expect(checkerWords(v as never)).toEqual(["gwlint 9 → 0 open"]);
    expect(checkerWords(null)).toEqual([]);
    await mount(<RunWorkLineView w={w} deployments={null} validation={v as never} />);
    expect($('[data-testid="rwl-checks"]')!.textContent).toContain("gwlint 9 → 0 open");
    const link = $('[data-testid="rwl-checks-link"]')!;
    expect(link.textContent).toBe("Change #12 › Checks");
    expect(link.getAttribute("href")).toContain("tab=checks");
  });
});

describe("the brief behind one toggle", () => {
  it("runBrief is the run's directive, trimmed", async () => {
    const { runBrief } = await import("../app/delivery-workflows-ui");
    expect(runBrief({ input: { directive: "  Fix the partner file.\n" } })).toBe("Fix the partner file.");
    expect(runBrief({ input: {} })).toBe("");
    expect(runBrief({})).toBe("");
  });
});
