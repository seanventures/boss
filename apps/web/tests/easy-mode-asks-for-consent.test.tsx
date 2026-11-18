// @vitest-environment happy-dom
/**
 * EASY MODE ASKS FOR CONSENT THE CONSOLE'S ONE WAY, THEN BUILDS IN THE SAME PROJECT.
 *
 * Release validation 2026-11-17: Command Center › a template › Easy Mode answered
 * 409 consent_required and the bench printed the raw "consent_required"; no sheet. Now:
 *   - the 409 opens the standard consent sheet (components/identity/consent-sheet.tsx);
 *   - the grant is created and the SAME launch is sent again (the API kept the project);
 *   - closing the sheet says, in words, that the project is made and waits, idle;
 *   - no machine code is ever shown on the bench.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BuildHome } from "../app/build-home";
import { ConsentHost } from "../components/identity/consent-sheet";
import { codeOnly } from "./_helpers/source";
import { mountAs, settle, stubFetch, tid, unmount, type Call } from "./_helpers/multirepo-fixtures";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));
vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));

afterEach(unmount);

/** As /v1/factory/launch answers it (identity/consent.py + factory_launch._keep_for_consent). */
const LAUNCH_409 = {
  error: "consent_required", task: "CSV quality desk", task_run_id: "pending:Q1w2e3",
  agents: [{ id: "agt_forge01", name: "Software Engineer", role_key: "software_engineer" }],
  permissions: ["code_task:read", "repository:read"], projects: ["prj_csv"],
  github: null, local: { repo: "_local/lucky", repos: ["_local/lucky"], summary: "on this machine" },
  durations: [{ kind: "task", label: "For this build only", days: null }, { kind: "standing", label: "For 30 days", days: 30 }],
  max_days: 90, project_id: "prj_csv", dedupe_key: "factory.launch:abc", created: true,
  retry: { project_id: "prj_csv", dedupe_key: "factory.launch:abc" },
};

function server(launches: Array<{ status: number; body: unknown }>) {
  return stubFetch((c: Call) => {
    if (c.path === "/api/factory/templates") return { body: { templates: [] } };
    if (c.path === "/api/factory/launch") return launches.shift() ?? { status: 500, body: { error: "too many launches" } };
    if (c.method === "POST" && c.path === "/v1/identity/me/delegations") return { status: 201, body: { id: "dlg_1" } };
    return undefined;
  });
}

async function press(id: string) {
  const el = tid(id);
  expect(el, id).not.toBeNull();
  await React.act(async () => { el!.click(); });
  await settle();
  await settle();
}

describe("Easy Mode under consent", () => {
  it("opens the consent sheet, creates the grant, and sends the same launch again", async () => {
    const calls = server([
      { status: 409, body: LAUNCH_409 },
      { status: 201, body: { project_id: "prj_csv", run_id: "run_ct_1" } },
    ]);
    await mountAs(<><BuildHome /><ConsentHost /></>, "owner");
    await press("build-lucky");
    // The standard sheet, naming the agent; never the raw code.
    expect(tid("consent-durations")).not.toBeNull();
    expect(document.body.textContent).toContain("Software Engineer");
    expect(document.body.textContent).not.toContain("consent_required");
    await press("consent-task");
    const grant = calls.find((c) => c.method === "POST" && c.path === "/v1/identity/me/delegations");
    expect(grant?.body).toMatchObject({ kind: "task", task_run_id: "pending:Q1w2e3", agents: ["agt_forge01"], projects: ["prj_csv"] });
    const launches = calls.filter((c) => c.path === "/api/factory/launch");
    expect(launches).toHaveLength(2);
    expect(launches[1].body).toEqual(launches[0].body);           // the same launch, unchanged
    expect(tid("build-project-id")?.textContent).toBe("prj_csv");  // the same project
    expect(tid("build-run-id")?.textContent).toBe("run_ct_1");
    expect(tid("build-status")?.textContent).toContain("Build started");
    expect(tid("build-launch-error")).toBeNull();
  });

  it("a closed sheet says the project waits, idle, in words, and sends nothing more", async () => {
    const calls = server([{ status: 409, body: LAUNCH_409 }]);
    await mountAs(<><BuildHome /><ConsentHost /></>, "owner");
    await press("build-lucky");
    expect(tid("consent-sheet")).not.toBeNull();
    // The sheet's own close (Escape), as a person dismisses it.
    await React.act(async () => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });
    await settle();
    await settle();
    expect(calls.filter((c) => c.path === "/api/factory/launch")).toHaveLength(1);
    expect(calls.some((c) => c.path === "/v1/identity/me/delegations")).toBe(false);
    const said = tid("build-launch-error")?.textContent ?? "";
    expect(said).toMatch(/consent/i);
    expect(said).toMatch(/idle/);
    expect(document.body.textContent).not.toContain("consent_required");
    expect(tid("build-launch")).toBeNull();
  });

  it("never prints a machine code: a bare code becomes a sentence", async () => {
    server([{ status: 409, body: { error: "budget_refused" } }]);
    await mountAs(<BuildHome />, "owner");
    await press("build-lucky");
    const said = tid("build-launch-error")?.textContent ?? "";
    expect(said).not.toMatch(/budget_refused/);
    expect(said).toMatch(/\s/);
  });

  it("the launch proxy passes a consent request through untouched", () => {
    const route = codeOnly("app/api/factory/launch/route.ts");
    expect(route).toMatch(/consent_required/);
    expect(route.indexOf("consent_required")).toBeLessThan(route.indexOf("describeApiError(data"));
  });
});
