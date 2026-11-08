/**
 * The Approvals inbox and the Command Center agree on what needs the operator.
 *
 * Review round 2: the inbox's "Needs action" became "any card the engine offers an answer on",
 * which swept every expired hold-default decision into the default view. On the live project
 * that is 127 of 165 decisions, some offering "Approve and merge" on pull requests from weeks
 * ago, while the Command Center still counted pending and delegated. Both now read the engine's
 * `attention` (decision_actions.attention); expired hold decisions have their own group.
 */
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { DecisionFeedItem } from "../lib/decisions";
import {
  decisionAttention,
  filterDecisionInbox,
  isAnswerableDecision,
  offeredActions,
  parseDecisionInboxFilter,
} from "../lib/decisions";
import { commandCenterFrom, decisionsHref } from "../lib/command-center";
import { parseCommandCenterView } from "../lib/command-center-view";
import { view } from "./_helpers/command-center-view-fixture";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
}));

vi.mock("../app/client-relative-time", () => ({
  ClientRelativeTime: ({ iso }: { iso?: string }) =>
    React.createElement("time", null, iso ?? ""),
}));

const { DecisionInbox } = await import("../app/decision-inbox-ui");
const { CommandCenter } = await import("../app/command-center-ui");

const ANSWERS = [
  { action: "approve" as const, label: "Approve and merge", tone: "approve" as const, effect: "e" },
  { action: "reject" as const, label: "Keep blocked", tone: "reject" as const, effect: "e" },
];

function item(id: string, status: string, extra: Partial<DecisionFeedItem> = {}):
    DecisionFeedItem {
  return {
    id, status, question: `q ${id}`, riskTier: 1, recommendation: {}, options: [],
    costSummary: {}, createdAt: "2026-09-01T00:00:00Z", updatedAt: "2026-09-02T00:00:00Z",
    ...extra,
  };
}

/** What the engine sends for each shape in a real decisions table. */
const ITEMS: DecisionFeedItem[] = [
  item("pending", "pending", { attention: "needs_action", allowedActions: ANSWERS }),
  item("stuck_pr", "revision_requested", { attention: "needs_action", allowedActions: ANSWERS,
                                           kind: "pull_request" }),
  item("expired_hold", "expired", { attention: "expired", allowedActions: ANSWERS,
                                    kind: "pull_request", deadlineAt: "2026-09-03T00:00:00Z" }),
  item("revising", "revision_requested", { attention: "resolved", allowedActions: [] }),
  item("approved", "approved", { attention: "resolved" }),
];

describe("the inbox groups by the engine's attention", () => {
  it("an expired hold decision is not in the default Needs action view", () => {
    expect(filterDecisionInbox(ITEMS).map((i) => i.id)).toEqual(["pending", "stuck_pr"]);
    expect(filterDecisionInbox(ITEMS, "expired").map((i) => i.id)).toEqual(["expired_hold"]);
    expect(filterDecisionInbox(ITEMS, "resolved").map((i) => i.id))
      .toEqual(["revising", "approved"]);
  });

  it("but it can still be answered, so its card draws the buttons", () => {
    const expired = ITEMS[2];
    expect(isAnswerableDecision(expired)).toBe(true);
    expect(offeredActions(expired).map((a) => a.label)).toContain("Approve and merge");
  });

  it("the Needs action count is the Command Center's approvals count", () => {
    // The engine computes both with decision_actions.attention; the API test
    // apps/api/tests/test_command_center_and_inbox_agree.py checks it against real rows.
    const cc = commandCenterFrom({ counts: { approvals: 2, expired_answerable: 1 } });
    expect(filterDecisionInbox(ITEMS, "needs_action")).toHaveLength(cc.counts.approvals);
    expect(filterDecisionInbox(ITEMS, "expired")).toHaveLength(cc.counts.expired_answerable);
  });

  it("an older API without attention falls back to the old meaning, expired apart", () => {
    const old = (status: string, offers = ANSWERS) =>
      decisionAttention({ status, allowedActions: offers });
    expect(old("pending")).toBe("needs_action");
    expect(old("revision_requested")).toBe("needs_action");
    expect(old("expired")).toBe("expired");
    expect(old("expired", [])).toBe("resolved");
    expect(old("approved")).toBe("resolved");
  });

  it("the expired filter survives a reload", () => {
    expect(parseDecisionInboxFilter("expired")).toBe("expired");
  });

  it("the Expired tab is drawn with its count, and an expired card says nothing was applied", () => {
    const markup = renderToStaticMarkup(
      <DecisionInbox feed={{ projectId: "p", apiReachable: true, items: ITEMS }} tasks={[]}
        roles={[]} projectId="p" actorId="ceo" orgId="org_1" initialFilter="expired" />,
    );
    expect(markup).toContain('data-testid="decision-filter-expired"');
    expect(markup).toContain("Expired, still answerable");
    expect(markup).toContain('data-testid="decision-expired-note"');
    expect(markup).toContain("Waiting since");
    expect(markup).toContain("nothing applied");
    expect(markup).not.toContain("q pending");
  });
});

describe("the Command Center points at the expired ones without listing them", () => {
  const counts = (over: Record<string, number>) => ({
    agents: 7, working: 0, needs_you: 0, stopped: 0, setup: 0, running: 0, stalled: 0, expired_answerable: 0, ...over,
  });

  it("says how many and links to the Expired view", () => {
    const html = renderToStaticMarkup(
      <CommandCenter projectId="p" orgId="org_1" actor="ceo"
        initial={parseCommandCenterView(view({ counts: counts({ expired_answerable: 127 }) }))} />,
    );
    expect(html).toContain('data-testid="cc2-expired-answerable"');
    expect(html).toContain("127 expired · still answerable ↗");
    expect(html).toContain("Nothing was applied. You can still answer them.");
    expect(html).toContain(decisionsHref("expired", "org_1").replace(/&/g, "&amp;"));
  });

  it("says nothing when there are none", () => {
    const html = renderToStaticMarkup(
      <CommandCenter projectId="p" orgId="org_1" actor="ceo"
        initial={parseCommandCenterView(view({ counts: counts({}) }))} />,
    );
    expect(html).not.toContain("cc2-expired-answerable");
  });

  it("NEED YOU is the inbox's Needs action count, the one number (§4.5)", () => {
    // The engine computes both with decision_actions.attention; the API test
    // apps/api/tests/test_command_center_and_inbox_agree.py checks the view against real rows.
    const needs = filterDecisionInbox(ITEMS, "needs_action").length;
    const html = renderToStaticMarkup(
      <CommandCenter projectId="p" orgId="org_1" actor="ceo"
        initial={parseCommandCenterView(view({ counts: counts({ needs_you: needs }) }))} />,
    );
    const kpi = /data-testid="cc2-kpi-needs-you"[^>]*>(\d+)</.exec(html);
    expect(Number(kpi?.[1])).toBe(needs);
  });
});
