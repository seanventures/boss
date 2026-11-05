import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { DecisionFeed, DecisionFeedItem } from "../lib/decisions";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
}));

vi.mock("../app/submit-button", () => ({
  SubmitButton: (props: React.ButtonHTMLAttributes<HTMLButtonElement>) =>
    React.createElement("button", { ...props, type: props.type ?? "submit" }, props.children),
}));

vi.mock("../app/client-relative-time", () => ({
  ClientRelativeTime: ({ iso }: { iso?: string }) =>
    React.createElement("time", null, iso ?? ""),
}));

const { DecisionInbox } = await import("../app/decision-inbox-ui");

function item(
  overrides: Partial<DecisionFeedItem> & Pick<DecisionFeedItem, "id" | "status">,
): DecisionFeedItem {
  return {
    question: overrides.question ?? overrides.id,
    riskTier: 2,
    recommendation: { action: "review_pull_request" },
    options: [],
    costSummary: {},
    createdAt: "2026-08-13T00:00:00Z",
    updatedAt: "2026-08-13T00:00:00Z",
    ...overrides,
  };
}

function feed(items: DecisionFeedItem[]): DecisionFeed {
  return { projectId: "boss-dogfood", apiReachable: true, items };
}

function html(node: React.ReactElement) {
  return renderToStaticMarkup(node);
}

const settles = [
  {
    action: "settle" as const,
    settle_at: "zero" as const,
    label: "Settle at $0 (provider refused)",
    tone: "approve" as const,
    effect: "Records that the provider refused the call, so nothing was spent, and closes this hold.",
  },
  {
    action: "settle" as const,
    settle_at: "estimate" as const,
    label: "Settle at the estimate",
    tone: "neutral" as const,
    effect: "Records the original estimate as the cost, because the outcome could not be confirmed, and closes this hold.",
  },
];

describe("a legacy exposure card", () => {
  it("settles at $0 or at the estimate, and does not offer approve", () => {
    const markup = html(
      <DecisionInbox
        feed={feed([
          item({
            id: "dec_legacy",
            status: "pending",
            question: "Reconcile historical unresolved model exposure",
            recommendation: { action: "reconcile_legacy_model_exposure" },
            allowedActions: settles,
            attention: "needs_action",
          }),
        ])}
        tasks={[]}
        roles={[]}
        projectId="boss-dogfood"
        actorId="ceo"
        orgId="org_1"
      />,
    );
    expect(markup).toContain("Settle at $0 (provider refused)");
    expect(markup).toContain("Settle at the estimate");
    expect(markup).toContain('name="settle_at"');
    expect(markup).toContain('value="zero"');
    expect(markup).toContain('value="estimate"');
    expect(markup).toContain('name="action" value="settle"');
    expect(markup).not.toContain('value="approve"');
    expect(markup).not.toContain(">Approve<");
  });

  it("does not put settle on an ordinary card", () => {
    const markup = html(
      <DecisionInbox
        feed={feed([
          item({ id: "dec_pr", status: "pending", question: "Approve the PR?" }),
        ])}
        tasks={[]}
        roles={[]}
        projectId="boss-dogfood"
        actorId="ceo"
        orgId="org_1"
      />,
    );
    expect(markup).toContain('value="approve"');
    expect(markup).not.toContain("Settle at $0");
    expect(markup).not.toContain('name="settle_at"');
  });
});
