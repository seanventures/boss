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
    expect(markup).toContain('value="approve"');
    expect(markup).toContain('value="reject"');
    // REWRITTEN: this used to assert an "Escalate" button posting action=escalate, which the
    // server never accepted — every press failed. Handing a decision on is delegation now,
    // drawn only when the engine offers it (see the contract tests below).
    expect(markup).not.toContain('value="escalate"');
  });

  it("draws exactly the actions the engine offers, labelled as it says", () => {
    const markup = html(
      <DecisionInbox
        feed={feed([item({
          id: "dec_pr", status: "pending", question: "Merge it?", kind: "pull_request",
          allowedActions: [
            { action: "approve", label: "Approve and merge", tone: "approve", effect: "e" },
            { action: "reject", label: "Keep blocked", tone: "reject", effect: "e" },
            { action: "revise", label: "Request changes", tone: "neutral", effect: "e",
              needs: "note" },
            { action: "delegate", label: "Hand to someone else", tone: "neutral", effect: "e",
              needs: "delegate_to" },
          ],
        })])}
        tasks={[]} roles={[]} projectId="p" actorId="ceo" orgId="org_1"
      />,
    );
    expect(markup).toContain("Approve and merge");
    expect(markup).toContain("Keep blocked");
    expect(markup).toContain('data-testid="decision-delegate"');
    expect(markup).toContain('name="delegate_to"');
    expect(markup).toContain('value="delegate"');
    expect(markup).not.toContain('name="escalate_to"');
  });

  it("a PR parked by the old 'Request changes' is answerable again and listed as needing you", () => {
    // Nothing read revision_requested on a PR and the status refused approval, so such a PR
    // could never be merged from BOSS. The engine reopens it and offers the answer again.
    const markup = html(
      <DecisionInbox
        feed={feed([item({
          id: "dec_stuck", status: "revision_requested", kind: "pull_request",
          question: "Merge the stuck PR?",
          allowedActions: [
            { action: "approve", label: "Approve and merge", tone: "approve", effect: "e" },
            { action: "reject", label: "Keep blocked", tone: "reject", effect: "e" },
          ],
        })])}
        tasks={[]} roles={[]} projectId="p" actorId="ceo" orgId="org_1"
      />,
    );
    expect(markup).toContain("Merge the stuck PR?");
    expect(markup).toContain("Approve and merge");
    expect(markup).toContain('data-testid="decision-approve"');
  });

  it("a workflow's own decision step says that approving releases the step", () => {
    const markup = html(
      <DecisionInbox
        feed={feed([item({
          id: "dec_wf", status: "pending", kind: "workflow_gate", question: "Publish?",
          recommendation: { action: "resume_workflow" },
          allowedActions: [{ action: "approve", label: "Approve", tone: "approve", effect: "e" }],
        })])}
        tasks={[]} roles={[]} projectId="p" actorId="ceo" orgId="org_1"
      />,
    );
    expect(markup).toContain("Approving releases the step");
    expect(markup).not.toContain("no automated follow-on");
  });

  it("a re-check's Fix card says the follow-up build it starts, never 'no follow-on'", () => {
    // BOSSAPP-1354: "Fix these" started a build while the card said nothing would follow.
    const effect = "Starts a follow-up build of 26 items on this change's branch, about $5.54, "
      + "that fixes the re-check's findings. Needs the Steer builds permission.";
    const markup = html(
      <DecisionInbox
        feed={feed([item({
          id: "dec_fix", status: "pending", question: "Fix these (about $5.54)",
          recommendation: { action: "start_recheck_followup", run_id: "crun_1" },
          allowedActions: [
            { action: "approve", label: "Fix these (about $5.54)", tone: "approve", effect },
            { action: "reject", label: "Not now", tone: "reject", effect: "Nothing is built." },
          ],
        })])}
        tasks={[]} roles={[]} projectId="p" actorId="ceo" orgId="org_1"
      />,
    );
    expect(markup).toContain("follow-up build of 26 items");
    expect(markup).not.toContain("no automated follow-on");
  });

  it("any card whose Approve the engine describes shows those words, not 'no follow-on'", () => {
    const markup = html(
      <DecisionInbox
        feed={feed([item({
          id: "dec_new", status: "pending", question: "Something new?",
          recommendation: { action: "an_action_this_console_never_heard_of" },
          allowedActions: [{ action: "approve", label: "Go", tone: "approve",
                             effect: "Starts the new thing now." }],
        })])}
        tasks={[]} roles={[]} projectId="p" actorId="ceo" orgId="org_1"
      />,
    );
    expect(markup).toContain("Starts the new thing now.");
    expect(markup).not.toContain("no automated follow-on");
  });

  it("a budget or capacity refusal is a notice with the fix, and only Dismiss", () => {
    // boss-1: model_capacity_denied showed Approve/Reject that did nothing and an internal
    // question string as its title.
    const markup = html(
      <DecisionInbox
        feed={feed([item({
          id: "dec_cap", status: "pending", kind: "refusal",
          question: "Model capacity refused typed decision setup:typesafe-key/key-proof-fa84",
          recommendation: { action: "model_capacity_denied", occurrences: 4 },
          allowedActions: [{ action: "reject", label: "Dismiss", tone: "neutral", effect: "e" }],
          guidance: { what_happened: "No connected model had capacity.",
                      what_to_do: "Add credit in Setup, then Retry the step.",
                      area: "setup", link_label: "Open Setup" },
        })])}
        tasks={[]} roles={[]} projectId="p" actorId="ceo" orgId="org_1"
      />,
    );
    expect(markup).toContain("No connected model had capacity.");
    expect(markup).toContain('data-testid="decision-guidance"');
    expect(markup).toContain("area=setup");
    expect(markup).toContain(">Dismiss<");
    expect(markup).not.toContain('data-testid="decision-approve"');
    expect(markup).toContain('name="reject_label" value="Dismiss"');
    // Seen-count on the PENDING card, where it matters.
    expect(markup).toContain('data-testid="decision-occurrences"');
  });

  it("a gate on a run carries the run id so the result can link straight to it", () => {
    const markup = html(
      <DecisionInbox
        feed={feed([item({
          id: "dec_gate", status: "pending", kind: "deploy_gate",
          recommendation: { workflow_run_id: "wfr_9", capability: "deploy" },
          allowedActions: [{ action: "approve", label: "Ship it", tone: "approve", effect: "e" }],
        })])}
        tasks={[]} roles={[]} projectId="p" actorId="ceo" orgId="org_1"
      />,
    );
    expect(markup).toContain('name="workflow_run_id" value="wfr_9"');
    expect(markup).toContain("Ship it");
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
    expect(markup).toContain("Spending that needs your OK");
    expect(markup).toContain("Pull requests waiting");
    expect(markup).toContain("rate limit");
    expect(markup).not.toContain("Old rejection");
    expect(markup).not.toContain("No active decisions");
  });

  it("DecisionInboxEmpty explains how cards appear", () => {
    const markup = html(
      <DecisionInboxEmpty filter="needs_action" orgId="org_1" />,
    );
    expect(markup).toContain("Queue is clear");
    expect(markup).toContain("Spending that needs your OK");
    expect(markup).toContain("ran out of credit or hit its rate limit");
    // Plain words (stranger test r2): no insider jargon on a first-run screen.
    for (const insider of ["TPM", "deadletter", "software factory", "governance ceiling"]) expect(markup).not.toContain(insider);
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

  it("shows hold default_action and reason on pending cards", () => {
    const markup = html(
      <DecisionInbox
        feed={feed([
          item({
            id: "dec_hold",
            status: "pending",
            question: "Approve sealed canary?",
            defaultAction: "hold",
            reason: "merge_decision_required",
          }),
        ])}
        tasks={[]}
        roles={[]}
        projectId="boss-dogfood"
        actorId="executive_console"
        orgId="org_1"
      />,
    );
    expect(markup).toContain('data-testid="decision-default-action"');
    expect(markup).toContain("default hold");
    expect(markup).toContain('data-testid="decision-reason"');
    // The reason in words, never the raw code.
    expect(markup).toMatch(/Why<\/span> (<!-- -->)?merging needs a person&#x27;s decision/);
    expect(markup).not.toContain("merge decision required");
    expect(markup).not.toContain("merge_decision_required<");
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
