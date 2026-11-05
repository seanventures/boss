// @vitest-environment happy-dom
/**
 * Factory › Repositories (multi-repo screen 2, app/multirepo/project-repos.tsx):
 *   - draws the project's repositories with their roles, apps, base and status, the One repo /
 *     Several repos switch, the apps in the project and the two side notes;
 *   - Remove refused while an app uses it shows the API's in_use message, and keeps the row;
 *   - the add picker lists only addable repositories and says "5 more there are not allowed";
 *     picking one PUTs it; a role change PUTs role_hint; the switch PUTs repo_mode (and shows
 *     apps_use_several when refused);
 *   - a viewer/operator sees the controls disabled with the reason; owner/admin act.
 *   - the Factory reads view=repositories and view=components&app= from the URL.
 */
import { afterEach, describe, expect, it } from "vitest";
import React from "react";

import { ProjectRepositories } from "../app/multirepo/project-repos";
import { readFactoryUrl } from "../app/code-tasks-ui";
import { needsWords } from "../lib/identity/words";
import { $$, choose, click, mountAs, projectReposFixture, stubFetch, tid, unmount, type Call, type Role } from "./_helpers/multirepo-fixtures";

afterEach(unmount);

function api(over: (c: Call) => { status?: number; body: unknown } | undefined = () => undefined) {
  const view = projectReposFixture();
  return stubFetch((c) => over(c) ?? (c.method === "GET" && c.path === "/v1/projects/p1/repos" ? { body: view }
    : c.method === "PUT" && c.path.startsWith("/v1/projects/p1/repos/") ? { body: view }
    : c.method === "PUT" && c.path === "/v1/projects/p1/repo-mode" ? { body: { ...view, repo_mode: (c.body as { repo_mode: string }).repo_mode } }
    : undefined));
}
const mount = (role: Role = "owner") => mountAs(<ProjectRepositories projectId="p1" />, role);

describe("Factory › Repositories", () => {
  it("draws the project's repositories as the mockup does", async () => {
    api();
    await mount();
    expect($$('[data-testid^="mr-prj-row-"]')).toHaveLength(4);
    const contracts = tid("mr-prj-row-bnd_contracts")!.textContent!;
    expect(contracts).toContain("acme/contracts");
    expect(contracts).toContain("Protobuf");
    expect(contracts).toContain("Storefront, Checkout");
    expect(contracts).toContain("main");
    expect(contracts).toContain("Working");
    expect((tid("mr-prj-role-bnd_infra") as HTMLSelectElement).value).toBe("deploy_config");
    expect(tid("mr-prj-row-bnd_infra")?.textContent).toContain("Read only");
    expect(tid("mr-prj-mode-multi")?.getAttribute("aria-checked")).toBe("true");
    expect(tid("mr-prj-mode-single")?.getAttribute("aria-checked")).toBe("false");
    expect(tid("mr-prj-app-storefront")?.textContent).toContain("contracts → api → web · deploys with infra");
    expect(tid("mr-prj-app-storefront")?.querySelector("a")?.getAttribute("href")).toContain("view=components");
    expect(document.body.textContent).toContain("Access follows the app.");
    expect(document.body.textContent).toContain("boss/*");
  });

  it("one repo on a GitHub token: the page agrees with itself (Working, used by its app, no 'nothing allowed')", async () => {
    const view = projectReposFixture();
    const one = {
      ...view, repo_mode: "single", registry_in_use: false, addable: [], not_allowed_count: 0,
      items: [{ ...view.items[0], binding_id: "bnd_sandbox", full_name: "paramg/boss-sandbox", is_default: true,
                role_hint: null, state: "working",
                used_by_apps: [{ app_key: "helpdesk", app_name: "HelpDesk", components: [] }] }],
    };
    stubFetch((c) => (c.method === "GET" && c.path === "/v1/projects/p1/repos" ? { body: one } : undefined));
    await mount();
    const row = tid("mr-prj-row-bnd_sandbox")!.textContent!;
    expect(row).toContain("Working");
    expect(row).toContain("HelpDesk");
    expect(tid("mr-prj-role-bnd_sandbox")?.textContent).toBe("Whole app");
    expect(row).not.toContain("No role");
    const note = tid("mr-prj-no-registry")!.textContent!;
    expect(note).not.toContain("No repository is allowed");
    expect(note).toContain("work through its GitHub connection");
  });

  it("Remove while an app uses it shows the in_use message and keeps the row", async () => {
    const calls = api((c) => (c.method === "DELETE"
      ? { status: 409, body: { error: "in_use", message: "Storefront and Checkout use acme/api. Remove it from their components first.", apps: ["storefront", "checkout"] } }
      : undefined));
    await mount();
    await click(tid("mr-prj-remove-bnd_api"));
    expect(calls.find((c) => c.method === "DELETE")?.path).toBe("/v1/projects/p1/repos/bnd_api");
    expect(tid("mr-prj-refusal")?.textContent).toBe("Storefront and Checkout use acme/api. Remove it from their components first.");
    expect(tid("mr-prj-row-bnd_api")).not.toBeNull();
  });

  it("the picker lists only addable repositories and says how many more are not allowed", async () => {
    const calls = api();
    await mount();
    const opts = [...(tid("mr-prj-add") as HTMLSelectElement).options].map((o) => o.value).filter(Boolean);
    expect(opts).toEqual(["ghr_payments"]);
    expect(tid("mr-prj-add-tip")?.textContent).toContain("5 more there are not allowed");
    expect(tid("mr-prj-add-tip")?.querySelector("a")?.getAttribute("href")).toContain("focus=repositories");
    await choose(tid("mr-prj-add"), "ghr_payments");
    expect(calls.find((c) => c.method === "PUT")).toMatchObject({ path: "/v1/projects/p1/repos/ghr_payments", body: {} });
  });

  it("changes a role, and the mode; a refused switch says why", async () => {
    const calls = api((c) => (c.path === "/v1/projects/p1/repo-mode"
      ? { status: 409, body: { error: "apps_use_several", message: "Storefront has 3 components. Map it to one repository first." } } : undefined));
    await mount("admin");
    await choose(tid("mr-prj-role-bnd_web"), "library");
    expect(calls.find((c) => c.method === "PUT")).toMatchObject({ path: "/v1/projects/p1/repos/bnd_web", body: { role_hint: "library" } });
    await click(tid("mr-prj-mode-single"));
    expect(calls.find((c) => c.path === "/v1/projects/p1/repo-mode")?.body).toEqual({ repo_mode: "single" });
    expect(tid("mr-prj-refusal")?.textContent).toContain("Storefront has 3 components");
  });

  it.each(["viewer", "operator"] as const)("a %s reads it all, with the writes disabled and why", async (role) => {
    const calls = api();
    await mount(role);
    const why = needsWords("repository:manage");
    expect($$('[data-testid^="mr-prj-row-"]')).toHaveLength(4);
    expect(tid("mr-prj-remove-bnd_api")?.getAttribute("aria-disabled")).toBe("true");
    expect(tid("mr-prj-remove-bnd_api")?.getAttribute("title")).toBe(why);
    expect(tid("mr-prj-mode-single")?.getAttribute("aria-disabled")).toBe("true");
    expect(tid("mr-prj-add")?.getAttribute("aria-disabled")).toBe("true");
    // The role reads as a chip, not a picker.
    expect(tid("mr-prj-role-bnd_web")?.tagName).toBe("SPAN");
    expect(tid("mr-prj-role-bnd_web")?.textContent).toBe("Frontend");
    await click(tid("mr-prj-remove-bnd_api"));
    await click(tid("mr-prj-mode-single"));
    await choose(tid("mr-prj-add"), "ghr_payments");
    expect(calls.filter((c) => c.method !== "GET")).toHaveLength(0);
  });

  it("warns when the registration looks healthy and the allow-list will refuse the clone", async () => {
    const view = projectReposFixture();
    const warned = {
      ...view,
      items: [{
        ...view.items[0],
        allow_list_warning: "acme/contracts: registration is active and the connection test passed, but the org allow-list says not_allowed. Egress will refuse the clone (repo_not_allowed).",
      }, ...view.items.slice(1)],
    };
    stubFetch((c) => (c.method === "GET" && c.path === "/v1/projects/p1/repos" ? { body: warned } : undefined));
    await mount();
    const note = tid("mr-prj-allow-warn-bnd_contracts");
    expect(note?.getAttribute("role")).toBe("alert");
    expect(note?.textContent).toContain("repo_not_allowed");
    expect(note?.textContent).toContain("not_allowed");
    expect(tid("mr-prj-allow-warn-bnd_api")).toBeNull();
  });

  it("the Factory deep-links Repositories and an app's Components", () => {
    expect(readFactoryUrl("?area=code_tasks&view=repositories")).toMatchObject({ view: "repositories", app: null });
    expect(readFactoryUrl("?area=code_tasks&view=components&app=storefront")).toMatchObject({ view: "components", app: "storefront" });
    // Components without an app is the Apps tab.
    expect(readFactoryUrl("?area=code_tasks&view=components")).toMatchObject({ view: "apps" });
    expect(readFactoryUrl("?area=code_tasks&view=nonsense")).toMatchObject({ view: "floor" });
  });
});
