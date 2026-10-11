// @vitest-environment happy-dom
/**
 * Multi-repo screen 7, one approval for a change (app/multirepo/change-set-approval.tsx inside
 * the Approvals row for `approve_change_set`):
 *   - the mockup's card: eyebrow, title, the one-decision sentence, four tiles (PRs, review,
 *     cross-repo, cost), the table (repository, PR, change, tests, deploys as), the rollback note
 *     with "See the deploy plan", the three answers and "No per-PR approval";
 *   - Approve sends the preview's content_hash through the inbox's form; a stale approval's
 *     refusal is shown as the server said it;
 *   - Send back needs a note; a viewer sees every answer disabled and why; a blocked set offers
 *     nothing to approve.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { needsWords } from "../lib/identity/words";
import type { DecisionFeedItem } from "../lib/decisions";
import type { ChangeSetView } from "../lib/multirepo";
import {
  $, blockedSetFixture, changeSetDecision, click, deployPlanFixture, inReviewFixture, mountAs, stubFetch, tid, type, unmount, type Role,
} from "./_helpers/multirepo-fixtures";

const resolve = vi.fn();
vi.mock("../app/actions", async (original) => ({
  ...(await original<typeof import("../app/actions")>()),
  resolveDecisionAction: (fd: FormData) => resolve(fd),
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));
vi.mock("../app/client-relative-time", () => ({ ClientRelativeTime: () => React.createElement("time", null, "4 min ago") }));

const { DecisionInbox } = await import("../app/decision-inbox-ui");

beforeEach(() => { resolve.mockReset(); });
afterEach(unmount);

function api(cs: ChangeSetView = inReviewFixture()) {
  return stubFetch((c) => {
    if (c.method === "GET" && c.path === "/v1/change-sets/rcs_1") return { body: cs };
    if (c.method === "GET" && c.path === "/v1/change-sets/rcs_1/deploy-plan") return { body: deployPlanFixture() };
    return undefined;
  });
}
function mount(role: Role = "owner", decision: Record<string, unknown> = changeSetDecision()) {
  return mountAs(
    <DecisionInbox feed={{ projectId: "p1", apiReachable: true, items: [decision as unknown as DecisionFeedItem] }}
                   tasks={[]} roles={[]} projectId="p1" actorId="ceo" orgId="org_1" />, role);
}
const formOf = () => Object.fromEntries((resolve.mock.calls[0][0] as FormData).entries());

describe("one approval for a change (screen 7)", () => {
  it("draws the mockup's card from the decision and the change", async () => {
    const calls = api();
    await mount();
    expect(calls.map((c) => c.path)).toEqual(expect.arrayContaining(["/v1/change-sets/rcs_1", "/v1/change-sets/rcs_1/deploy-plan"]));
    expect(tid("decision-row")?.dataset.decisionStatus).toBe("pending");
    expect(tid("mr-approval-eyebrow")?.textContent).toBe("Ship a change · Storefront");
    expect($('[data-testid="mr-approval"] h3')?.textContent).toBe("Add saved carts");
    expect(tid("mr-approval-lead")?.textContent).toBe(
      "One decision for all 3 pull requests. They merge together and deploy together, or not at all.");
    const stats = tid("mr-approval-stats")!.textContent!;
    expect(stats).toContain("3 linked");
    expect(stats).toContain("contracts #88 · api #412 · web #977");
    expect(tid("mr-approval-review")?.textContent).toContain("0 open");
    expect(tid("mr-approval-review")?.textContent).toContain("3 fixed · adversarial review on each PR");
    expect(tid("mr-approval-xcheck")?.textContent).toContain("5 / 5");
    expect(tid("mr-approval-cost")?.textContent).toContain("$11.80");
    const api412 = tid("mr-approval-row-acme/api")!.textContent!;
    expect(api412).toContain("#412");
    expect(api412).toContain("+431 −58");
    expect(api412).toContain("✓ passed");
    expect(api412).toContain("v2.32.0 → eu-west-1");
    expect(tid("mr-approval-row-acme/contracts")?.textContent).toContain("1.4.0 · with the merge");
    expect(tid("mr-approval-rollback")?.textContent).toContain(
      "BOSS rolls every component back to what is live now: contracts 1.3.2, backend v2.31.0, frontend 2026.10.08-3.");
    expect((tid("mr-approval-plan-link") as HTMLAnchorElement).getAttribute("href")).toContain("area=infra");
    expect((tid("mr-approval-plan-link") as HTMLAnchorElement).getAttribute("href")).toContain("changeset=rcs_1");
    expect(tid("mr-approval-approve")?.textContent?.trim()).toBe("Approve and ship all 3");
    expect(tid("mr-approval-send-back")?.textContent).toBe("Send back");
    expect(tid("mr-approval-reject")?.textContent).toBe("Reject");
    expect(tid("mr-approval-no-partial")?.textContent).toBe("No per-PR approval: approving part of a change is not possible.");
    expect(tid("decision-approve")).toBeNull();
  });

  it("Approve sends the reviewed content hash through the inbox's form", async () => {
    api();
    resolve.mockResolvedValue({ ok: true, message: "Approved." });
    await mount();
    await click(tid("mr-approval-approve"));
    expect(resolve).toHaveBeenCalledTimes(1);
    expect(formOf()).toMatchObject({ decision_id: "dec_cs", action: "approve", project_id: "p1", actor: "ceo", content_hash: "hash_heads_v1" });
  });

  it("a stale approval shows the server's refusal", async () => {
    api();
    resolve.mockResolvedValue({ ok: false, error: "New commits arrived after this was asked. BOSS asks again." });
    await mount();
    await click(tid("mr-approval-approve"));
    expect(tid("action-form-error")?.textContent).toBe("New commits arrived after this was asked. BOSS asks again.");
  });

  it("Send back needs a note, then sends it with revise", async () => {
    api();
    resolve.mockResolvedValue({ ok: true, message: "Sent back with your note." });
    await mount();
    await click(tid("mr-approval-send-back"));
    const send = tid("mr-approval-revise") as HTMLButtonElement;
    expect(send.disabled).toBe(true);
    await click(send);
    expect(resolve).not.toHaveBeenCalled();
    await type(tid("mr-approval-note"), "Use the shared cart id type in web too.");
    expect((tid("mr-approval-revise") as HTMLButtonElement).disabled).toBe(false);
    await click(tid("mr-approval-revise"));
    expect(formOf()).toMatchObject({ action: "revise", note: "Use the shared cart id type in web too." });
  });

  it("Reject sends reject", async () => {
    api();
    resolve.mockResolvedValue({ ok: true });
    await mount();
    await click(tid("mr-approval-reject"));
    expect(formOf()).toMatchObject({ action: "reject", decision_id: "dec_cs" });
  });

  it("a viewer sees the card with every answer disabled and why", async () => {
    api();
    await mount("viewer");
    expect(tid("mr-approval")).not.toBeNull();
    for (const id of ["mr-approval-approve", "mr-approval-reject"]) {
      expect(tid(id)?.getAttribute("aria-disabled")).toBe("true");
      expect(tid(id)?.title).toContain(needsWords("decision:approve"));
    }
    await click(tid("mr-approval-approve"));
    expect(resolve).not.toHaveBeenCalled();
  });

  it("an operator may approve", async () => {
    api();
    await mount("operator");
    expect(tid("mr-approval-approve")?.getAttribute("aria-disabled")).toBeNull();
  });

  it("a blocked set shows the reason and no approval affordance", async () => {
    api(blockedSetFixture());
    await mount();
    expect(tid("mr-approval-blocked")?.textContent).toContain("Cross-repo check failed");
    expect(tid("mr-approval-approve")).toBeNull();
    expect(tid("mr-approval-send-back")).toBeNull();
  });

  it("a decided change shows its verdict, not the buttons", async () => {
    api();
    await mount("owner", changeSetDecision({ status: "approved", approvedBy: "Param", allowedActions: [] }));
    await click(tid("decision-filter-resolved"));
    expect(tid("mr-approval-verdict")?.textContent).toContain("approved");
    expect(tid("mr-approval-verdict")?.textContent).toContain("Param");
    expect(tid("mr-approval-approve")).toBeNull();
  });
});
