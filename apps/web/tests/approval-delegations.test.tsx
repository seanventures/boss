/**
 * APPROVE ON MY BEHALF (IDENTITY.md §6.15): a decision a person's approval delegate answered
 * names both — "by <delegate>, for <person>" — and a delegation reads as what it allows.
 */
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { decisionStatusDetail, delegateName } from "../app/decision-inbox-ui";
import { NEVER, delegationLine, type ApprovalDelegation } from "../app/account/agents/approve-on-my-behalf";
import type { DecisionFeedItem } from "../lib/decisions";

const base = {
  id: "dec_1", question: "Approve the PRD?", status: "approved", riskTier: 2, recommendation: {},
  options: [], costSummary: {}, approvedBy: "usr_olive", approvedByName: "Olive Owner",
  approvedAt: "2026-10-28T02:00:00Z", createdAt: "2026-10-28T01:00:00Z", updatedAt: "2026-10-28T02:00:00Z",
} as unknown as DecisionFeedItem;

describe("a decision a delegate answered names both identities", () => {
  it("approved by the delegate, for the person", () => {
    const line = decisionStatusDetail({ ...base, viaDelegate: "sva_x", viaDelegateName: "Approves for Olive Owner",
      viaForName: "Olive Owner" });
    expect(line).toMatch(/^Approved by Approves for Olive Owner, for Olive Owner at /);
  });
  it("rejected by the delegate names the person too", () => {
    const line = decisionStatusDetail({ ...base, status: "rejected", approvedBy: undefined, approvedAt: undefined,
      viaDelegate: "sva_x", viaDelegateName: null, viaForName: "Olive Owner" });
    expect(line).toBe("Rejected by their approval delegate, for Olive Owner.");
  });
  it("a person's own approval reads as before", () => {
    expect(decisionStatusDetail(base)).toMatch(/^Approved by Olive Owner at /);
    expect(delegateName({ viaDelegateName: null })).toBe("their approval delegate");
  });
});

describe("a delegation reads as what it allows", () => {
  it("kinds, projects, expiry and the cap", () => {
    const d = {
      id: "apd_1", status: "active", person: { id: "usr_olive", name: "Olive" }, delegate: { id: "sva_x", name: "D" },
      key_id: "key_1", kinds: ["prd"], kind_labels: ["PRD approvals"], projects: ["boss-platform"],
      extra_risk_tiers: [], max_risk_tier: 2, daily_cap: 5, used_today: 2, purpose: "",
      created_at: "2026-10-28T00:00:00Z", expires_at: "2026-11-27T00:00:00Z", revoked_at: null, last_used_at: null,
    } as ApprovalDelegation;
    const line = delegationLine(d);
    expect(line).toContain("PRD approvals · boss-platform · until");
    expect(line).toContain("up to 5 a day (2 today)");
    expect(renderToStaticMarkup(<span>{NEVER}</span>)).toContain("Never merges to main, deploys, raises a budget");
  });
});
