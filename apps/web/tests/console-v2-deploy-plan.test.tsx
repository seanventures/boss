// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/run-deployments", () => ({
  loadRunDeployment: vi.fn(),
  forgetRunDeployment: vi.fn(),
}));

import { loadRunDeployment } from "../lib/run-deployments";
import { DeployControls } from "../components/v2/DeployControls";

let host: HTMLDivElement;
let root: Root;

const bound = { linked_host: "staging-host", linked_target_id: "dt_1", deployments: [] };
const plan = {
  status: "planned", deployment_run_id: "dr_new", changeset_id: "chg_new",
  next_action: "approve_and_apply_changeset",
};

function renderControls() {
  return root.render(<DeployControls runId="crun_1" appName="Demo" projectId="project_1"
    actor="executive_console" deployable deployableReason="" />);
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  vi.mocked(loadRunDeployment).mockResolvedValue(bound);
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
});

describe("v2 deployment planning controls", () => {
  it("shows a ChangeSet and approval step after a provider plan", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(plan), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await act(async () => renderControls());

    const button = host.querySelector<HTMLButtonElement>('[data-testid="v2-deploy"]')!;
    expect(button.textContent).toContain("Plan deployment");
    await act(async () => button.click());

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(host.querySelector('[data-testid="v2-controls-plan"]')?.textContent)
      .toContain("chg_new");
    expect(host.textContent).toContain("approve it, then apply it");
    expect(host.textContent).not.toContain("deployment started");
    expect(button.disabled).toBe(true);
  });

  it("shows direct-host 409 detail instead of a success message", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      error: "Community host deployment is disabled until a policy-guarded ChangeSet exists.",
    }), { status: 409 })));
    await act(async () => renderControls());
    await act(async () => host.querySelector<HTMLButtonElement>('[data-testid="v2-deploy"]')!.click());

    expect(host.querySelector('[role="alert"]')?.textContent)
      .toContain("Community host deployment is disabled");
    expect(host.querySelector('[data-testid="v2-controls-plan"]')).toBeNull();
  });
});
