// @vitest-environment happy-dom
/**
 * DRY RUN 3, GAP 8 (the Work list and the run header):
 *   - the iterations toggle (▸ and its "· N" count) expands the row and never opens the workflow;
 *   - a cancelled or closed run says "Cancelled" / "Closed", never "Done";
 *   - the full-page run title separates the subject from the workflow name;
 *   - a change never deployed reads "Staging —", never the environment's health;
 *   - "Your move: <button>" sits in the run header, even while a fix runs on the change.
 */
import React from "react";
import { readFileSync } from "node:fs";
import { join } from "node:path";
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

const LOCAL = "http://boss-local/local_ng/gateway/pull/19";
const LINKS = { decision: "?project_id=ng&area=decisions&decision=dec_9", workflow_run: "?project_id=ng&area=delivery_workflows&wfr=wfr_lp",
  build: "?project_id=ng&area=code_tasks&build=crun_b1", pr: { number: 19, external_url: LOCAL }, merge_pr: { number: 19, external_url: LOCAL },
  merged_pr: null, deploy: "", preview: "" };
const ATT = (id: string, state: string, status: string) => ({ id, label: id, kind: "build", title: "", state, status,
  created_at: "2026-11-22T08:00:00Z", updated_at: "2026-11-22T08:10:00Z", stalled_since: "", cost_usd: 0, pr: null,
  build: `?project_id=ng&area=code_tasks&build=${id}`, href: "" });
const RAW = {
  id: "wfr_lp", label: "Lumen Pay tenant onboarding", kind: "feature", age: "5m", agents: [], stages: [], cost_usd: 0,
  state: "needs_you", status: "waiting", action_label: "Merge", run_number: 24, created_at: "2026-11-22T08:00:00Z", updated_at: "2026-11-22T08:30:00Z",
  links: LINKS, attempts: [ATT("crun_f1", "done", "cancelled"), ATT("crun_r1", "needs_you", "completed"), ATT("crun_b1", "running", "repairing")],
};
async function item(over: Record<string, unknown> = {}) {
  const { parseWorkItem } = await import("../lib/project-experience");
  return parseWorkItem({ ...RAW, ...over });
}
const ctx = { projectId: "ng", actor: "ceo", refreshWork: () => {}, open: vi.fn(), overview: { project: { repos: [] } } };

function follow(): { hrefs: string[]; stop: () => void } {
  const hrefs: string[] = [];
  const spy = (e: Event) => {
    const a = (e.target as HTMLElement).closest?.("a");
    if (a && a.style.display === "none") { hrefs.push(a.getAttribute("href") ?? ""); e.preventDefault(); }
  };
  document.addEventListener("click", spy, true);
  return { hrefs, stop: () => document.removeEventListener("click", spy, true) };
}

describe("the iterations toggle", () => {
  for (const testid of ["px-work-caret-wfr_lp", "px-work-iters-wfr_lp"]) {
    it(`${testid} expands the row and never opens the workflow`, async () => {
      const { WorkRow } = await import("../app/projects/tab-work");
      await mount(<WorkRow w={await item()} ctx={ctx as never} />);
      const f = follow();
      try {
        await act(async () => { $(`[data-testid="${testid}"]`)!.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
        expect($('[data-testid="px-work-iterations-wfr_lp"]')).not.toBeNull();
        expect(f.hrefs).toEqual([]);
        await act(async () => { $(`[data-testid="${testid}"]`)!.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
        expect($('[data-testid="px-work-iterations-wfr_lp"]')).toBeNull();
        expect(f.hrefs).toEqual([]);
      } finally { f.stop(); }
    });
  }
  it("the row's title still opens the workflow", async () => {
    const { WorkRow } = await import("../app/projects/tab-work");
    await mount(<WorkRow w={await item()} ctx={ctx as never} />);
    const f = follow();
    try {
      await act(async () => { $('[data-testid="px-work-open-wfr_lp"]')!.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
      expect(f.hrefs.length).toBe(1);
    } finally { f.stop(); }
  });
});

describe("how a run ended", () => {
  it("a cancelled run reads Cancelled, a closed one Closed, a completed one Done", async () => {
    const { statusChip } = await import("../app/projects/tab-work");
    expect(statusChip(await item({ state: "done", status: "cancelled" })).word).toBe("Cancelled");
    expect(statusChip(await item({ state: "done", status: "closed" })).word).toBe("Closed");
    expect(statusChip(await item({ state: "done", status: "completed" })).word).toBe("Done");
  });
  it("a cancelled iteration reads Cancelled, never Done", async () => {
    const { IterationRows } = await import("../app/projects/tab-work");
    await mount(<IterationRows w={await item()} />);
    const row = $('[data-testid="px-work-iter-crun_f1"]')!;
    expect(row.textContent).toContain("Cancelled");
    expect(row.textContent).not.toContain("Done");
  });
});

describe("the run header", () => {
  it("separates the subject from the workflow name", async () => {
    const { RunFullTitle } = await import("../app/delivery-workflows-ui");
    await mount(<RunFullTitle subject="Lumen Pay tenant onboarding" workflow="Onboard a config file" version={1} />);
    const t = $('[data-testid="run-full-title"]')!.textContent!;
    expect(t).toBe("Lumen Pay tenant onboarding · Onboard a config file v1");
    unmount();
    await mount(<RunFullTitle subject={null} workflow="Onboard a config file" version={1} />);
    expect($('[data-testid="run-full-title"]')!.textContent).toBe("Onboard a config file v1");
  });
  it("Staging — for a change never deployed; its health once deployed", async () => {
    const { RunWorkLineView } = await import("../app/workflows/run-work-line");
    const DEPLOYS = { environments: [{ key: "staging", health: "healthy" }, { key: "production", health: "unknown" }] };
    await mount(<RunWorkLineView w={await item()} deployments={DEPLOYS as never} />);
    expect($('[data-testid="rwl-staging"]')!.textContent).toBe("Staging —");
    expect($('[data-testid="rwl-prod"]')!.textContent).toBe("Prod —");
    unmount();
    await mount(<RunWorkLineView w={await item({ links: { ...LINKS, deploy: "?project_id=ng&area=infra&deploy=dep_1" } })} deployments={DEPLOYS as never} />);
    expect($('[data-testid="rwl-staging"]')!.textContent).toBe("Staging healthy");
  });
  it("Your move: the merge, even while a fix still runs on the change", async () => {
    const { RunWorkLineView } = await import("../app/workflows/run-work-line");
    const fixing = { ...LINKS, merge_pr: { ...LINKS.merge_pr, fixing: { attempt: "crun_b1", label: "Build", state: "running", since: "", findings: 2 } } };
    await mount(<RunWorkLineView w={await item({ links: fixing })} deployments={null} />);
    expect($('[data-testid="rwl-move"]')!.textContent).toContain("Your move:");
    expect($('[data-testid="rwl-move-btn"]')!.textContent).toBe("Merge change #19");
  });
  it("the full page puts the work line in its header", async () => {
    const src = readFileSync(join(__dirname, "../app/delivery-workflows-ui.tsx"), "utf8");
    const head = src.slice(src.indexOf('<header className="dw-full-head">', src.indexOf("data-run-id={fullRun.id}")));
    expect(head.slice(0, head.indexOf("</header>"))).toContain("<RunWorkLine");
    expect(src).toContain("{fullPage ? null : <RunWorkLine");
  });
});
