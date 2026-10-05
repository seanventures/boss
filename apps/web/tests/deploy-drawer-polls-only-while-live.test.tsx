// @vitest-environment happy-dom

import React, { act, Profiler } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DeployDrawer } from "../app/infra/deploy-drawer";
import { deployDetailFixture, deployRow, mockInfraFetch, type W } from "./_helpers/infra-fixtures";
import { byTestId, mount, type Mounted } from "./_helpers/infra-tab-render";

/**
 * The deploy drawer follows a moving deploy every 3 s through one ETag'd poll and stops once it
 * settles: no poll after a terminal state, and an unchanged answer (a 304) re-renders nothing.
 * The old views polled every 2-3 s with a 1 s clock forever, and the promotion line refreshed
 * the whole console every 5 s.
 */

let m: Mounted | null = null;
let restore: (() => void) | null = null;
beforeEach(() => { vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"] }); });
afterEach(async () => {
  await m?.unmount(); m = null; restore?.(); restore = null;
  vi.useRealTimers();
});

const running = (): W => deployDetailFixture("stack:sd_run");
const finished = (): W => ({
  ...running(), live: false, waiting_on: null,
  row: { ...deployRow("stack:sd_run"), state: "succeeded", word: "Live", tone: "green",
         steps: deployRow("stack:sd_live").steps, duration_ms: 90_000, finished_at: new Date().toISOString() },
  steps: (running().steps as W[]).map((s) => ({ ...s, state: "done", duration_ms: 9000 })),
});

async function tick(ms: number) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
}

describe("the deploy drawer's poll", () => {
  it("polls every 3 s while live, a 304 renders nothing, and it stops at a terminal state", async () => {
    // Answers in order: running, running twice more (unchanged → 304s), finished; then finished forever.
    const answers = [running(), running(), running(), finished()];
    let served = 0;
    const f = mockInfraFetch({ routes: [{ match: /^\/v1\/infra\/deploys\/[^/]+$/, etag: true,
      reply: () => answers[Math.min(served++, answers.length - 1)] }] });
    restore = f.install();
    let renders = 0;
    m = await mount(
      <Profiler id="drawer" onRender={() => { renders += 1; }}>
        <DeployDrawer projectId="project_1" actor="ceo" deployRef="stack:sd_run" onClose={() => {}} />
      </Profiler>, "?area=infra&view=deploys&deploy=stack%3Asd_run");
    const detailGets = () => f.gets(/^\/v1\/infra\/deploys\/[^/]+$/).length;

    await tick(0);
    expect(detailGets()).toBe(1);
    expect(byTestId("inf-deploy-state")?.textContent).toContain("Deploying");
    expect(byTestId("inf-deploy-steps")?.textContent).toContain("Now: Building image");

    const before304 = renders;
    await tick(2_900);
    expect(detailGets()).toBe(1);            // not before 3 s
    await tick(200);
    expect(detailGets()).toBe(2);            // the 304
    expect(f.calls.filter((c) => /\/v1\/infra\/deploys\/[^/]+$/.test(c.path)).length).toBe(2);
    // React may spend one bail-out pass on the first identical update after a commit; nothing
    // the poll sets changes, and from then on an unchanged answer renders nothing at all.
    expect(renders - before304).toBeLessThanOrEqual(1);
    const steady = renders;
    const steps = byTestId("inf-deploy-steps");
    await tick(3_000);
    expect(detailGets()).toBe(3);            // the second 304
    expect(renders).toBe(steady);
    expect(byTestId("inf-deploy-steps")).toBe(steps);

    await tick(3_000);
    expect(detailGets()).toBe(4);
    expect(byTestId("inf-deploy-state")?.textContent).toContain("Live");

    await tick(60_000);
    expect(detailGets()).toBe(4);            // settled: no more reads
  });

  it("a deploy that is already terminal is read once", async () => {
    const f = mockInfraFetch();
    restore = f.install();
    m = await mount(<DeployDrawer projectId="project_1" actor="ceo" deployRef="stack:sd_failed" onClose={() => {}} />);
    await tick(0);
    expect(byTestId("inf-deploy-failed")?.textContent).toContain("Migration failed");
    await tick(30_000);
    expect(f.gets(/^\/v1\/infra\/deploys\/[^/]+$/)).toHaveLength(1);
  });
});
