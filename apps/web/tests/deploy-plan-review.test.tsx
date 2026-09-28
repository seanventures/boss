// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn() }),
}));

vi.mock("../app/actions", () => ({
  deployTargetAction: vi.fn(),
  getDeliveryDefaultsAction: vi.fn(async () => ({ owner: "example" })),
  getDeploymentAction: vi.fn(),
  listRefsAction: vi.fn(async () => ({ branches: [], default_branch: "main" })),
  redeployAction: vi.fn(),
  setAutoDeployAction: vi.fn(),
}));

import {
  deployTargetAction, getDeploymentAction, redeployAction,
} from "../app/actions";
import { DeployWizard, LiveDeployment, RecentDeployments } from "../app/deploy-ui";
import { TargetControls } from "../app/delivery-manage";

let host: HTMLDivElement;
let root: Root;

const target = {
  id: "dt_1", app_name: "Demo", environment: "staging", repo: "example/demo",
  status: "active", target_kind: "digitalocean_app", target_ref: "app_1",
};
const previousRun = {
  id: "dr_old", status: "succeeded", app_name: "Demo", environment: "staging",
  commit_ref: "abc123", trigger: "manual", duration_ms: 200,
  finished_at: "2026-09-28T00:00:00Z", failure_reason: null, url: null,
};
const plan = {
  deployment_run_id: "dr_new", status: "planned", changeset_id: "chg_new",
  next_action: "approve_and_apply_changeset",
};

function button(label: string) {
  const found = Array.from(host.querySelectorAll("button"))
    .find((item) => item.textContent?.trim() === label);
  if (!found) throw new Error(`missing button: ${label}`);
  return found;
}

async function chooseTarget() {
  await act(async () => button("Plan a deployment").click());
  const select = host.querySelector<HTMLSelectElement>("select")!;
  await act(async () => {
    select.value = "dt_1";
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
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

describe("deployment ChangeSet review flow", () => {
  it("shows the planned ChangeSet and next step without claiming execution", async () => {
    vi.mocked(deployTargetAction).mockResolvedValue(plan);
    await act(async () => root.render(<DeployWizard targets={[target]} />));
    await chooseTarget();
    expect(host.textContent).toContain("For a provider-backed target, BOSS creates a ChangeSet");

    await act(async () => button("Create deployment plan").click());
    expect(host.textContent).toContain("Deployment plan created");
    expect(host.textContent).toContain("chg_new");
    expect(host.textContent).toContain("approve it, then apply it");
    expect(host.textContent).toContain("Nothing has deployed");
    expect(host.querySelector<HTMLAnchorElement>('a[href*="area=infra"]')).not.toBeNull();
    expect(getDeploymentAction).not.toHaveBeenCalled();
  });

  it("shows the API's direct-host 409 reason in the wizard", async () => {
    vi.mocked(deployTargetAction).mockResolvedValue({
      error: "Community host deployment is disabled until a policy-guarded ChangeSet exists.",
    });
    await act(async () => root.render(<DeployWizard targets={[target]} />));
    await chooseTarget();
    await act(async () => button("Create deployment plan").click());

    expect(host.querySelector('[role="alert"]')?.textContent)
      .toContain("Community host deployment is disabled");
    expect(host.textContent).not.toContain("Deployment plan created");
  });

  it("shows the redeploy ChangeSet beside its source run", async () => {
    vi.mocked(redeployAction).mockResolvedValue(plan);
    await act(async () => root.render(<RecentDeployments deployments={[previousRun]} />));
    await act(async () => button("plan redeploy").click());

    expect(host.textContent).toContain("chg_new");
    expect(host.textContent).toContain("approve it, then apply it");
    expect(getDeploymentAction).not.toHaveBeenCalled();
  });

  it("shows a redeploy 409 instead of silently dropping it", async () => {
    vi.mocked(redeployAction).mockResolvedValue({
      error: "Direct host deployment is disabled until a policy-guarded ChangeSet exists.",
    });
    await act(async () => root.render(<RecentDeployments deployments={[previousRun]} />));
    await act(async () => button("plan redeploy").click());

    expect(host.querySelector('[role="alert"]')?.textContent)
      .toContain("Direct host deployment is disabled");
    expect(host.textContent).not.toContain("Deployment plan created");
  });

  it("renders an unapproved run as awaiting review, not deployed", async () => {
    const onFinished = vi.fn();
    vi.mocked(getDeploymentAction).mockResolvedValue({
      deployment_run_id: "dr_new", status: "created", app_name: "Demo",
      environment: "staging", running: true, steps: [], duration_ms: null,
      url: "demo.example.test",
    });
    await act(async () => root.render(<LiveDeployment runId="dr_new" onFinished={onFinished} />));

    expect(host.textContent).toContain("Awaiting ChangeSet review");
    expect(host.textContent).toContain("No deployment is running");
    expect(host.textContent).not.toContain("Deployed");
    expect(host.textContent).not.toContain("Open demo.example.test");
    expect(onFinished).not.toHaveBeenCalled();
  });

  it("shows the target-card ChangeSet instead of an immediate deploy", async () => {
    vi.mocked(deployTargetAction).mockResolvedValue(plan);
    await act(async () => root.render(
      <TargetControls target={target} communityEdition={false} />,
    ));
    await act(async () => button("Plan deployment").click());

    expect(host.textContent).toContain("Deployment plan created");
    expect(host.textContent).toContain("chg_new");
    expect(host.textContent).not.toContain("Deploying");
    expect(button("Plan deployment").hasAttribute("disabled")).toBe(true);
    expect(getDeploymentAction).not.toHaveBeenCalled();
  });

  it("shows a target-card 409 reason", async () => {
    vi.mocked(deployTargetAction).mockResolvedValue({
      error: "Direct host deployment is disabled until a policy-guarded ChangeSet exists.",
    });
    await act(async () => root.render(
      <TargetControls target={target} communityEdition={false} />,
    ));
    await act(async () => button("Plan deployment").click());

    expect(host.querySelector('[role="alert"]')?.textContent)
      .toContain("Direct host deployment is disabled");
    expect(host.textContent).not.toContain("Deployment plan created");
  });
});
