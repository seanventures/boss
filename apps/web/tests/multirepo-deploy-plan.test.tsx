// @vitest-environment happy-dom
/**
 * Multi-repo screen 8, the deploy plan (app/multirepo/deploy-plan.tsx) in Infra › Deploys:
 *   - the mockup: crumb, the sentence, "Target: production · eu-west-1", "Deploy all 3", the waves
 *     (order, component, repo, from → to, steps, rollback), One rollback in reverse order, the
 *     notes Automatic rollback and Merged together;
 *   - Deploy all POSTs deploy (deployment:create); a plan that is not ready says its problem and
 *     Deploy all is disabled with that reason;
 *   - Roll back all asks first, then POSTs rollback (deployment:rollback);
 *   - a viewer reads it with both disabled and why;
 *   - Deploys lists the change-set releases at the top and `changeset=` opens the plan.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { needsWords } from "../lib/identity/words";
import type { DeployPlan } from "../lib/multirepo";
import {
  $$, changeSetFixture, click, deployPlanFixture, mountAs, stubFetch, tid, unmount, type Call, type Role,
} from "./_helpers/multirepo-fixtures";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));

const { DeployPlanView, ChangeSetReleases } = await import("../app/multirepo/deploy-plan");

afterEach(unmount);

function api(plan: DeployPlan = deployPlanFixture(), post?: (c: Call) => { status?: number; body: unknown }) {
  return stubFetch((c) => {
    if (c.method === "GET" && c.path === "/v1/change-sets/rcs_1/deploy-plan") return { body: plan };
    if (c.method === "POST" && post) return post(c);
    if (c.method === "GET" && c.path === "/v1/projects/p1/change-sets") {
      return { body: { items: [{ ...changeSetFixture(), state: "merged" }, { ...changeSetFixture(), id: "rcs_0", title: "Storefront: Fix price rounding", state: "live" }] } };
    }
    return undefined;
  });
}
const mount = (role: Role = "owner", onBack = () => {}) =>
  mountAs(<DeployPlanView projectId="p1" changeSetId="rcs_1" onBack={onBack} />, role);

describe("the deploy plan (screen 8)", () => {
  it("draws the mockup: target, waves in order, one rollback, the notes", async () => {
    const calls = api();
    await mount();
    expect(calls[0]).toMatchObject({ path: "/v1/change-sets/rcs_1/deploy-plan", search: "?project_id=p1" });
    expect(tid("mr-deploy-plan")?.textContent).toContain("Storefront · Add saved carts");
    expect(tid("mr-dp-lead")?.textContent).toBe(
      "Three components ship as one release, in order. If any step fails, everything goes back to what is live now.");
    expect(tid("mr-dp-target")?.textContent).toBe("Target: production · eu-west-1");
    expect(tid("mr-dp-deploy")?.textContent).toBe("Deploy all 3");
    expect((tid("mr-dp-deploy") as HTMLButtonElement).disabled).toBe(false);
    expect($$(".mr-wave").map((w) => w.dataset.testid)).toEqual(["mr-wave-1", "mr-wave-2", "mr-wave-3"]);
    expect(tid("mr-wave-1")?.textContent).toContain("Contracts");
    expect(tid("mr-wave-1")?.textContent).toContain("acme/contracts");
    expect(tid("mr-wave-ver-2")?.textContent).toBe("v2.31.0 → v2.32.0");
    expect(tid("mr-wave-2")?.textContent).toContain("Health check must pass, else this wave rolls back");
    expect(tid("mr-wave-2")?.textContent).toContain("Rollback: Previous release v2.31.0");
    expect($$('[data-testid^="mr-dp-rb-"]').map((r) => r.dataset.testid)).toEqual(["mr-dp-rb-3", "mr-dp-rb-2", "mr-dp-rb-1"]);
    expect(tid("mr-dp-rb-2")?.textContent).toContain("backend → Previous release v2.31.0");
    expect(tid("mr-dp-rb-2")?.textContent).toContain("ready");
    expect(tid("mr-dp-auto")?.textContent).toContain("Automatic rollback If a deploy fails or its health check fails");
    expect(tid("mr-dp-merged-together")?.textContent).toContain("Merged together: the three PRs merge only after all pass at once.");
  });

  it("Deploy all starts the release and shows the plan the server answers", async () => {
    const calls = api(deployPlanFixture(), (c) => ({ body: deployPlanFixture({ state: "deploying", status: "deploying",
      waves: deployPlanFixture().waves.map((w, i) => (i === 0 ? { ...w, status: "live" } : w)) }) }));
    await mount();
    await click(tid("mr-dp-deploy"));
    expect(calls.find((c) => c.method === "POST")).toMatchObject({ path: "/v1/change-sets/rcs_1/deploy", body: { project_id: "p1" } });
    expect(tid("mr-dp-state")?.textContent).toBe("Deploying");
    expect(tid("mr-dp-deploy")).toBeNull();
    expect(tid("mr-wave-1")?.dataset.status).toBe("live");
  });

  it("a plan that is not ready says why and Deploy all is disabled with that reason", async () => {
    const problem = "frontend has nowhere to ship: add a machine in Infra › Hosts (it needs only a machine), or choose its target in the app's Components.";
    const calls = api(deployPlanFixture({ ready: false, problems: [problem], host: null }));
    await mount();
    expect(tid("mr-dp-problems")?.textContent).toBe(problem);
    const btn = tid("mr-dp-deploy") as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    expect(btn.title).toBe(problem);
    expect(tid("mr-dp-target")?.textContent).toBe("Target: production");
    await click(btn);
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);
  });

  it("Roll back all asks first, then rolls every component back", async () => {
    const live = deployPlanFixture({ state: "live", status: "live" });
    const calls = api(live, () => ({ body: { ...live, state: "rolled_back", status: "rolled_back" } }));
    await mount();
    expect(tid("mr-dp-deploy")).toBeNull();
    await click(tid("mr-dp-rollback-all"));
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);
    expect(tid("mr-dp-rollback-confirm")?.textContent).toContain("Roll back all 3 components to their previous release, newest first?");
    await click(tid("mr-dp-rollback-yes"));
    expect(calls.find((c) => c.method === "POST")).toMatchObject({ path: "/v1/change-sets/rcs_1/rollback", body: { project_id: "p1" } });
    expect(tid("mr-dp-state")?.textContent).toBe("Rolled back");
    expect((tid("mr-dp-rollback-all") as HTMLButtonElement).disabled).toBe(true);
  });

  it("nothing deployed yet: Roll back all is disabled and says so", async () => {
    api();
    await mount();
    const btn = tid("mr-dp-rollback-all") as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    expect(btn.title).toBe("Nothing is deployed yet, so there is nothing to roll back");
  });

  it("a refused deploy shows the API's sentence", async () => {
    api(deployPlanFixture(), () => ({ status: 409, body: { error: "not_merged", message: "Only a merged change can be deployed." } }));
    await mount();
    await click(tid("mr-dp-deploy"));
    expect(tid("mr-dp-action-error")?.textContent).toBe("Only a merged change can be deployed.");
  });

  it.each([["viewer"], ["auditor"]] as const)("a %s reads the plan; Deploy all and Roll back all are disabled and say why", async (role) => {
    const calls = api(deployPlanFixture());
    await mount(role);
    expect(tid("mr-wave-3")).not.toBeNull();
    expect(tid("mr-dp-deploy")?.getAttribute("aria-disabled")).toBe("true");
    expect(tid("mr-dp-deploy")?.title).toBe(needsWords("deployment:create"));
    await click(tid("mr-dp-deploy"));
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);
  });

  it("a viewer cannot roll back a live release", async () => {
    api(deployPlanFixture({ state: "live" }));
    await mount("viewer");
    expect(tid("mr-dp-rollback-all")?.getAttribute("aria-disabled")).toBe("true");
    expect(tid("mr-dp-rollback-all")?.title).toBe(needsWords("deployment:rollback"));
  });

  it("an operator may deploy and roll back", async () => {
    api(deployPlanFixture({ state: "live" }));
    await mount("operator");
    expect(tid("mr-dp-rollback-all")?.getAttribute("aria-disabled")).toBeNull();
  });
});

describe("Deploys lists the change-set releases", () => {
  it("one row per release, opening its plan", async () => {
    const calls = api();
    const onOpen = vi.fn();
    await mountAs(<ChangeSetReleases projectId="p1" onOpen={onOpen} />, "viewer");
    const list = calls.find((c) => c.path === "/v1/projects/p1/change-sets")!;
    expect(new URLSearchParams(list.search).get("state")).toBe("merged,deploying,live,rolled_back");
    expect(new URLSearchParams(list.search).get("mode")).toBe("multi");
    expect(tid("mr-release-rcs_1")?.textContent).toContain("Merged");
    expect(tid("mr-release-rcs_1")?.textContent).toContain("contracts · api · web");
    expect(tid("mr-release-rcs_0")?.textContent).toContain("Live");
    await click(tid("mr-release-rcs_1"));
    expect(onOpen).toHaveBeenCalledWith("rcs_1");
  });

  it("no releases: nothing is drawn", async () => {
    stubFetch(() => ({ body: { items: [] } }));
    await mountAs(<ChangeSetReleases projectId="p1" onOpen={() => {}} />, "owner");
    expect(tid("mr-releases")).toBeNull();
  });

  it("?area=infra&tab=deploys&changeset=<id> reads as the Deploys tab with the change", async () => {
    const { readInfraUrl } = await import("../lib/infra-view");
    expect(readInfraUrl("?area=infra&tab=deploys&changeset=rcs_1")).toMatchObject({ view: "deploys", changeset: "rcs_1" });
  });
});
