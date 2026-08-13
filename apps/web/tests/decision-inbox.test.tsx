import { readFileSync } from "node:fs";
import { resolve } from "node:path";

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

const { DecisionInbox, DecisionInboxEmpty } = await import(
  "../app/decision-inbox-ui"
);

function item(
  overrides: Partial<DecisionFeedItem> & Pick<DecisionFeedItem, "id" | "status">,
): DecisionFeedItem {
  return {
    question: overrides.question ?? overrides.id,
    riskTier: 2,
    recommendation: { action: "review_pull_request", title: "inbox filter" },
    options: [],
    costSummary: {},
    createdAt: "2026-08-13T00:00:00Z",
    updatedAt: "2026-08-13T00:00:00Z",
    ...overrides,
  };
}

function feed(items: DecisionFeedItem[], extra: Partial<DecisionFeed> = {}): DecisionFeed {
  return {
    projectId: "boss-dogfood",
    apiReachable: true,
    items,
    ...extra,
  };
}

function html(node: React.ReactElement) {
  return renderToStaticMarkup(node);
}

describe("DecisionInbox (W1.6 actionable default)", () => {
  it("defaults to needs_action and hides resolved history", () => {
    const markup = html(
      <DecisionInbox
        feed={feed([
          item({ id: "dec_live", status: "pending", question: "Approve the PR?" }),
          item({ id: "dec_old", status: "approved", question: "Already done" }),
        ])}
        tasks={[]}
        roles={[]}
        projectId="boss-dogfood"
        actorId="executive_console"
        orgId="org_1"
      />,
    );

    expect(markup).toContain('data-decision-filter="needs_action"');
    expect(markup).toContain("Approve the PR?");
    expect(markup).not.toContain("Already done");
    expect(markup).toContain('data-testid="decision-approve"');
    expect(markup).toContain('data-testid="decision-reject"');
    expect(markup).toContain('data-testid="decision-escalate"');
    expect(markup).toContain('value="approve"');
    expect(markup).toContain('value="reject"');
    expect(markup).toContain('value="escalate"');
    expect(markup).toContain("Escalate to role or approver");
  });

  it("renders an honest empty pending queue, not a silent history dump", () => {
    const markup = html(
      <DecisionInbox
        feed={feed([
          item({ id: "dec_old", status: "rejected", question: "Old rejection" }),
        ])}
        tasks={[]}
        roles={[]}
        projectId="boss-dogfood"
        actorId="executive_console"
        orgId="org_1"
      />,
    );

    expect(markup).toContain('data-empty-state="queue-clear"');
    expect(markup).toContain("Queue is clear");
    expect(markup).toContain("Budget reservations");
    expect(markup).toContain("Pull-request");
    expect(markup).toContain("TPM");
    expect(markup).not.toContain("Old rejection");
    expect(markup).not.toContain("No active decisions");
  });

  it("DecisionInboxEmpty explains how cards appear", () => {
    const markup = html(
      <DecisionInboxEmpty filter="needs_action" orgId="org_1" />,
    );
    expect(markup).toContain("Queue is clear");
    expect(markup).toContain("Budget reservations or spend that need a human gate");
    expect(markup).toContain("TPM / credit / model-limit escalations");
    expect(markup).toContain("View resolved history");
  });

  it("mounts DecisionInbox on Mission Control instead of a raw unfiltered list", () => {
    const page = readFileSync(resolve(__dirname, "../app/page.tsx"), "utf8");
    expect(page).toContain("<DecisionInbox");
    expect(page).toContain("initialFilter={requestedDecisionFilter}");
    expect(page).toContain("parseDecisionInboxFilter");
    expect(page).not.toContain('id="decision-inbox"');
    expect(page).not.toContain("No active decisions");
    expect(page).not.toMatch(/decisions\.items\.map\(\(decision\)/);
  });

  it("does not pretend the API is a clear queue when it is unreachable", () => {
    const markup = html(
      <DecisionInbox
        feed={feed([], { apiReachable: false, blockedReason: "API returned 503" })}
        tasks={[]}
        roles={[]}
        projectId="boss-dogfood"
        actorId="executive_console"
      />,
    );
    expect(markup).toContain('data-empty-state="api-unavailable"');
    expect(markup).toContain("API returned 503");
    expect(markup).not.toContain("Queue is clear");
  });
});
