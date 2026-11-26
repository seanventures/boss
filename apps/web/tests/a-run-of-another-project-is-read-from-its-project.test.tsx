// @vitest-environment happy-dom
/**
 * A run from another project is read from ITS project (walk of rc10.36, Workflows › Runs).
 *
 * The Runs list asks with scope=org, so it holds runs of every project in the org. The run that
 * opened by default belonged to another project, and each of its panels asked under the page's
 * project: /api/activity, /v1/decisions/<id>, /api/self-fix?view=run_autonomy,
 * /api/workflow-report and /api/run-acts?view=chain all answered 404, five console errors on
 * load. Every read about a run carries the run's own project.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { standingOf } from "./_helpers/run-standing";
import { codeOnly } from "./_helpers/source";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
(globalThis as Record<string, unknown>).React = React;

const { RunsView, runProject } = await import("../app/delivery-workflows-ui");
const { forgetRunDefinitions } = await import("../app/workflows/flow/run-lane");

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  vi.unstubAllGlobals();
});

function step(id: string, capability: string, status: string, extra: Record<string, unknown> = {}) {
  return { step_id: id, kind: "capability", status, capability, output: {}, evidence: [], error: null,
           approval: {}, due_at: null, timeout_applied: null, attempt: 1, started_at: null, completed_at: null,
           decision_id: null, ...extra };
}
const RUN = {
  id: "wfr_other", project_id: "northgate", project_name: "Northgate Gateway",
  status: "waiting", standing: standingOf("waiting"), workflow_key: "onboard", workflow_name: "Onboard a config file",
  version: 1, step_count: 2, steps_done: 1, current_step: "merge", waiting_on: "You", blocked_reason: null, error: null,
  updated_at: null, actions: [], input: { title: "Tenant onboarding" }, finished: false,
  steps: [
    step("fix", "code", "succeeded"),
    step("merge", "merge", "waiting", { decision_id: "dec_1", approval: { required: true } }),
  ],
};

describe("a run of another project", () => {
  it("asks every panel under the run's project, never the page's", async () => {
    forgetRunDefinitions();
    const asked: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
      const url = new URL(String(input), "http://console.test");
      asked.push(`${url.pathname}?${url.searchParams.toString()}`);
      return Response.json({});
    }));
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    const noop = () => {};
    await act(async () => root!.render(
      <RunsView runs={[RUN as never]} openRun="wfr_other" setOpenRun={noop} onAdvance={noop} busy={null}
                capabilities={[]} onRetryStep={noop} onSkipStep={noop} onRerun={noop} onStop={noop} onDelete={noop}
                onStopAndRerun={noop} onBuildOnThis={noop} onRecheck={noop} onSetTarget={noop} onPrepareTarget={noop}
                onRunPreview={noop} onBound={noop} projectId="boss-dogfood" actor="ceo" roles={[]} targets={[]} hosts={[]} />));
    for (let i = 0; i < 25; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });

    const aboutRun = asked.filter((u) => u.includes("wfr_other") || u.includes("dec_1"));
    expect(aboutRun.length).toBeGreaterThan(2);
    for (const u of aboutRun) {
      expect(u, u).not.toContain("project_id=boss-dogfood");
    }
    expect(aboutRun.some((u) => u.startsWith("/api/run-acts?") && u.includes("project_id=northgate"))).toBe(true);
  });

  it("falls back to the page's project for a run that names none", () => {
    expect(runProject({ project_id: "northgate" }, "p1")).toBe("northgate");
    expect(runProject({ project_id: null }, "p1")).toBe("p1");
    expect(runProject({}, "p1")).toBe("p1");
  });

  it("writes about a run (retry, skip, recheck) go to the run's project too", () => {
    const src = codeOnly("app/delivery-workflows-ui.tsx");
    expect(src).toContain("project_id: runOf ? runProject(runOf, projectId) : projectId");
    expect(src).toContain('post("recheck", { project_id: runProject(r, projectId)');
    expect(src).toContain("projectId={runProject(run, projectId)}");
  });
});
