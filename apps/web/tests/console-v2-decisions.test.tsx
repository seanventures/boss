import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { DecisionsScreen } from "../components/v2/DecisionsScreen";
import type { DecisionQueue, V2Decision } from "../lib/v2/decisions";

/**
 * The Decisions screen.
 *
 * The parity map's stated risk for this area is "a gate that silently kills work", and that is
 * literal: an unanswered gate with a deadline and a default action resolves itself, so the
 * work either proceeds unreviewed or is dropped. Everything below tests that this screen makes
 * that visible rather than rendering a tidy list of pending questions.
 */

const dec = (over: Partial<V2Decision> = {}): V2Decision => ({
  id: "dec_1",
  task_id: "task_1",
  question: "Approve the PRD for Shorty?",
  recommendation: "",
  options: [],
  risk_tier: 1,
  status: "pending",
  delegated_to: null,
  deadline_at: null,
  default_action: null,
  if_ignored: "waits for you — nothing happens on its own",
  created_at: "2026-08-26T12:00:00Z",
  ...over,
});

const queue = (over: Partial<DecisionQueue> = {}): DecisionQueue => ({
  project_id: "boss-dogfood",
  items: [dec()],
  total: 1,
  auto_resolving: 0,
  apiReachable: true,
  ...over,
});

const html = (q: DecisionQueue) => renderToStaticMarkup(<DecisionsScreen queue={q} />);

describe("what happens if nobody answers", () => {
  it("a self-resolving gate is marked as one", () => {
    const out = html(queue({
      items: [dec({ deadline_at: "2026-08-26T14:00:00Z", default_action: "approve",
                    if_ignored: "approve automatically" })],
      auto_resolving: 1,
    }));
    expect(out).toContain('data-will-fire="true"');
    expect(out).toContain("approve automatically");
  });

  it("a gate that waits forever is not marked as firing", () => {
    // Marking everything urgent is the same as marking nothing urgent.
    const out = html(queue());
    expect(out).toContain('data-will-fire="false"');
    expect(out).toMatch(/nothing happens on its own/);
  });

  it("the count of decisions that will be made without you leads the page", () => {
    const out = html(queue({ total: 5, auto_resolving: 2 }));
    expect(out).toContain('data-testid="v2-decisions-auto"');
    expect(out).toMatch(/2 will be decided without you/);
  });

  it("nothing self-resolving means no alarming count is shown", () => {
    expect(html(queue({ total: 3, auto_resolving: 0 })))
      .not.toContain('data-testid="v2-decisions-auto"');
  });

  it("the deadline is shown alongside the consequence", () => {
    // A consequence with no time attached cannot be prioritised against anything else.
    const out = html(queue({
      items: [dec({ deadline_at: "2026-08-26T14:30:00Z", default_action: "reject",
                    if_ignored: "reject automatically" })],
    }));
    expect(out).toMatch(/2026-08-26 14:30/);
  });
});

describe("an empty list must not be reassuring by accident", () => {
  it("an unreachable API says so, and says why that is dangerous here", () => {
    // On this page specifically, a wrongly-empty list is worse than an error: gates can fire
    // on their own, so "nothing is waiting" invites the operator to walk away.
    const out = html(queue({ apiReachable: false, blockedReason: "API returned 503", items: [], total: 0 }));
    expect(out).toContain('data-testid="v2-decisions-unreachable"');
    expect(out).toContain("503");
    expect(out).toMatch(/NOT/);
  });

  it("a genuinely empty queue says work is not blocked", () => {
    const out = html(queue({ items: [], total: 0 }));
    expect(out).toContain('data-testid="v2-decisions-none"');
    expect(out).toMatch(/not waiting on an approval/i);
  });

  it("the two empty states are distinguishable", () => {
    const a = html(queue({ apiReachable: false, items: [], total: 0 }));
    const b = html(queue({ items: [], total: 0 }));
    expect(a).toContain("v2-decisions-unreachable");
    expect(b).toContain("v2-decisions-none");
    expect(b).not.toContain("v2-decisions-unreachable");
  });
});

describe("enough to act on", () => {
  it("shows how the question can be answered, not just that it must be", () => {
    const out = html(queue({ items: [dec({ options: ["Approve and deploy", "Reject"] })] }));
    expect(out).toContain("Approve and deploy");
    expect(out).toContain("Reject");
  });

  it("carries a recommendation when there is one", () => {
    expect(html(queue({ items: [dec({ recommendation: "Approve — all gates passed" })] })))
      .toContain('data-testid="v2-decision-rec-dec_1"');
  });

  it("invents nothing when there is no recommendation", () => {
    // A bland invented recommendation and a real one look identical, and only one is honest.
    expect(html(queue())).not.toContain('data-testid="v2-decision-rec-dec_1"');
  });

  it("a delegated gate says who holds it", () => {
    // Delegated is not resolved. Showing it as plain 'pending' loses who is actually blocking.
    const out = html(queue({ items: [dec({ status: "delegated", delegated_to: "ceo" })] }));
    expect(out).toMatch(/delegated/);
    expect(out).toMatch(/ceo/);
  });

  it("answering links to the one place that can answer", () => {
    // Duplicating an approval control before the parity check has run would ship two paths to
    // one irreversible act.
    expect(html(queue())).toContain('data-testid="v2-decision-open-dec_1"');
  });
});
