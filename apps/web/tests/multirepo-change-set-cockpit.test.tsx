// @vitest-environment happy-dom
/**
 * Multi-repo screen 5, the change's cockpit (app/multirepo/change-set-cockpit.tsx), reached from
 * a build that is one lane of a change (Factory cockpit) or by `?area=code_tasks&changeset=`:
 *   - the mockup's content: crumb, chips (3 repositories, 38 min, $6.40 so far), the stage strip,
 *     one lane per repository with its steps and right side, Plan, Now, Grant → read-only sheet;
 *   - a failed lane is red with its error; the lane after it says what it waits for;
 *   - a blocked set says why and offers nothing to approve;
 *   - Stop asks first, then POSTs; a viewer sees Stop disabled and why;
 *   - an ordinary build keeps the one-repository cockpit exactly (regression).
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { needsWords } from "../lib/identity/words";
import type { ChangeSetView } from "../lib/multirepo";
import {
  $, $$, blockedSetFixture, changeSetFixture, click, mountAs, stubFetch, tid, unmount, type Role,
} from "./_helpers/multirepo-fixtures";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));
vi.mock("../app/factory/legacy-build-parts", async (orig) => ({ ...(await orig<object>()), DeploymentLine: () => <span /> }));
vi.mock("../app/deploy-ui", () => ({ LiveDeployment: () => <span /> }));

const { ChangeSetCockpit } = await import("../app/multirepo/change-set-cockpit");
const { Cockpit } = await import("../app/factory/cockpit");

afterEach(() => { unmount(); window.history.replaceState(null, "", "/"); });

function api(cs: ChangeSetView = changeSetFixture()) {
  return stubFetch((c) => {
    if (c.method === "GET" && c.path === "/v1/change-sets/rcs_1") return { body: cs };
    if (c.method === "GET" && c.path === "/v1/runs/crun_api/change-set") return { body: cs };
    if (c.method === "GET" && c.path === "/v1/change-sets/rcs_1/grant") {
      return { body: { grant: cs.grant, consent: {
        app_key: "storefront", app_name: "Storefront", run_label: "Add saved carts",
        repos: [{ full_name: "acme/contracts", access: "write", components: ["contracts"], words: "Read · Write on boss/* · Pull requests" },
                { full_name: "acme/infra", access: "read", components: ["deploy"], words: "Read only" }],
        unreachable: ["acme/mobile", "acme/payments"], how: "GitHub App token limited to these 4 repositories",
        lasts: "1 hour, renewed per step, ends with the run", never: "push to a base branch" } } };
    }
    if (c.method === "POST" && c.path === "/v1/change-sets/rcs_1/stop") return { body: { ...cs, state: "cancelled" } };
    return undefined;
  });
}
const mount = (role: Role = "owner") =>
  mountAs(<ChangeSetCockpit projectId="p1" changeSetId="rcs_1" actor="ceo" onClose={() => {}} />, role);

describe("the change's cockpit (screen 5)", () => {
  it("draws the mockup: header, stage strip, lanes, plan, now and grant", async () => {
    const calls = api();
    await mount();
    expect(calls[0]).toMatchObject({ method: "GET", path: "/v1/change-sets/rcs_1", search: "?project_id=p1" });
    expect(tid("mr-csc-crumb")?.textContent?.trim()).toBe("Factory›Storefront›Add saved carts");
    expect(tid("mr-csc-title")?.textContent).toBe("Add saved carts");
    expect(tid("mr-csc-repos")?.textContent).toBe("3 repositories");
    expect(tid("mr-csc-minutes")?.textContent).toBe("38 min");
    expect(tid("mr-csc-cost")?.textContent).toBe("$6.40 so far");
    expect($$('[data-testid^="mr-stage-"]').map((s) => `${s.textContent}:${s.dataset.state}`)).toEqual([
      "Plan:done", "2Build · 1 of 3:active", "3Review set:waiting", "4You approve:waiting", "5Deploy together:waiting"]);
    expect($$('.mr-lane').map((l) => l.dataset.testid)).toEqual(["mr-lane-1", "mr-lane-2", "mr-lane-3"]);
    expect(tid("mr-lane-1")?.textContent).toContain("acme/contracts");
    expect(tid("mr-lane-pill-1")?.textContent).toBe("PR #88 open");
    expect(tid("mr-lane-1")?.textContent).toContain("+64 −3 · 2 files");
    expect(tid("mr-lane-1-pr")?.dataset.state).toBe("done");
    expect(tid("mr-lane-2")?.dataset.now).toBe("true");
    expect(tid("mr-lane-2-tests")?.dataset.state).toBe("active");
    expect(tid("mr-lane-pill-2")?.textContent).toBe("Tests");
    expect(tid("mr-lane-pill-3")?.textContent).toBe("Waiting for api");
    expect(tid("mr-lane-wait-3")?.textContent).toBe("starts when api’s tests pass");
    expect(tid("mr-plan-state-contracts")?.textContent).toBe("done");
    expect(tid("mr-plan-state-backend")?.textContent).toBe("building");
    expect(tid("mr-plan-state-frontend")?.textContent).toBe("next");
    expect(tid("mr-csc-now")?.textContent).toContain("Tests in acme/api");
    expect(tid("mr-csc-grant")?.textContent).toBe("Grant: contracts, api, web (write on boss/*), infra (read). See consent");
  });

  it("See consent opens the grant read only: the repositories, what it cannot reach, no buttons to grant", async () => {
    const calls = api();
    await mount();
    await click(tid("mr-csc-see-consent"));
    expect(calls.some((c) => c.path === "/v1/change-sets/rcs_1/grant" && c.search === "?project_id=p1")).toBe(true);
    const sheet = tid("mr-grant-sheet")!;
    expect(sheet.textContent).toContain("Which repositories may the agents touch?");
    expect(tid("mr-grant-repo-acme/contracts")?.textContent).toContain("Write");
    expect(tid("mr-grant-repo-acme/infra")?.textContent).toContain("Read only");
    expect(sheet.textContent).toContain("acme/mobile");
    expect(sheet.textContent).toContain("Param, remembered for Storefront");
    expect(sheet.querySelector('[data-testid="consent-mr-allow"]')).toBeNull();
    expect([...sheet.querySelectorAll("button")].map((b) => b.textContent)).toEqual(["Close"]);
  });

  it("a failed lane is red with its error; the lane after it says what it waits for", async () => {
    const cs = changeSetFixture();
    cs.lanes[1] = { ...cs.lanes[1], lane_state: "failed", error: "The GitHub App lost access to acme/api. Fix in GitHub, then recheck.",
                    steps: cs.lanes[1].steps.map((s) => (s.key === "tests" ? { ...s, state: "failed" } : s)) };
    api(cs);
    await mount();
    expect(tid("mr-lane-2")?.dataset.state).toBe("failed");
    expect(tid("mr-lane-pill-2")?.textContent).toBe("Failed");
    expect(tid("mr-lane-error-2")?.textContent).toBe("The GitHub App lost access to acme/api. Fix in GitHub, then recheck.");
    expect(tid("mr-lane-2-tests")?.dataset.state).toBe("failed");
    expect(tid("mr-lane-pill-3")?.textContent).toBe("Waiting for api");
  });

  it("a blocked set shows the reason and asks for no approval", async () => {
    api(blockedSetFixture());
    await mount();
    expect(tid("mr-csc-blocked")?.textContent).toContain("Cross-repo check failed: web calls DELETE /carts/saved/{id}");
    expect(tid("mr-stage-review")?.dataset.state).toBe("failed");
    expect(document.body.textContent).not.toMatch(/Approve/);
  });

  it("Stop asks first, then stops every lane", async () => {
    const calls = api();
    await mount();
    await click(tid("mr-csc-stop"));
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);
    expect(tid("mr-csc-stop-confirm")?.textContent).toContain("Stop every lane of this change?");
    await click(tid("mr-csc-stop-yes"));
    expect(calls.find((c) => c.method === "POST")).toMatchObject({ path: "/v1/change-sets/rcs_1/stop", body: { project_id: "p1" } });
    expect(tid("mr-csc-state")?.textContent).toBe("Stopped");
    expect(tid("mr-csc-stop")).toBeNull();
  });

  it.each([["viewer"], ["auditor"]] as const)("a %s reads it all; Stop and Steer are disabled and say why", async (role) => {
    const calls = api();
    await mount(role);
    expect(tid("mr-lane-1")).not.toBeNull();
    for (const id of ["mr-csc-stop", "mr-csc-steer"]) {
      expect(tid(id)?.getAttribute("aria-disabled")).toBe("true");
      expect(tid(id)?.title).toBe(needsWords("code_task:steer"));
    }
    await click(tid("mr-csc-stop"));
    expect(tid("mr-csc-stop-confirm")).toBeNull();
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);
  });

  it("an operator may Stop", async () => {
    api();
    await mount("operator");
    expect(tid("mr-csc-stop")?.getAttribute("aria-disabled")).toBeNull();
  });
});

describe("the Factory cockpit opens the change for a lane's build", () => {
  it("a build in a multi-repo change set shows the change's cockpit", async () => {
    api();
    await mountAs(<Cockpit projectId="p1" runId="crun_api" actor="ceo" onClose={() => {}} onOpenBuild={() => {}} />, "owner");
    expect(tid("mr-csc")?.dataset.changeSet).toBe("rcs_1");
    expect(tid("fx-cockpit")).toBeNull();
  });

  it("an ordinary build keeps the one-repository cockpit exactly (404 no_change_set)", async () => {
    const { cockpit } = await import("./_helpers/factory-fixtures");
    const calls = stubFetch((c) => {
      if (c.path === "/v1/runs/crun_inv2/change-set") return { status: 404, body: { error: "no_change_set", message: "This build is not part of a change." } };
      if (c.path.endsWith("/cockpit")) return { body: cockpit() };
      return { body: {} };
    });
    await mountAs(<Cockpit projectId="p1" runId="crun_inv2" actor="ceo" onClose={() => {}} onOpenBuild={() => {}} />, "owner");
    expect(calls.some((c) => c.path === "/v1/runs/crun_inv2/change-set")).toBe(true);
    expect(tid("mr-csc")).toBeNull();
    expect($('[data-testid="fx-state"] .fx-state-word')?.textContent).toBe("Coding");
    expect($("h1")?.textContent).toBe("Invoice export");
  });

  it("a single-mode change set is the one-repository cockpit too", async () => {
    const { cockpit } = await import("./_helpers/factory-fixtures");
    stubFetch((c) => {
      if (c.path === "/v1/runs/crun_inv2/change-set") return { body: { ...changeSetFixture(), mode: "single" } };
      if (c.path.endsWith("/cockpit")) return { body: cockpit() };
      return { body: {} };
    });
    await mountAs(<Cockpit projectId="p1" runId="crun_inv2" actor="ceo" onClose={() => {}} onOpenBuild={() => {}} />, "owner");
    expect(tid("mr-csc")).toBeNull();
    expect($('[data-testid="fx-state"] .fx-state-word')?.textContent).toBe("Coding");
  });
});

describe("a change is addressable", () => {
  it("?area=code_tasks&changeset=<id> reads as the change", async () => {
    const { readFactoryUrl } = await import("../app/code-tasks-ui");
    expect(readFactoryUrl("?area=code_tasks&changeset=rcs_1&tab=prs")).toMatchObject({ changeset: "rcs_1", build: null, view: "floor" });
    expect(readFactoryUrl("?area=code_tasks")).toMatchObject({ changeset: null });
  });
});
