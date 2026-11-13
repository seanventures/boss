/**
 * BOSSAPP-885 on Organization › Lines: each planned role shows its chain of command (who takes
 * its stalled work, in order, before a person), a retired manager strikes through and blocks the
 * role, and stalled work offered to a manager is listed with a Take over button that posts the
 * takeover through /api/org as the manager the chain offered it to.
 */
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { OrgChartArea } from "../app/workforce-ui";
import { codeOnly } from "./_helpers/source";
import type { OrgChart, OrgChainView } from "../lib/v2/workforce";

const chart = (over: Partial<OrgChart> = {}): OrgChart => ({
  project_id: "p", window_hours: 168, apiReachable: true,
  derived_from: "observed handoffs", note: "", root_inferred: false,
  nodes: [], tree: [], roots: [], ...over,
} as OrgChart);

const rung = (id: string, retired = false) => ({
  party: id.startsWith("person:") ? id : `agent:${id}`, kind: id.startsWith("person:") ? "person" : "agent",
  id: id.replace(/^person:/, ""), name: id.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()), retired,
}) as OrgChainView["chain"][number];

const captain = { id: "captain", name: "Captain", role: "captain", depth: 0 } as never;
const qa = { id: "qa", name: "QA", role: "qa", depth: 1 } as never;

function planned(over: Partial<OrgChart> = {}) {
  return renderToStaticMarkup(<OrgChartArea projectId="p" actor="ceo" chart={chart({
    nodes: [captain, qa], tree: [captain, qa],
    declared: { captain: { reports_to: "person:usr_lead" }, qa: { reports_to: "captain" } },
    ...over,
  })} />);
}

describe("an org chart that carries authority", () => {
  it("draws each role's chain of command up to the person", () => {
    const out = planned({ chains: {
      qa: { agent: "agent:qa", chain: [rung("qa"), rung("captain"), rung("person:usr_lead")],
            acting_manager: rung("captain"), retired_gap: null, blocked: null },
      captain: { agent: "agent:captain", chain: [rung("captain"), rung("person:usr_lead")],
                 acting_manager: null, retired_gap: null, blocked: null },
    } });
    expect(out).toContain('data-testid="org-chain-qa"');
    expect(out).toMatch(/org-chain-rung" data-kind="agent">Captain/);
    expect(out).toMatch(/data-kind="person">usr_lead/);
    expect(out).not.toContain("org-chain-blocked-qa");
    expect(out).toContain("A stalled role&#x27;s work goes to its manager first, then a person.");
  });

  it("strikes a retired manager through and says the role is blocked", () => {
    const out = planned({ chains: {
      qa: { agent: "agent:qa", chain: [rung("qa"), rung("captain", true)], acting_manager: null,
            retired_gap: rung("captain", true),
            blocked: "'qa' cannot start new work: its chain of command passes through 'captain', which is retired." },
    } });
    expect(out).toContain('data-testid="org-chain-blocked-qa"');
    expect(out).toMatch(/data-retired="true">Captain/);
    expect(out).toContain("which is retired");
  });

  it("lists stalled work waiting on a manager with a gated Take over", () => {
    const out = planned({ takeover_offers: [{
      id: "orgesc_1", run_id: "crun_1", work_item_id: "wi_1", subject: "agent:qa",
      offered_to: "agent:captain", reason: "supervisor_reach_out", question: "Which API?",
      deadline: null, chain: ["agent:qa", "agent:captain"], origin: "person_wait",
      title: "Fix the orders contract",
    }] });
    expect(out).toContain('data-testid="org-takeover"');
    expect(out).toContain("Fix the orders contract");
    expect(out).toContain("QA stalled · Captain has it");
    expect(out).toContain('data-testid="org-takeover-btn-crun_1"');
    expect(out).toContain('data-permission="agent:update"');
    // The press posts the takeover as the manager the chain offered it to.
    const src = codeOnly("app/workforce-ui.tsx");
    expect(src).toContain('op: "takeover"');
    expect(src).toContain("taker: offer.offered_to");
  });

  it("shows nothing new when nobody is waiting and no chain is declared", () => {
    const out = planned();
    expect(out).not.toContain("org-takeover");
    expect(out).not.toContain("org-chain-");
  });
});
