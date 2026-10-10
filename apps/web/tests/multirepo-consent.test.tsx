// @vitest-environment happy-dom
/**
 * The consent sheet's multi-repo layout (screen 9, components/identity/consent-sheet.tsx):
 *   - a 409 consent_required WITHOUT `multirepo` is exactly the sheet it was (regression);
 *   - WITH `multirepo` it asks "Which repositories may the agents touch?": the repositories
 *     allowed for this run with Write/Read and their words, the ones not reachable, who uses it,
 *     How / Lasts / Never, "Remember for Storefront" and "Allow 4 repositories";
 *   - Remember ticked creates the standing grant; unticked, the task grant (ends with the run);
 *   - Cancel decides nothing.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import React from "react";

import { ConsentSheet } from "../components/identity/consent-sheet";
import { consentFromBody, type ConsentRequest } from "../lib/identity/consent";
import { $$, click, mountAs, stubFetch, tid, unmount, type Call } from "./_helpers/multirepo-fixtures";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));

afterEach(unmount);

const BUILD_409 = {
  error: "consent_required", task: "Add saved carts", task_run_id: "pending:Ab12",
  agents: [{ id: "agt_helm", name: "Helm", role_key: "builder" }, { id: "agt_vera", name: "Vera", role_key: "reviewer" },
           { id: "agt_beacon", name: "Beacon", role_key: "release_manager" }],
  permissions: ["code_task:create"], projects: ["p1"],
  github: { repo: "acme/web", repos: ["acme/contracts", "acme/api", "acme/web", "acme/infra"],
            permissions: { contents: "write", pull_requests: "write" }, summary: "code write · pushes only to boss/*" },
  durations: [{ kind: "task", label: "For this build only", days: null }, { kind: "standing", label: "For 30 days", days: 30 }],
  max_days: 90,
};
const MULTIREPO = {
  app_key: "storefront", app_name: "Storefront", run_label: "R-1482",
  repos: [
    { full_name: "acme/contracts", access: "write", components: ["contracts"], words: "Read · Write on boss/r1482-* · Pull requests" },
    { full_name: "acme/api", access: "write", components: ["backend"], words: "Read · Write on boss/r1482-* · Pull requests" },
    { full_name: "acme/web", access: "write", components: ["frontend"], words: "Read · Write on boss/r1482-* · Pull requests" },
    { full_name: "acme/infra", access: "read", components: ["deploy"], words: "Read only · for the deploy plan" },
  ],
  unreachable: ["acme/docs-site", "acme/legacy-admin", "acme/ml-experiments", "acme/mobile", "acme/payments"],
  how: "GitHub App token limited to these 4 repositories", lasts: "1 hour, renewed per step, ends with the run",
  never: "push to a base branch, change settings, delete branches it did not create",
};

function grants() {
  return stubFetch((c: Call) => (c.method === "POST" && c.path === "/v1/identity/me/delegations" ? { status: 201, body: { id: "dlg_1" } } : undefined));
}
async function open(body: unknown) {
  const request = consentFromBody(body)!;
  const decided: Array<string | null> = [];
  const closed: number[] = [];
  const req: ConsentRequest = { ...request, onDecided: (id) => decided.push(id) };
  await mountAs(<ConsentSheet request={req} onClose={() => closed.push(1)} />, "owner");
  return { decided, closed, request };
}

describe("consent without multirepo (unchanged)", () => {
  it("is the sheet it was: who acts, the durations, a task grant", async () => {
    const calls = grants();
    const { decided, request } = await open(BUILD_409);
    expect(request.multirepo).toBeUndefined();
    expect(tid("consent-multirepo")).toBeNull();
    expect(document.querySelector("h2")?.textContent).toBe("Let Helm, Vera and Beacon act for you?");
    expect(tid("consent-durations")?.textContent).toBe("For this build onlyFor 30 days");
    await click(tid("consent-task"));
    expect(calls[0].body).toMatchObject({ kind: "task", task_run_id: "pending:Ab12", agents: ["agt_helm", "agt_vera", "agt_beacon"] });
    expect(decided).toEqual(["dlg_1"]);
  });
});

describe("consent with multirepo", () => {
  it("names exactly the app's repositories, what it cannot reach, who and how", async () => {
    grants();
    const { request } = await open({ ...BUILD_409, multirepo: MULTIREPO });
    expect(request.multirepo?.repos).toHaveLength(4);
    expect(tid("consent-mr-eyebrow")?.textContent).toBe("Before the build starts");
    expect(document.querySelector("h2")?.textContent).toContain("Which repositories may the agents touch?");
    expect(tid("consent-mr-subtitle")?.textContent).toBe("Run R-1482 · Storefront · exactly the app's repositories");
    expect($$('[data-testid^="consent-mr-repo-"]').map((e) => e.dataset.testid)).toEqual([
      "consent-mr-repo-acme/contracts", "consent-mr-repo-acme/api", "consent-mr-repo-acme/web", "consent-mr-repo-acme/infra"]);
    expect(tid("consent-mr-repo-acme/api")?.textContent).toContain("Read · Write on boss/r1482-* · Pull requests");
    expect(tid("consent-mr-repo-acme/api")?.textContent).toContain("Write");
    expect(tid("consent-mr-repo-acme/infra")?.textContent).toContain("Read only · for the deploy plan");
    expect(tid("consent-mr-repo-acme/infra")?.getAttribute("data-access")).toBe("read");
    expect(tid("consent-mr-unreachable")?.textContent).toContain("5 other repositories in acme");
    expect(tid("consent-mr-unreachable")?.textContent).toContain("acme/legacy-admin");
    expect(tid("consent-mr-who")?.textContent).toContain("Helmbuilds");
    expect(tid("consent-mr-who")?.textContent).toContain("Verareviews");
    expect(tid("consent-mr-who")?.textContent).toContain("Beacondeploys");
    expect(tid("consent-mr-how")?.textContent).toContain("GitHub App token limited to these 4 repositories");
    expect(tid("consent-mr-how")?.textContent).toContain("1 hour, renewed per step, ends with the run");
    expect(tid("consent-mr-allow")?.textContent).toBe("Allow 4 repositories");
    expect(document.body.textContent).toContain("Remember for Storefront");
    // The plain sheet's choices are not drawn as well.
    expect(tid("consent-durations")).toBeNull();
  });

  it("Remember ticked is the standing grant", async () => {
    const calls = grants();
    const { decided, closed } = await open({ ...BUILD_409, multirepo: MULTIREPO });
    expect((tid("consent-mr-remember") as HTMLInputElement).checked).toBe(true);
    expect(tid("consent-mr-allow")?.dataset.kind).toBe("standing");
    await click(tid("consent-mr-allow"));
    expect(calls[0]).toMatchObject({ path: "/v1/identity/me/delegations", body: { kind: "standing", days: 30 } });
    expect((calls[0].body as Record<string, unknown>).task_run_id).toBeUndefined();
    expect((calls[0].body as { github: unknown }).github).toEqual({ repos: ["acme/contracts", "acme/api", "acme/web", "acme/infra"], contents: "write", pull_requests: "write" });
    expect(decided).toEqual(["dlg_1"]);
    expect(closed).toEqual([1]);
  });

  it("Remember unticked is the task grant, ending with the run", async () => {
    const calls = grants();
    await open({ ...BUILD_409, multirepo: MULTIREPO });
    await click(tid("consent-mr-remember"));
    expect((tid("consent-mr-remember") as HTMLInputElement).checked).toBe(false);
    expect(tid("consent-mr-allow")?.dataset.kind).toBe("task");
    await click(tid("consent-mr-allow"));
    expect(calls[0].body).toMatchObject({ kind: "task", task_run_id: "pending:Ab12" });
  });

  it("only a standing grant offered: Remember is fixed on", async () => {
    grants();
    await open({ ...BUILD_409, task_run_id: null, multirepo: MULTIREPO });
    const box = tid("consent-mr-remember") as HTMLInputElement;
    expect(box.checked).toBe(true);
    expect(box.disabled).toBe(true);
  });

  it("Cancel decides nothing", async () => {
    const calls = grants();
    const { decided } = await open({ ...BUILD_409, multirepo: MULTIREPO });
    await click(tid("consent-mr-cancel"));
    expect(decided).toEqual([null]);
    expect(calls).toHaveLength(0);
  });
});
