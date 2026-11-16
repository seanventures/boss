// @vitest-environment happy-dom
/**
 * Promote stays on the deployments tab, and only for a run the existing action can send.
 * promoteDeploymentAction posts deployment_run_id to /v1/deployments/{id}/promote, which
 * accepts a staging deployment_runs row whose status is succeeded, deployed, or completed.
 * The tab already receives that row as environments[].deploy.attempt when kind is "registry".
 * A stack deployment id is not that row. This test refuses a button that invents one.
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
}));
vi.mock("../app/actions", () => ({
  promoteDeploymentAction: vi.fn(async () => ({ ok: true })),
}));

import { promoteDeploymentAction } from "../app/actions";
import { DeploymentsTab } from "../app/projects/tab-deployments";
import type { PxCtx } from "../app/projects/project-page";
import { FX_DEPLOYMENTS } from "../lib/project-experience-fixtures";
import { click, mountAs, stubFetch, tid, unmount, type Call } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const RUN = "deprun_ok";
const ctx = {
  projectId: "helpdesk-ai-demo", actor: "ceo", orgId: "org_1", demo: false,
  overview: { project: { live_url: "" } }, work: null, workState: "ready",
  refreshWork: () => {}, onboarding: null, tab: () => {}, open: () => {}, ask: () => {},
} as unknown as PxCtx;

function attempt(id: string, kind: string, status: string) {
  return { id, kind, status, commit: "a41c9e2", at: "2026-11-08T12:00:00Z" };
}

function payload(envs: Record<string, unknown>[]) {
  return { ...FX_DEPLOYMENTS, environments: envs.length ? envs : FX_DEPLOYMENTS.environments };
}

function staging(over: Record<string, unknown> = {}) {
  return { ...FX_DEPLOYMENTS.environments[0], target_id: "dept_1", key: "staging", ...over };
}

function engine(body: unknown) {
  return stubFetch((c: Call) => (c.path.endsWith("/deployments") ? { body } : { body: {} }));
}

async function open(body: unknown) {
  engine(body);
  await mountAs(<DeploymentsTab ctx={ctx} />, "owner");
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

afterEach(() => {
  unmount();
  vi.mocked(promoteDeploymentAction).mockClear();
});

describe("a succeeded staging deployment promotes from its card", () => {
  it("calls promoteDeploymentAction with the run id the payload already carries", async () => {
    await open(payload([staging({
      deploy: { state: "live", word: "Live", live: true, reason: "",
                attempt: attempt(RUN, "registry", "succeeded"), progress: null },
    })]));
    expect(tid("px-env-staging")).not.toBeNull();
    const button = tid("px-env-promote-staging");
    expect(button).not.toBeNull();
    expect(button!.textContent).toContain("Promote");
    expect(button!.getAttribute("data-deployment-run-id")).toBe(RUN);
    expect(button!.getAttribute("data-permission")).toBe("deployment:production");
    await click(button);
    expect(promoteDeploymentAction).toHaveBeenCalledTimes(1);
    const fd = vi.mocked(promoteDeploymentAction).mock.calls[0][1] as FormData;
    expect(fd.get("deployment_run_id")).toBe(RUN);
    expect(fd.get("actor")).toBe("ceo");
  });

  it("also promotes a staging run the endpoint already treats as succeeded", async () => {
    for (const status of ["deployed", "completed"]) {
      unmount();
      await open(payload([staging({
        deploy: { state: "live", word: "Live", live: true, reason: "",
                  attempt: attempt(`deprun_${status}`, "registry", status), progress: null },
      })]));
      expect(tid("px-env-promote-staging")!.getAttribute("data-deployment-run-id")).toBe(`deprun_${status}`);
    }
  });

  it("draws no button when the payload has no promotable staging run id", async () => {
    const stackLive = staging({
      deploy: { state: "live", word: "Live", live: true, reason: "",
                attempt: attempt("sdep_2", "stack", "healthy"), progress: null },
    });
    const failedRun = staging({
      deploy: { state: "failed", word: "Last deploy failed", live: false, reason: "smoke",
                attempt: attempt("deprun_bad", "registry", "failed"), progress: null },
    });
    const prodRun = { ...staging(), key: "production", label: "Production",
      deploy: { state: "live", word: "Live", live: true, reason: "",
                attempt: attempt("deprun_prod", "registry", "succeeded"), progress: null } };
    for (const body of [payload([]), payload([stackLive]), payload([failedRun]), payload([prodRun])]) {
      unmount();
      await open(body);
      expect(tid("px-env-promote-staging")).toBeNull();
      expect(document.querySelector("[data-testid^='px-env-promote-']")).toBeNull();
      expect(promoteDeploymentAction).not.toHaveBeenCalled();
    }
  });
});
