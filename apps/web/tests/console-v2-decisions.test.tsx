import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { DecisionsScreen } from "../components/v2/DecisionsScreen";
import type { DecisionQueue, PanelFeed, V2Decision, V2Panel } from "../lib/v2/decisions";

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


// --------------------------------------------------------------------------- //
// Panels
//
// The reference streams a scripted debate with a live vote tally. BOSS's panels are real and
// produce real minutes, but they are NOT watched live. A fake live feed would be the most
// convincing lie in this console.
// --------------------------------------------------------------------------- //
const panel = (over: Partial<V2Panel> = {}): V2Panel => ({
  id: "m1",
  title: "Pricing panel",
  purpose: "Decide the pricing model.",
  status: "completed",
  deliberating: false,
  panelists: ["cpo", "cto"],
  decisions: ["Go freemium"],
  dissent: [],
  blocked: false,
  blocked_reason: null,
  has_minutes: true,
  completed_at: "2026-08-26T12:00:00Z",
  created_at: "2026-08-26T11:00:00Z",
  turns: [],
  synthesis: null,
  ...over,
});

const panels = (over: Partial<PanelFeed> = {}): PanelFeed => ({
  project_id: "boss-dogfood",
  sessions: [panel()],
  deliberating: 0,
  blocked: 0,
  streamed: false,
  stream_note: "BOSS panels are not streamed. A meeting runs and its minutes land when it completes.",
  apiReachable: true,
  ...over,
});

const withPanels = (p: PanelFeed) =>
  renderToStaticMarkup(<DecisionsScreen queue={queue()} panels={p} />);

describe("panels", () => {
  it("says it is not a live feed", () => {
    // Animating turns that are not arriving would be the most convincing lie here.
    const out = withPanels(panels());
    expect(out).toContain('data-testid="v2-panels-not-streamed"');
    expect(out).toMatch(/not streamed/);
  });

  it("shows dissent separately from the decision", () => {
    // A panel that agreed and a panel that overruled an objection produce the same decision
    // and are not the same event. BOSS records this and v1 never displayed it.
    const out = withPanels(panels({
      sessions: [panel({ decisions: ["Ship it"], dissent: ["Legal objected: consent copy"] })],
    }));
    expect(out).toContain('data-testid="v2-panel-dissent-m1"');
    expect(out).toMatch(/Legal objected/);
  });

  it("a unanimous panel shows no dissent line", () => {
    expect(withPanels(panels())).not.toContain('data-testid="v2-panel-dissent-m1"');
  });

  it("a blocked panel says why, and is neither debating nor concluded", () => {
    const out = withPanels(panels({
      sessions: [panel({ blocked: true, blocked_reason: "waiting on the PRD", deliberating: false })],
      blocked: 1,
    }));
    expect(out).toContain('data-testid="v2-panel-blocked-m1"');
    expect(out).toMatch(/blocked/);
    expect(out).toMatch(/waiting on the PRD/);
  });

  it("a panel with no minutes says so", () => {
    // "Concluded" with nothing written down is not the same as concluded.
    expect(withPanels(panels({ sessions: [panel({ has_minutes: false })] })))
      .toContain('data-testid="v2-panel-nominutes-m1"');
  });

  it("an unreachable panel feed does not read as no panels", () => {
    expect(withPanels(panels({ apiReachable: false, sessions: [] })))
      .toContain('data-testid="v2-panels-unreachable"');
  });

  it("puts the session list beside what was said", () => {
    const out = withPanels(panels());
    expect(out).toContain('data-testid="v2-panels-list"');
    expect(out).toContain('data-testid="v2-panel-detail-m1"');
  });

  it("shows what each agent actually said", () => {
    // A panel whose reasoning is invisible is indistinguishable from a coin toss with minutes
    // attached. BOSS records the turns and the console has never displayed them.
    const out = withPanels(panels({
      sessions: [panel({ turns: [
        { speaker: "cto", phase: "review", text: "The cost model does not hold.", stance: null },
        { speaker: "qa", phase: "review", text: "Two tests are missing.", stance: null },
      ] })],
    }));
    expect(out).toContain('data-testid="v2-panel-turns-m1"');
    expect(out).toMatch(/The cost model does not hold/);
    expect(out).toMatch(/Two tests are missing/);
  });

  it("shows no stance badge when the transcript did not record one", () => {
    // Inferring FOR/AGAINST from the words would put a vote in an agent's name on a page whose
    // whole purpose is the record.
    const out = withPanels(panels({
      sessions: [panel({ turns: [
        { speaker: "cto", phase: "", text: "I have concerns about the cost.", stance: null },
      ] })],
    }));
    expect(out).not.toContain('data-testid="v2-turn-stance-m1-0"');
  });

  it("shows a stance when the transcript did record one", () => {
    const out = withPanels(panels({
      sessions: [panel({ turns: [
        { speaker: "cto", phase: "", text: "Yes.", stance: "FOR" },
      ] })],
    }));
    expect(out).toContain('data-testid="v2-turn-stance-m1-0"');
  });

  it("a panel with no transcript says what is missing, not nothing", () => {
    const out = withPanels(panels({ sessions: [panel({ turns: [] })] }));
    expect(out).toContain('data-testid="v2-panel-noturns-m1"');
    expect(out).toMatch(/only\s+the outcome below/);
  });

  it("names who was on the panel", () => {
    expect(withPanels(panels())).toContain('data-testid="v2-panel-panelists-m1"');
  });

  it("the verdict box carries the decision and any dissent separately", () => {
    const out = withPanels(panels({
      sessions: [panel({ decisions: ["Ship it"], dissent: ["Legal objected"] })],
    }));
    expect(out).toContain('data-testid="v2-panel-verdict-m1"');
    expect(out).toContain('data-testid="v2-panel-decisions-m1"');
    expect(out).toContain('data-testid="v2-panel-dissent-m1"');
  });

  it("draws no vote tally, because BOSS does not record votes", () => {
    // The reference totals FOR and AGAINST. Counting stances BOSS never recorded would be a
    // number invented on the one screen that exists to be the record.
    const out = withPanels(panels());
    expect(out).not.toMatch(/TALLY/);
    expect(out).not.toMatch(/FOR \d+\s+AGAINST/);
  });

  it("renders the arguments as markdown, not as raw asterisks", () => {
    // The agents write headings, bold and tables. Shown raw, the reader sees ** and ## instead
    // of emphasis — which on a page of four long arguments is most of what they see.
    const out = withPanels(panels({
      sessions: [panel({ turns: [
        { speaker: "cto", phase: "round 1", text: "**Cost** is the risk.", stance: null },
      ] })],
    }));
    expect(out).toContain("<strong>Cost</strong>");
    expect(out).not.toMatch(/\*\*Cost\*\*/);
  });

  it("shows the panel's own synthesis as the verdict", () => {
    // The `decisions` column is empty on every real meeting; the synthesis is where the
    // outcome actually is.
    const out = withPanels(panels({
      sessions: [panel({ decisions: [], synthesis: "Freemium with a paid upsell." })],
    }));
    expect(out).toContain('data-testid="v2-panel-synthesis-m1"');
    expect(out).toMatch(/Freemium with a paid upsell/);
  });

  it("the decisions queue still renders without panels", () => {
    // Panels are additive; the queue must not depend on them.
    const out = renderToStaticMarkup(<DecisionsScreen queue={queue()} />);
    expect(out).toContain('data-testid="v2-decisions"');
    expect(out).not.toContain('data-testid="v2-panels-list"');
  });
});
