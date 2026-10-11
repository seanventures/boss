// @vitest-environment happy-dom
/**
 * Repo and models › Repositories (multi-repo screen 1, app/multirepo/registry.tsx), against the
 * API's real shapes at the fetch boundary:
 *   - draws the mockup: GitHub App · acme, nine repositories, Read · Write · PRs checks, Working /
 *     Read only / Not allowed / Archived, the read-only note, "Save · 4 allowed";
 *   - filters All 9 / Allowed 4 / Needs a fix 1 / Not allowed 5, and Find;
 *   - an archived repository cannot be ticked;
 *   - Save sends exactly the ticked ids (PUT /v1/github/repos/allowed) and draws the answer;
 *   - a viewer or operator sees every control disabled with the reason; owner and admin act.
 */
import { afterEach, describe, expect, it } from "vitest";
import React from "react";

import { RepositoryRegistry } from "../app/multirepo/registry";
import { needsWords } from "../lib/identity/words";
import type { Registry } from "../lib/multirepo";
import { $$, click, mountAs, registryFixture, stubFetch, tid, type, unmount, type Call } from "./_helpers/multirepo-fixtures";

afterEach(unmount);

function api(reg: Registry = registryFixture()) {
  return stubFetch((c: Call) => {
    if (c.method === "GET" && c.path === "/v1/github/repos") return { body: reg };
    if (c.method === "PUT" && c.path === "/v1/github/repos/allowed") {
      const ids = (c.body as { allowed_ids: string[] }).allowed_ids;
      return { body: { ...reg, items: reg.items.map((r) => ({ ...r, allowed: ids.includes(r.id), state: ids.includes(r.id) ? (r.state === "not_allowed" ? "checking" : r.state) : r.archived ? "archived" : "not_allowed" })) } };
    }
    if (c.method === "POST" && c.path === "/v1/github/repos/recheck") return { body: reg };
    return undefined;
  });
}
const rows = () => $$('[data-testid^="mr-reg-row-"]');
const mount = (role: Parameters<typeof mountAs>[1] = "owner") => mountAs(<RepositoryRegistry projectId="p1" />, role);

describe("Repo and models › Repositories", () => {
  it("draws the registry as the mockup does", async () => {
    const calls = api();
    await mount();
    expect(calls[0]).toMatchObject({ method: "GET", path: "/v1/github/repos", search: "?project_id=p1" });
    expect(tid("mr-reg-title")?.textContent).toBe("GitHub App · acme");
    expect(tid("mr-reg-sub")?.textContent).toContain("reaches 9 repositories");
    expect(rows()).toHaveLength(9);
    expect(tid("mr-reg-row-ghr_web")?.textContent).toContain("acme/web");
    expect(tid("mr-reg-row-ghr_web")?.textContent).toContain("Storefront UI");
    expect(tid("mr-reg-row-ghr_web")?.textContent).toContain("TypeScript");
    expect(tid("mr-check-ghr_web-read")?.textContent).toBe("✓ Read");
    expect(tid("mr-check-ghr_infra-write")?.textContent).toBe("✗ Write");
    expect(tid("mr-check-ghr_mobile-pull_requests")?.textContent).toBe("– PRs");
    expect(tid("mr-reg-state-ghr_web")?.textContent).toBe("Working");
    expect(tid("mr-reg-state-ghr_infra")?.textContent).toBe("Read only");
    expect(tid("mr-reg-state-ghr_mobile")?.textContent).toBe("Not allowed");
    expect(tid("mr-reg-state-ghr_legacy")?.textContent).toBe("Archived");
    expect(tid("mr-reg-row-ghr_contracts")?.textContent).toContain("Acme Shop, Payments");
    expect(tid("mr-reg-readonly-ghr_infra")?.textContent).toContain("acme/infra is read only.");
    expect(tid("mr-reg-save")?.textContent).toBe("Save · 4 allowed");
    expect(tid("mr-reg-change")?.getAttribute("href")).toContain("github.com");
    // In use and listed: no Refresh from GitHub.
    expect(tid("mr-reg-refresh")).toBeNull();
  });

  it("filters All / Allowed / Needs a fix / Not allowed, and finds by name", async () => {
    api();
    await mount();
    expect(tid("mr-reg-filter-all")?.textContent).toBe("All 9");
    expect(tid("mr-reg-filter-allowed")?.textContent).toBe("Allowed 4");
    expect(tid("mr-reg-filter-needs_fix")?.textContent).toBe("Needs a fix 1");
    expect(tid("mr-reg-filter-not_allowed")?.textContent).toBe("Not allowed 5");
    await click(tid("mr-reg-filter-allowed"));
    expect(rows().map((r) => r.dataset.testid)).toEqual(["mr-reg-row-ghr_web", "mr-reg-row-ghr_api", "mr-reg-row-ghr_contracts", "mr-reg-row-ghr_infra"]);
    await click(tid("mr-reg-filter-needs_fix"));
    expect(rows().map((r) => r.dataset.testid)).toEqual(["mr-reg-row-ghr_infra"]);
    await click(tid("mr-reg-filter-not_allowed"));
    expect(rows()).toHaveLength(5);
    await click(tid("mr-reg-filter-all"));
    await type(tid("mr-reg-find"), "pay");
    expect(rows().map((r) => r.dataset.testid)).toEqual(["mr-reg-row-ghr_payments"]);
  });

  it("an archived repository cannot be ticked", async () => {
    api();
    await mount();
    const tick = tid("mr-reg-tick-ghr_legacy")!;
    expect(tick.getAttribute("aria-disabled")).toBe("true");
    expect(tick.getAttribute("title")).toMatch(/Archived/);
    await click(tick);
    expect(tick.getAttribute("aria-checked")).toBe("false");
    expect((tid("mr-reg-save") as HTMLButtonElement).disabled).toBe(true);
  });

  it("Save sends exactly the ticked ids, and draws the answer", async () => {
    const calls = api();
    await mount();
    expect((tid("mr-reg-save") as HTMLButtonElement).disabled).toBe(true);
    await click(tid("mr-reg-tick-ghr_mobile"));
    await click(tid("mr-reg-tick-ghr_api"));
    expect(tid("mr-reg-row-ghr_mobile")?.hasAttribute("data-changed")).toBe(true);
    expect(tid("mr-reg-save")?.textContent).toBe("Save · 4 allowed");
    await click(tid("mr-reg-save"));
    const put = calls.find((c) => c.method === "PUT");
    expect(put).toMatchObject({ path: "/v1/github/repos/allowed",
                                body: { project_id: "p1", allowed_ids: ["ghr_web", "ghr_contracts", "ghr_infra", "ghr_mobile"] } });
    expect(tid("mr-reg-state-ghr_api")?.textContent).toBe("Not allowed");
    expect(tid("mr-reg-state-ghr_mobile")?.textContent).toBe("Checking");
    expect((tid("mr-reg-save") as HTMLButtonElement).disabled).toBe(true);
  });

  it("offers Refresh from GitHub when nothing is listed yet", async () => {
    const calls = api({ ...registryFixture(), items: [], in_use: false, counts: { all: 0, allowed: 0, needs_fix: 0, not_allowed: 0 } });
    await mount("admin");
    expect(tid("mr-reg-empty")).not.toBeNull();
    stubFetch((c) => (c.path === "/v1/github/repos/refresh" ? { body: registryFixture() } : undefined));
    await click(tid("mr-reg-refresh"));
    expect(rows()).toHaveLength(9);
    expect(calls).toBeDefined();
  });

  it.each(["viewer", "operator"] as const)("a %s sees the table with every write disabled and why", async (role) => {
    const calls = api();
    await mount(role);
    expect(rows()).toHaveLength(9);
    const why = needsWords("repository:register");
    const tick = tid("mr-reg-tick-ghr_mobile")!;
    expect(tick.getAttribute("aria-disabled")).toBe("true");
    expect(tick.getAttribute("title")).toBe(why);
    await click(tick);
    expect(tick.getAttribute("aria-checked")).toBe("false");
    for (const id of ["mr-reg-save", "mr-reg-recheck", "mr-reg-check-ghr_web"]) {
      expect(tid(id)?.getAttribute("aria-disabled"), id).toBe("true");
      expect(tid(id)?.getAttribute("title"), id).toBe(why);
    }
    await click(tid("mr-reg-recheck"));
    expect(calls.filter((c) => c.method !== "GET")).toHaveLength(0);
  });

  it.each(["owner", "admin"] as const)("an %s can recheck", async (role) => {
    const calls = api();
    await mount(role);
    expect(tid("mr-reg-recheck")?.getAttribute("aria-disabled")).toBeNull();
    await click(tid("mr-reg-recheck"));
    expect(calls.find((c) => c.method === "POST")).toMatchObject({ path: "/v1/github/repos/recheck", body: { project_id: "p1" } });
  });

  it("hides itself on an API without the registry", async () => {
    stubFetch(() => undefined);
    const host = await mount();
    expect(host.querySelector('[data-testid="mr-registry"]')).toBeNull();
  });
});
