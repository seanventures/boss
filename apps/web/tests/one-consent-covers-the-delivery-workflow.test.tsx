// @vitest-environment happy-dom
/**
 * ONE CONSENT FOR THE WORK (operator, 2026-11-19: "Why we need approval for starting the
 * workflow"). A new build's consent also covers its delivery workflow, and the sheet says so in
 * one line (engine identity/consent.py DELIVERY_COVERS, sent as `covers`). A sheet without it
 * (a follow-up, a schedule) shows nothing extra.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConsentSheet } from "../components/identity/consent-sheet";
import { consentFromBody } from "../lib/identity/consent";
import { mountAs, stubFetch, unmount } from "./_helpers/multirepo-fixtures";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));
vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));

afterEach(unmount);

const COVERS = "Covers this build and its delivery workflow: tests, review, preview, staging deploy, smoke test and monitoring. Merging to main, production deploys, spending past the budget and permission changes still ask you.";
const BUILD_409 = {
  error: "consent_required", task: "Recipe box", task_run_id: "pending:Ab12",
  agents: [{ id: "agt_forge", name: "Software Engineer", role_key: "software_engineer" },
           { id: "agt_sre", name: "Site Reliability Engineer", role_key: "sre" }],
  permissions: ["code_task:read", "deployment:create"], projects: ["prj_rb"], github: null,
  local: { repo: "local_prj_rb/recipe-box", repos: ["local_prj_rb/recipe-box"], summary: "on this machine" },
  durations: [{ kind: "task", label: "For this build only", days: null }, { kind: "standing", label: "For 30 days", days: 30 }],
  max_days: 90, covers: COVERS,
};
const tid = (id: string) => document.querySelector(`[data-testid='${id}']`);

describe("the start consent says it covers the delivery workflow", () => {
  it("reads `covers` and shows it in one line", async () => {
    stubFetch(() => undefined);
    const req = consentFromBody(BUILD_409)!;
    expect(req.covers).toBe(COVERS);
    await mountAs(<ConsentSheet request={req} onClose={() => undefined} />, "owner");
    expect(tid("consent-covers")?.textContent).toBe(COVERS);
  });

  it("shows nothing extra when the body does not say it", async () => {
    stubFetch(() => undefined);
    const { covers: _drop, ...plain } = BUILD_409;
    void _drop;
    const req = consentFromBody(plain)!;
    expect(req.covers).toBeNull();
    await mountAs(<ConsentSheet request={req} onClose={() => undefined} />, "owner");
    expect(tid("consent-covers")).toBeNull();
  });
});
