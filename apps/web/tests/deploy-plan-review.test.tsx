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
// A host target's plan (governed_host_deploy.py): one decision in Approvals, then BOSS applies.
const hostPlan = {
  deployment_run_id: "dr_host", status: "planned", changeset_id: "chg_host",
  next_action: "approve_decision", decision_id: "dec_host",
  plan_review: {
    app_name: "Lead-Triage Inbox", environment: "staging", host: "hetzner-ci-01",
    commit_ref: "0123456789abcdef", app_port: 3010,
    steps: ["Preflight the host (read-only)", "Build the app at the commit",
            "Transfer and start it", "Health check"],
    secret_refs: [{ name: "ssh_key", ref: "sref_host" }],
    rollback: ["If the health check fails, BOSS restores the previous release on the host."],
    cost: { expected_cost_usd_micros: 0, note: "Your own host: no provider cost." },
  },
};
const notAttached = "Demo is not attached to a deployment host. "
  + "Attach the app to a verified host in Infra, then plan again.";

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

  it("shows a host plan's review and links its approval, without claiming a deploy", async () => {
    vi.mocked(deployTargetAction).mockResolvedValue(hostPlan);
    await act(async () => root.render(<DeployWizard targets={[target]} />));
    await chooseTarget();
    await act(async () => button("Create deployment plan").click());

    expect(host.textContent).toContain("Plan ready — waiting for approval");
    const review = host.querySelector('[data-testid="plan-review"]')!.textContent!;
    expect(review).toContain("Lead-Triage Inbox");
    expect(review).toContain("hetzner-ci-01 · staging");
    expect(review).toContain("0123456789ab");
    expect(review).toContain("Rollback: If the health check fails");
    expect(review).toContain("Cost: $0.00");
    expect(review).toContain("Secrets (by reference): ssh_key");
    expect(host.textContent).toContain("Nothing has deployed");
    expect(host.querySelector<HTMLAnchorElement>('a[href*="area=decisions"]')?.textContent)
      .toContain("Approve it in Approvals");
    expect(getDeploymentAction).not.toHaveBeenCalled();
  });

  it("shows the API's 409 reason in the wizard", async () => {
    vi.mocked(deployTargetAction).mockResolvedValue({ error: notAttached });
    await act(async () => root.render(<DeployWizard targets={[target]} />));
    await chooseTarget();
    await act(async () => button("Create deployment plan").click());

    expect(host.querySelector('[role="alert"]')?.textContent)
      .toContain("not attached to a deployment host");
    expect(host.textContent).not.toContain("Deployment plan created");
    expect(host.textContent).not.toContain("Plan ready");
  });

  it("shows the redeploy ChangeSet beside its source run", async () => {
    vi.mocked(redeployAction).mockResolvedValue(plan);
    await act(async () => root.render(<RecentDeployments deployments={[previousRun]} />));
    await act(async () => button("plan redeploy").click());

    expect(host.textContent).toContain("chg_new");
    expect(host.textContent).toContain("approve it, then apply it");
    expect(getDeploymentAction).not.toHaveBeenCalled();
  });

  it("shows a host redeploy's plan review beside its source run", async () => {
    vi.mocked(redeployAction).mockResolvedValue(hostPlan);
    await act(async () => root.render(<RecentDeployments deployments={[previousRun]} />));
    await act(async () => button("plan redeploy").click());

    expect(host.textContent).toContain("Plan ready — waiting for approval");
    expect(host.querySelector('[data-testid="plan-review"]')?.textContent)
      .toContain("hetzner-ci-01");
    expect(getDeploymentAction).not.toHaveBeenCalled();
  });

  it("shows a redeploy 409 instead of silently dropping it", async () => {
    vi.mocked(redeployAction).mockResolvedValue({ error: notAttached });
    await act(async () => root.render(<RecentDeployments deployments={[previousRun]} />));
    await act(async () => button("plan redeploy").click());

    expect(host.querySelector('[role="alert"]')?.textContent)
      .toContain("not attached to a deployment host");
    expect(host.textContent).not.toContain("Deployment plan created");
  });

  it("shows a host run waiting for approval with its plan and the Approvals link", async () => {
    vi.mocked(getDeploymentAction).mockResolvedValue({
      deployment_run_id: "dr_host", status: "awaiting_approval", app_name: "Lead-Triage Inbox",
      environment: "staging", running: false, steps: [], duration_ms: null,
      url: "203.0.113.5:3010", governed: true, decision_id: "dec_host",
      plan_review: hostPlan.plan_review,
    });
    await act(async () => root.render(<LiveDeployment runId="dr_host" />));

    expect(host.textContent).toContain("Waiting for approval");
    expect(host.textContent).toContain("Nothing runs until it is approved");
    expect(host.querySelector('a[href*="area=decisions"]')).not.toBeNull();
    expect(host.textContent).not.toContain("Open 203.0.113.5:3010");
  });

  it("shows an approved host run's steps, logs and URL once it is healthy", async () => {
    vi.mocked(getDeploymentAction).mockResolvedValue({
      deployment_run_id: "dr_host", status: "succeeded", app_name: "Lead-Triage Inbox",
      environment: "staging", running: false, duration_ms: 42000, url: "203.0.113.5:3010",
      governed: true, decision_id: "dec_host",
      steps: [
        { step: "preflight", label: "Preflight checks", status: "ok", duration_ms: 900, log: "ready" },
        { step: "observe", label: "Health check", status: "ok", duration_ms: 300 },
      ],
    });
    await act(async () => root.render(<LiveDeployment runId="dr_host" />));

    expect(host.textContent).toContain("Deployed");
    expect(host.textContent).toContain("Preflight checks");
    expect(host.textContent).toContain("Health check");
    expect(host.querySelector<HTMLAnchorElement>('a[href="http://203.0.113.5:3010/"]'))
      .not.toBeNull();
  });

  it("says a host run rolled back when its health check failed", async () => {
    vi.mocked(getDeploymentAction).mockResolvedValue({
      deployment_run_id: "dr_host", status: "rolled_back", app_name: "Lead-Triage Inbox",
      environment: "staging", running: false, duration_ms: 42000, url: "203.0.113.5:3010",
      failure_reason: "The health check failed after release; BOSS rolled the host back.",
      steps: [{ step: "rollback", label: "Roll back", status: "ok", duration_ms: 500 }],
    });
    await act(async () => root.render(<LiveDeployment runId="dr_host" />));

    expect(host.textContent).toContain("Rolled back");
    expect(host.textContent).not.toContain("Open 203.0.113.5:3010");
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

  // The target card (delivery-manage.tsx TargetControls) retired into Infra › Deploys. Its two
  // cases — a plan is shown as planned and awaiting approval, never as deploying; a refusal is
  // shown in words — are held by infra-deploys-a-repository-to-a-vm-through-an-approval.test.tsx
  // ("plans the chosen ref onto the chosen target through the routed deploy, then opens its
  // plan" and "shows the API's refusal of a plan in words").
});
