// @vitest-environment happy-dom
/**
 * Multi-repo screen 6, the linked pull requests (app/multirepo/linked-prs.tsx), the change
 * cockpit's "Pull requests" tab (`&tab=prs`):
 *   - the PRs side by side: repo, Ready / N blocking, #n title, diff, CI and gates, "Links the
 *     other 2 · base main ← boss/…", the reviewers' findings with severity, Open on GitHub;
 *   - the cross-repo check with Pass/Fail rows and "4 of 5 pass"; Set blocked · issues and the
 *     note "The whole set waits";
 *   - "Run the check again" POSTs cross-check (code_task:steer) and shows a 409 as said; a viewer
 *     sees it disabled and why.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { needsWords } from "../lib/identity/words";
import type { ChangeSetView } from "../lib/multirepo";
import {
  $$, blockedSetFixture, changeSetFixture, click, inReviewFixture, mountAs, stubFetch, tid, unmount, type Call, type Role,
} from "./_helpers/multirepo-fixtures";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));

const { ChangeSetCockpit } = await import("../app/multirepo/change-set-cockpit");

beforeEach(() => { window.history.replaceState(null, "", "/?area=code_tasks&changeset=rcs_1&tab=prs"); });
afterEach(() => { unmount(); window.history.replaceState(null, "", "/"); });

function api(cs: ChangeSetView, recheck?: (c: Call) => { status?: number; body: unknown }) {
  return stubFetch((c) => {
    if (c.method === "GET" && c.path === "/v1/change-sets/rcs_1") return { body: cs };
    if (c.method === "POST" && c.path === "/v1/change-sets/rcs_1/cross-check") return recheck ? recheck(c) : { body: cs };
    return undefined;
  });
}
const mount = (role: Role = "owner") =>
  mountAs(<ChangeSetCockpit projectId="p1" changeSetId="rcs_1" actor="ceo" onClose={() => {}} />, role);

describe("linked pull requests (screen 6)", () => {
  it("opens on the PRs from &tab=prs and draws each side by side", async () => {
    api(blockedSetFixture());
    await mount();
    expect(tid("mr-csc-tab-prs")?.getAttribute("aria-selected")).toBe("true");
    expect(tid("mr-csc-title")?.textContent).toBe("Add saved carts· 3 linked pull requests, reviewed as one set");
    expect($$(".mr-pr").map((p) => p.dataset.testid)).toEqual(["mr-pr-1", "mr-pr-2", "mr-pr-3"]);
    expect(tid("mr-pr-pill-1")?.textContent).toBe("Ready");
    expect(tid("mr-pr-pill-2")?.textContent).toBe("1 blocking");
    expect(tid("mr-pr-2")?.dataset.blocked).toBe("true");
    expect(tid("mr-pr-pill-3")?.textContent).toBe("Ready");
    const api412 = tid("mr-pr-2")!.textContent!;
    expect(api412).toContain("acme/api");
    expect(api412).toContain("#412, PUT/GET /carts/saved, POST /carts/saved/share; owner check; migration 0042");
    expect(api412).toContain("+412 −58 · 11 files");
    expect(api412).toContain("✓ CI");
    expect(api412).toContain("✓ gates");
    expect(tid("mr-pr-links-2")?.textContent?.trim()).toBe("Links the other 2, base main ← boss/r1482-saved-carts");
    expect(tid("mr-finding-2-0")?.textContent).toContain("Sentinel");
    expect(tid("mr-finding-2-0")?.textContent).toContain("handlers/saved.go:48");
    expect(tid("mr-finding-2-0")?.textContent).toContain("High");
    expect(tid("mr-finding-2-0")?.dataset.blocking).toBe("true");
    expect(tid("mr-finding-2-1")?.textContent).toContain("fixed");
    expect(tid("mr-finding-1-0")?.textContent).toContain("Pass");
    // BOSSAPP-954: a GitHub PR opens in BOSS (its project's change page; GitHub when none).
    expect((tid("mr-pr-open-2") as HTMLAnchorElement).getAttribute("href")).toBe("/changes/github/acme/api/412");
  });

  it("the cross-repo check: rows, k of n pass, set blocked, the whole set waits", async () => {
    api(blockedSetFixture());
    await mount();
    expect(tid("mr-xcheck-count")?.textContent).toBe("4 of 5 pass");
    expect($$('[data-testid^="mr-xcheck-row-"]').map((r) => r.dataset.pass)).toEqual(["yes", "yes", "no", "yes", "yes"]);
    expect(tid("mr-xcheck-row-2")?.textContent).toContain("web calls only what the contract defines");
    expect(tid("mr-xcheck-row-2")?.textContent).toContain("Fail");
    expect(tid("mr-set-status")?.textContent).toBe("Set blocked, 2 issues");
    expect(tid("mr-set-reason")?.textContent).toContain("Cross-repo check failed");
    expect(tid("mr-set-waits")?.textContent).toContain("The whole set waits.");
    expect(document.body.textContent).not.toMatch(/Approve and ship/);
  });

  it("a set in review says it waits for approval; no 'whole set waits' note", async () => {
    api(inReviewFixture());
    await mount();
    expect(tid("mr-set-status")?.textContent).toBe("Waiting for your approval");
    expect(tid("mr-xcheck-count")?.textContent).toBe("5 of 5 pass");
    expect(tid("mr-set-waits")).toBeNull();
  });

  it("before every PR is open: no check yet, and Run the check again shows the 409 as said", async () => {
    const calls = api(changeSetFixture(), () => ({ status: 409, body: { error: "not_ready", message: "Every repository must have its PR open first." } }));
    await mount();
    expect(tid("mr-xcheck-none")?.textContent).toBe("Not run yet: it runs once every PR in the set is open.");
    expect(tid("mr-pr-pill-3")?.textContent).toBe("No PR yet");
    await click(tid("mr-xcheck-rerun"));
    expect(calls.find((c) => c.method === "POST")).toMatchObject({ path: "/v1/change-sets/rcs_1/cross-check", body: { project_id: "p1" } });
    expect(tid("mr-xcheck-error")?.textContent).toBe("Every repository must have its PR open first.");
  });

  it("Run the check again replaces the result with the server's", async () => {
    const after = inReviewFixture();
    api(blockedSetFixture(), () => ({ body: after }));
    await mount();
    await click(tid("mr-xcheck-rerun"));
    expect(tid("mr-xcheck-count")?.textContent).toBe("5 of 5 pass");
  });

  it("a viewer reads the PRs; Run the check again is disabled and says why", async () => {
    const calls = api(blockedSetFixture());
    await mount("viewer");
    expect(tid("mr-pr-2")).not.toBeNull();
    expect(tid("mr-xcheck-rerun")?.getAttribute("aria-disabled")).toBe("true");
    expect(tid("mr-xcheck-rerun")?.title).toBe(needsWords("code_task:steer"));
    await click(tid("mr-xcheck-rerun"));
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);
  });

  it("the tabs switch and keep the URL", async () => {
    api(blockedSetFixture());
    await mount();
    await click(tid("mr-csc-tab-build"));
    expect(new URLSearchParams(window.location.search).get("tab")).toBeNull();
    expect(tid("mr-lanes")).not.toBeNull();
    await click(tid("mr-csc-tab-prs"));
    expect(new URLSearchParams(window.location.search).get("tab")).toBe("prs");
  });
});
