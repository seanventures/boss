// @vitest-environment happy-dom
/**
 * D3: a link to one step of a run (`?wfr=<run>&step=<id>`, the Log's `Step ↗`) opens the run
 * on THAT step — it opened on the run's own pick (the failed step), whatever the link said.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { standingOf } from "./_helpers/run-standing";

vi.mock("../app/workflows/studio-parts", () => import("./_helpers/studio-parts-stub"));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
(globalThis as Record<string, unknown>).React = React;
const { RunsView } = await import("../app/delivery-workflows-ui");

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});

const step = (step_id: string, status: string) => ({
  step_id, kind: "capability", status, capability: "notify", output: {}, evidence: [], error: null, approval: {},
  due_at: null, timeout_applied: null, attempt: 1, started_at: null, completed_at: null, decision_id: null });
const RUN = { id: "wfr_1", status: "failed", standing: standingOf("failed"), workflow_key: "k", workflow_name: "W",
              version: 1, steps: [step("plan", "succeeded"), step("build", "succeeded"), step("review", "failed")],
              step_count: 3, steps_done: 2, current_step: null, waiting_on: null, blocked_reason: null, error: null,
              updated_at: null, actions: [], input: { title: "Lead triage" } };

async function open(search: string) {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({})));
  window.history.replaceState(null, "", `/${search}`);
  const noop = () => {};
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(
    <RunsView runs={[RUN] as never} openRun="wfr_1" setOpenRun={noop} onAdvance={noop} busy={null}
              capabilities={[]} onRetryStep={noop} onSkipStep={noop} onRerun={noop} onStop={noop} onDelete={noop}
              onStopAndRerun={noop} onBuildOnThis={noop} onRecheck={noop} onSetTarget={noop} onPrepareTarget={noop}
              onRunPreview={noop} onBound={noop} projectId="p" actor="ceo" roles={[]} targets={[]} hosts={[]} />));
  for (let i = 0; i < 10; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

const selected = () => [...document.querySelectorAll("[data-step-id][data-selected='true']")]
  .map((n) => n.getAttribute("data-step-id"));

describe("a step link", () => {
  it("lands on the linked step, not the run's own pick", async () => {
    await open("?area=delivery_workflows&wfr=wfr_1&step=build");
    expect(selected()).toEqual(["build"]);
  });

  it("without a step, the run still opens where it wants attention", async () => {
    await open("?area=delivery_workflows&wfr=wfr_1");
    expect(selected()).toEqual(["review"]);
  });

  it("a step of another run, or one this run doesn't have, changes nothing", async () => {
    await open("?area=delivery_workflows&wfr=wfr_2&step=build");
    expect(selected()).toEqual(["review"]);
  });
});
