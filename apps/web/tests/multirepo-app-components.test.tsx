// @vitest-environment happy-dom
/**
 * Factory › Apps › app › Components (multi-repo screen 4, app/multirepo/app-components.tsx):
 *   - draws the components left to right in build order (contracts → backend → frontend) with
 *     repository, language, path, live ("—" when unknown), ships to and needs; the deploy config
 *     row (read only); Recent changes (empty state now, one row per change set when there are);
 *     the side note "Runs on Storefront may touch …";
 *   - Edit components PUTs the whole list and shows a refusal's message (a cycle) as is;
 *   - a viewer/operator reads it all with Edit disabled and why; owner/admin edit;
 *   - the app row's menu opens it.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import React from "react";

import { AppComponentsView } from "../app/multirepo/app-components";
import { needsWords } from "../lib/identity/words";
import type { AppComponents } from "../lib/multirepo";
import {
  $$, click, componentsFixture, mountAs, projectReposFixture, stubFetch, tid, type, unmount, type Call, type Role,
} from "./_helpers/multirepo-fixtures";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));

afterEach(unmount);

function api(data: AppComponents = componentsFixture(), put?: (c: Call) => { status?: number; body: unknown }) {
  return stubFetch((c) => {
    if (c.method === "GET" && c.path === "/v1/apps/storefront/components") return { body: data };
    if (c.method === "GET" && c.path === "/v1/projects/p1/repos") return { body: projectReposFixture() };
    if (c.method === "PUT" && c.path === "/v1/apps/storefront/components") return put ? put(c) : { body: data };
    return undefined;
  });
}
const mount = (role: Role = "owner") =>
  mountAs(<AppComponentsView projectId="p1" appKey="storefront" onBack={() => {}} />, role);

describe("App › Components", () => {
  it("draws the components in build order with what each needs", async () => {
    const calls = api();
    await mount();
    expect(calls[0]).toMatchObject({ path: "/v1/apps/storefront/components", search: "?project_id=p1" });
    expect($$('[data-testid^="mr-cmp-card-"]').map((e) => e.dataset.testid))
      .toEqual(["mr-cmp-card-contracts", "mr-cmp-card-backend", "mr-cmp-card-frontend"]);
    expect(tid("mr-cmp-lead")?.textContent).toBe("Storefront is built from 3 repositories. Builds go left to right; deploys too.");
    const front = tid("mr-cmp-card-frontend")!.textContent!;
    expect(front).toContain("acme/web");
    expect(front).toContain("TypeScript");
    expect(front).toContain("/apps/storefront");
    expect(front).toContain("shop.acme.com");
    expect(front).toContain("Needscontracts, backend");
    expect(front).toContain("Live—");
    expect(tid("mr-cmp-card-contracts")?.textContent).toContain("npm @acme/contracts");
    expect(tid("mr-cmp-card-contracts")?.textContent).toContain("Needs—");
    expect(tid("mr-cmp-deploy-deploy")?.textContent).toContain("acme/infra · /envs/storefront");
    expect(tid("mr-cmp-deploy-deploy")?.textContent).toContain("Read only");
    expect(tid("mr-cmp-no-changes")).not.toBeNull();
    expect(tid("mr-cmp-scope")?.textContent).toBe(
      "Runs on Storefront may touch acme/contracts, acme/api and acme/web, and read acme/infra. Nothing else in the organization.");
  });

  it("lists recent changes one row per change set", async () => {
    api({ ...componentsFixture(), change_sets: [
      { id: "cs_1", title: "Add saved carts", state: "building", repos: ["contracts", "api", "web"], prs: 3, created_at: new Date().toISOString() },
      { id: "cs_2", title: "Fix price rounding", state: "live", repos: ["api"], prs: 1, created_at: "2026-10-08T10:00:00Z" },
    ] });
    await mount();
    expect($$('[data-testid^="mr-cmp-change-"]')).toHaveLength(2);
    expect(tid("mr-cmp-change-cs_1")?.textContent).toContain("Add saved carts");
    expect(tid("mr-cmp-change-cs_1")?.textContent).toContain("3 linked PRs");
    expect(tid("mr-cmp-change-cs_1")?.textContent).toContain("Building");
    expect(tid("mr-cmp-change-cs_2")?.textContent).toContain("Live");
    expect(tid("mr-cmp-no-changes")).toBeNull();
  });

  it("Edit components PUTs the whole list", async () => {
    const calls = api();
    await mount();
    await click(tid("mr-cmp-edit"));
    expect($$('[data-testid^="mr-cmp-row-"]')).toHaveLength(4);
    // Code first in build order, then the deploy config.
    expect((tid("mr-cmp-name-0") as HTMLInputElement).value).toBe("contracts");
    expect(tid("mr-cmp-ord-0")?.textContent).toBe("#1");
    expect(tid("mr-cmp-ord-2")?.textContent).toBe("#3");
    expect(tid("mr-cmp-ord-3")?.textContent).toBe("deploy");
    await type(tid("mr-cmp-path-1"), "/svc");
    await click(tid("mr-cmp-save"));
    const put = calls.find((c) => c.method === "PUT");
    expect(put?.body).toEqual({ project_id: "p1", app_name: "Storefront", components: [
      { name: "contracts", binding_id: "bnd_contracts", kind: "code", access: "write", depends_on: [], ship_target: { kind: "package", label: "npm", package: "@acme/contracts" } },
      { name: "backend", binding_id: "bnd_api", path: "/svc", kind: "code", access: "write", depends_on: ["contracts"], ship_target: { kind: "host", label: "api.acme.shop" } },
      { name: "frontend", binding_id: "bnd_web", path: "/apps/storefront", kind: "code", access: "write", depends_on: ["contracts", "backend"], ship_target: { kind: "host", label: "shop.acme.com" } },
      { name: "deploy", binding_id: "bnd_infra", path: "/envs/storefront", kind: "deploy_config", access: "read", depends_on: [], ship_target: { kind: "none" } },
    ] });
    expect(tid("mr-cmp-editor")).toBeNull();
  });

  it("a refused save shows the API's message and keeps editing", async () => {
    api(componentsFixture(), () => ({ status: 422, body: { error: "cycle", message: "contracts and backend need each other. Remove one of the two." } }));
    await mount("admin");
    await click(tid("mr-cmp-edit"));
    // contracts now needs backend: a cycle.
    await click(tid("mr-cmp-needs-0")!.querySelector("button"));
    await click(tid("mr-cmp-save"));
    expect(tid("mr-cmp-save-error")?.textContent).toBe("contracts and backend need each other. Remove one of the two.");
    expect(tid("mr-cmp-editor")).not.toBeNull();
  });

  it.each(["viewer", "operator"] as const)("a %s reads it with Edit disabled and why", async (role) => {
    const calls = api();
    await mount(role);
    expect($$('[data-testid^="mr-cmp-card-"]')).toHaveLength(3);
    expect(tid("mr-cmp-edit")?.getAttribute("aria-disabled")).toBe("true");
    expect(tid("mr-cmp-edit")?.getAttribute("title")).toBe(needsWords("repository:manage"));
    await click(tid("mr-cmp-edit"));
    expect(tid("mr-cmp-editor")).toBeNull();
    expect(calls.filter((c) => c.method !== "GET")).toHaveLength(0);
  });

  it("an app with no components says so and offers Add component", async () => {
    api({ ...componentsFixture(), components: [], deploy_config: [], repo_mode: "single" });
    await mount();
    expect(tid("mr-cmp-none")).not.toBeNull();
    expect(tid("mr-cmp-edit")?.textContent).toContain("Add component");
  });
});

describe("the app row opens its Components", () => {
  it("Apps › ⋯ › Components", async () => {
    stubFetch((c) => (c.path === "/v1/factory/apps" ? { body: { apps: [
      { key: "crun_1", app: "Storefront", app_key: "storefront", run_id: "crun_1", word: "PR open", attempts: [{ run_id: "crun_1", attempt: 1, word: "PR open" }] },
    ] } } : undefined));
    const { AppsTab } = await import("../app/factory/apps-tab");
    const opened: string[] = [];
    await mountAs(<AppsTab projectId="p1" runs={[]} previews={[]} onOpen={() => {}} onNew={() => {}} onComponents={(k) => opened.push(k)} />, "viewer");
    await click(tid("fx-app-more-crun_1"));
    await click(tid("fx-app-menu-components-crun_1"));
    expect(opened).toEqual(["storefront"]);
  });
});
