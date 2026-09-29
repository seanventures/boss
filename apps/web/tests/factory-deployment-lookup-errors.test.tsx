// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import type { CodeTaskRun } from "@boss/api-client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/run-deployments", () => ({
  loadRunDeployment: vi.fn(),
  forgetRunDeployment: vi.fn(),
}));

import { DeploymentLine } from "../app/code-tasks-ui";
import { notifyChanged } from "../app/changed";
import { loadRunDeployment, forgetRunDeployment } from "../lib/run-deployments";

const run = { id: "crun_one", title: "Demo", status: "completed" } as CodeTaskRun;
const failedDeployment = {
  linked_host: "staging-host",
  linked_target_id: "dt_one",
  deployments: [{ id: "dr_one", status: "failed", host_name: "staging-host",
    deployment_target_id: "dt_one", failure_reason: "health probe failed" }],
};

let host: HTMLDivElement;
let root: Root;

function actionIds(): string[] {
  return ["app-deploy-first", "app-target-retire", "app-setup-deployment",
    "app-deployment-retry", "app-deployment-unbind"];
}

function renderLine() {
  root.render(<DeploymentLine run={run} projectId="project_one"
    actor="executive_console" deployable />);
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
});

describe("Factory deployment lookup failures", () => {
  it("shows checking without offering a deployment action before the first answer", async () => {
    vi.mocked(loadRunDeployment).mockImplementation(() => new Promise(() => {}));
    await act(async () => renderLine());
    expect(host.querySelector('[data-testid="app-deployment-checking"]')).not.toBeNull();
    for (const id of actionIds()) expect(host.querySelector(`[data-testid="${id}"]`)).toBeNull();
  });

  it("shows an initial error and enables only Retry check", async () => {
    vi.mocked(loadRunDeployment)
      .mockResolvedValueOnce({ error: "HTTP 502" })
      .mockResolvedValueOnce(failedDeployment);
    await act(async () => renderLine());
    expect(host.querySelector('[data-testid="app-deployment-unreadable"]')).not.toBeNull();
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("HTTP 502");
    for (const id of actionIds()) expect(host.querySelector(`[data-testid="${id}"]`)).toBeNull();

    await act(async () => {
      host.querySelector<HTMLButtonElement>('[data-testid="app-deployment-check-retry"]')!.click();
    });
    expect(forgetRunDeployment).toHaveBeenCalledWith("crun_one");
    expect(host.querySelector('[data-testid="app-deployment-unreadable"]')).toBeNull();
    expect(host.querySelector('[data-testid="app-deployment-retry"]')).not.toBeNull();
  });

  it("clears a previously loaded binding and actions after a failed refresh", async () => {
    vi.mocked(loadRunDeployment)
      .mockResolvedValueOnce(failedDeployment)
      .mockResolvedValueOnce({ error: "deployment lookup timed out" });
    await act(async () => renderLine());
    expect(host.querySelector('[data-testid="app-deployment-retry"]')).not.toBeNull();
    expect(host.querySelector('[data-testid="app-deployment-unbind"]')).not.toBeNull();

    await act(async () => notifyChanged("workflows"));
    expect(forgetRunDeployment).toHaveBeenCalledWith("crun_one");
    expect(host.querySelector('[data-testid="app-deployment-unreadable"]')).not.toBeNull();
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("timed out");
    for (const id of actionIds()) expect(host.querySelector(`[data-testid="${id}"]`)).toBeNull();
  });
});
