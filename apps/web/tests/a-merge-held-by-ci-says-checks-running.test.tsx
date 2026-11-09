// @vitest-environment happy-dom
/**
 * A MERGE CI STILL HOLDS IS NOT OFFERED AS AN APPROVE THE GATE REFUSES (BOSSAPP-946, finding 6).
 *
 * The local-first demo's drawer offered "Merge PR #1" while CI was still running, and the press
 * was refused ("CI is still running on this change, so the merge cannot be approved yet").
 * The engine now sends `held` ({state, label, reason}, ci_gate.approval_hold) on the Needs-you
 * card, the decision card and the change set's merge; every surface draws Approve as
 * "Checks running…", disabled, with the gate's words.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));
vi.mock("../app/client-relative-time", () => ({ ClientRelativeTime: () => React.createElement("time", null, "4 min ago") }));

import { DecisionActs } from "../app/projects/drawers-more";
import type { PxCtx } from "../app/projects/project-page";
import { FX_OVERVIEW } from "../lib/project-experience-fixtures";
import { parseOverview, type NeedsYou } from "../lib/project-experience";
import { parseChangeSet } from "../lib/repo-links";
import type { DecisionFeedItem } from "../lib/decisions";
import { mountAs, stubFetch, tid, unmount } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { DecisionInbox } = await import("../app/decision-inbox-ui");

const HELD = { state: "waiting", label: "Checks running…",
               reason: "CI is still running on this change; the merge can be approved when it passes." };
const URL = "http://boss-local/local_hd/hd/pull/1";

function card(held: unknown): NeedsYou {
  return parseOverview({ ...FX_OVERVIEW, needs_you: [{
    id: "dec_m1", kind: "merge", label: "Merge change #1", detail: "Approve code change", external_url: URL,
    mini: [], action: { label: "Review change", href: "?drawer=needs:dec_m1", decision_id: "dec_m1" },
    ...(held ? { held } : {}) }] }).needs_you[0];
}

const ctx = { projectId: "hd", work: null, overview: { recent_work: [] }, open: vi.fn(),
              refreshWork: vi.fn() } as unknown as PxCtx;

afterEach(() => unmount());

describe("a merge held by CI", () => {
  it("the drawer says Checks running…, disabled, with the reason", async () => {
    const calls = stubFetch(() => undefined);
    await mountAs(<DecisionActs ctx={ctx} n={card(HELD)} />);
    const button = tid("px-drawer-needs-approve") as HTMLButtonElement;
    expect(button.textContent).toContain("Checks running…");
    expect(button.disabled).toBe(true);
    expect(button.dataset.held).toBe("waiting");
    expect(tid("px-drawer-needs-held")?.textContent).toContain("CI is still running");
    button.click();
    expect(calls.filter((c) => c.method === "POST")).toEqual([]);
  });

  it("once CI clears, the drawer offers the merge again", async () => {
    stubFetch(() => undefined);
    await mountAs(<DecisionActs ctx={ctx} n={card(null)} />);
    const button = tid("px-drawer-needs-approve") as HTMLButtonElement;
    expect(button.disabled).toBe(false);
    expect(button.textContent).not.toContain("Checks running");
    expect(tid("px-drawer-needs-held")).toBeNull();
  });

  it("Approvals draws the same held button", async () => {
    stubFetch(() => undefined);
    const item = {
      id: "dec_m1", question: "Approve code change (change #1)", status: "pending", riskTier: 2,
      recommendation: { action: "review_pull_request", pull_request_url: URL }, options: [], costSummary: {},
      createdAt: "2026-11-09T17:00:00Z", updatedAt: "2026-11-09T17:00:00Z", kind: "pull_request",
      attention: "needs_action",
      allowedActions: [{ action: "approve", label: "Approve", tone: "approve", effect: "", needs: null },
                       { action: "reject", label: "Reject", tone: "reject", effect: "", needs: null }],
      held: HELD,
    } as unknown as DecisionFeedItem;
    await mountAs(<DecisionInbox feed={{ projectId: "hd", apiReachable: true, items: [item] }}
                                 tasks={[]} roles={[]} projectId="hd" actorId="ceo" orgId="org_1" />);
    const approve = tid("decision-approve") as HTMLButtonElement;
    expect(approve.textContent).toContain("Checks running…");
    expect(approve.disabled).toBe(true);
    expect(tid("decision-held")?.textContent).toContain("CI is still running");
  });

  it("the change set keeps the engine's hold on its merge", () => {
    const cs = parseChangeSet({ number: 1, merge: { how: "approve", decision_id: "dec_m1", held: HELD } });
    expect(cs.merge?.held?.label).toBe("Checks running…");
    expect(parseChangeSet({ number: 1, merge: { how: "approve", decision_id: "dec_m1" } }).merge?.held).toBeNull();
  });
});
