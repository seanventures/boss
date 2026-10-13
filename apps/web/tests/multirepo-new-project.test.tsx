// @vitest-environment happy-dom
/**
 * New project › Where the code lives (multi-repo screen 3, app/workflows/new-project-composer.tsx):
 *   - One repo is the default and sends TODAY'S request unchanged — the same body as on an API
 *     with no multi-repo routes at all (regression);
 *   - Several repos (offered with ≥2 project repositories) asks for suggestions, draws component →
 *     repository rows with their suggestion lines and build order (#1/#2/#3, "deploy"), the lock
 *     note and the footer "3 repositories · 3 linked PRs · you approve once";
 *   - Start with Several repos PUTs the app's components first, then starts exactly as today
 *     plus `repo_layout: {mode: "multi", app_name}`; a refused PUT shows its message and starts nothing;
 *   - fewer than two repositories: a one-line hint to the project's Repositories instead;
 *   - a viewer/operator sees Several repos disabled with the reason (repository:manage).
 */
import { afterEach, describe, expect, it } from "vitest";
import React from "react";

import type { CatalogueEntry, Installed } from "../app/delivery-workflows-ui";
import { NewProjectComposer } from "../app/workflows/new-project-composer";
import { needsWords } from "../lib/identity/words";
import {
  $$, click, mountAs, projectReposFixture, stubFetch, tid, type, unmount, type Call, type Role,
} from "./_helpers/multirepo-fixtures";

afterEach(unmount);

const SCHEMA = {
  type: "object", required: ["directive", "title"],
  properties: { directive: { type: "string", format: "textarea" }, title: { type: "string" },
                repository: { type: "string" }, acceptance_criteria: { type: "array", items: { type: "string" } },
                reference_links: { type: "array", items: { type: "string" } } },
};
const KEY = "standard_product_delivery";
const SPD: Installed = { id: "wf_spd", workflow_key: KEY, name: "Standard Product Delivery", is_catalogue: true,
                         forked_from_version_id: null, namespace: "product", input_schema: SCHEMA };
const CAT: CatalogueEntry = { key: KEY, name: KEY, description: "", work_class: "project.new", step_count: 9, steps: [],
                              gates: ["release"], requires_project: ["repository"], estimated_minutes: 60, input_schema: SCHEMA };
const SUGGESTED = { components: [
  { name: "Contracts", binding_id: "bnd_contracts", repository: "acme/contracts", language: "Protobuf", path: "/", kind: "code", access: "write", depends_on: [], reason: "Suggested: name has “contracts” · Protobuf" },
  { name: "Backend", binding_id: "bnd_api", repository: "acme/api", language: "Go", path: "/", kind: "code", access: "write", depends_on: ["Contracts"], reason: "Suggested: name has “api” · Go" },
  { name: "Frontend", binding_id: "bnd_web", repository: "acme/web", language: "TypeScript", path: "/", kind: "code", access: "write", depends_on: ["Contracts", "Backend"], reason: "Suggested: name has “web” · TypeScript" },
  { name: "Deploy config", binding_id: "bnd_infra", repository: "acme/infra", language: "HCL", path: "/", kind: "deploy_config", access: "read", depends_on: [], reason: "Suggested: Terraform · read only" },
] };
const BRIEF = "Let shoppers save their cart and come back to it";

type Opts = { repos?: "four" | "one" | "none"; put?: (c: Call) => { status?: number; body: unknown } };
function api(o: Opts = {}) {
  const view = projectReposFixture();
  return stubFetch((c) => {
    if (c.method === "POST" && c.path === "/api/delivery-workflows") return { body: { run: { id: "wfr_started" } } };
    if (c.path === "/api/project-agents") return { body: { default_roles: [] } };
    if (c.path === "/api/agent") return { body: { items: [] } };
    if (c.path === "/v1/factory/apps") return { body: { apps: [] } };
    if (c.path === "/v1/projects/p1/repos") {
      if (o.repos === "none") return undefined;
      return { body: o.repos === "one" ? { ...view, repo_mode: "single", items: view.items.slice(2, 3) } : view };
    }
    if (c.method === "POST" && c.path === "/v1/projects/p1/suggest-components") return { body: SUGGESTED };
    if (c.method === "PUT" && c.path.startsWith("/v1/apps/")) return o.put ? o.put(c) : { body: { components: [], deploy_config: [] } };
    const q = new URLSearchParams(c.search);
    if (q.get("view") === "repositories") return { body: { repositories: [{ id: "r1", full_name: "acme/web", default_branch: "main" }],
                                                           suggested: "acme/web", suggested_source: "registry_primary" } };
    if (q.get("view") === "readiness") return { body: { ready: true, missing: [], checked: [
      { key: "repository", label: "Repo", fix: "", area: null, ok: true, detail: "", needed_by: [], first_step_label: "", after_a_person: false }] } };
    return undefined;
  });
}
const mount = (role: Role = "owner") => mountAs(
  <NewProjectComposer projectId="p1" actor="ceo" workflows={[SPD]} catalogue={[CAT]} startWith={KEY} onStarted={() => {}} />, role);
const starts = (calls: Call[]) => calls.filter((c) => c.method === "POST" && c.path === "/api/delivery-workflows");

describe("Where the code lives", () => {
  it("One repo is the default and sends today's request unchanged", async () => {
    const calls = api();
    await mount();
    expect(tid("np-lives")?.textContent).toContain("Repository");  // rc9 copy: one repo says "Repository"
    expect(tid("np-lives")?.textContent).toContain("acme/web");
    expect(tid("np-layout-one")?.getAttribute("aria-checked")).toBe("true");
    await type(tid("dw-new-directive"), BRIEF);
    await click(tid("dw-new-start"));
    const body = starts(calls)[0].body;
    expect(calls.some((c) => c.method === "PUT")).toBe(false);
    expect(calls.some((c) => c.path.endsWith("/suggest-components"))).toBe(false);
    unmount();

    // The same press on an API without multi-repo routes sends the very same body.
    const before = api({ repos: "none" });
    await mount();
    expect(tid("np-layout")).toBeNull();
    expect(tid("np-layout-hint")).toBeNull();
    await type(tid("dw-new-directive"), BRIEF);
    await click(tid("dw-new-start"));
    expect(starts(before)[0].body).toEqual(body);
    expect(body).toEqual({ action: "start", project_id: "p1", actor: "ceo", workflow_key: KEY,
                           input: { title: BRIEF, directive: BRIEF } });
  });

  it("Several repos suggests the mapping, in build order, and says what agents may touch", async () => {
    const calls = api();
    await mount();
    await click(tid("np-layout-several"));
    expect(calls.find((c) => c.path === "/v1/projects/p1/suggest-components")?.body)
      .toEqual({ binding_ids: ["bnd_contracts", "bnd_api", "bnd_web", "bnd_infra"] });
    expect($$('[data-testid^="np-map-row-"]')).toHaveLength(4);
    expect((tid("np-map-name-0") as HTMLInputElement).value).toBe("Contracts");
    expect((tid("np-map-repo-1") as HTMLSelectElement).value).toBe("bnd_api");
    expect(tid("np-map-sugg-2")?.textContent).toBe("Suggested: name has “web” · TypeScript");
    expect(["np-map-ord-0", "np-map-ord-1", "np-map-ord-2", "np-map-ord-3"].map((id) => tid(id)?.textContent))
      .toEqual(["#1", "#2", "#3", "deploy"]);
    expect(tid("np-map-lock")?.textContent).toBe(
      "Agents in this project may touch these 4 repositories only (acme/infra read only). You confirm this before the first build.");
    expect(tid("np-cost-several")?.textContent).toBe("3 repositories · 3 linked PRs · you approve once");
    // Adding a row adds an empty component; removing it takes it away again.
    await click(tid("np-map-add"));
    expect($$('[data-testid^="np-map-row-"]')).toHaveLength(5);
    await click(tid("np-map-remove-4"));
    expect($$('[data-testid^="np-map-row-"]')).toHaveLength(4);
  });

  it("Start with Several repos PUTs the components, then starts with repo_layout", async () => {
    const calls = api();
    await mount();
    await click(tid("np-layout-several"));
    await type(tid("dw-new-directive"), BRIEF);
    await click(tid("dw-new-start"));
    const put = calls.findIndex((c) => c.method === "PUT");
    const start = calls.findIndex((c) => c.method === "POST" && c.path === "/api/delivery-workflows");
    expect(put).toBeGreaterThan(-1);
    expect(start).toBeGreaterThan(put);
    expect(decodeURIComponent(calls[put].path)).toBe(`/v1/apps/${BRIEF}/components`);
    expect(calls[put].body).toEqual({ project_id: "p1", app_name: BRIEF, components: [
      { name: "Contracts", binding_id: "bnd_contracts", kind: "code", access: "write", depends_on: [] },
      { name: "Backend", binding_id: "bnd_api", kind: "code", access: "write", depends_on: ["Contracts"] },
      { name: "Frontend", binding_id: "bnd_web", kind: "code", access: "write", depends_on: ["Contracts", "Backend"] },
      { name: "Deploy config", binding_id: "bnd_infra", kind: "deploy_config", access: "read", depends_on: [] },
    ] });
    expect(calls[start].body).toEqual({ action: "start", project_id: "p1", actor: "ceo", workflow_key: KEY,
      input: { title: BRIEF, directive: BRIEF, repo_layout: { mode: "multi", app_name: BRIEF } } });
  });

  it("a refused PUT says why and starts nothing", async () => {
    const calls = api({ put: () => ({ status: 422, body: { error: "read_only_build_target", message: "acme/infra is read only, so it cannot be built. Make it deploy config." } }) });
    await mount();
    await click(tid("np-layout-several"));
    await type(tid("dw-new-directive"), BRIEF);
    await click(tid("dw-new-start"));
    expect(tid("dw-new-error")?.textContent).toBe("acme/infra is read only, so it cannot be built. Make it deploy config.");
    expect(starts(calls)).toHaveLength(0);
  });

  it("with fewer than two repositories it offers a hint, not the switch", async () => {
    api({ repos: "one" });
    await mount();
    expect(tid("np-layout")).toBeNull();
    expect(tid("np-layout-hint")?.getAttribute("title")).toContain("Several repos needs two or more");
    expect(tid("np-layout-hint")?.querySelector("a")?.getAttribute("href")).toContain("view=repositories");
  });

  it.each(["viewer", "operator"] as const)("a %s sees Several repos disabled and why", async (role) => {
    const calls = api();
    await mount(role);
    const several = tid("np-layout-several")!;
    expect(several.getAttribute("aria-disabled")).toBe("true");
    expect(several.getAttribute("title")).toBe(needsWords("repository:manage"));
    await click(several);
    expect(tid("np-several")).toBeNull();
    expect(calls.some((c) => c.path.endsWith("/suggest-components"))).toBe(false);
  });
});
