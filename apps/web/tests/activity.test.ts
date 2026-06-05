import { describe, expect, it } from "vitest";

import { emptyAgentModuleFeed } from "../lib/agent-modules";
import { emptyAuditSummary } from "../lib/audit";
import { emptyArtifactFeed } from "../lib/artifacts";
import { emptyInboxFeed, emptyOutboxFeed } from "../lib/comms";
import { emptyCommercialFeed } from "../lib/commercial";
import { emptyActivityFeed } from "../lib/events";
import { emptyDeadletterFeed } from "../lib/deadletters";
import { emptyEntitlementFeed } from "../lib/entitlements";
import { emptyEnterpriseFeed } from "../lib/enterprise";
import { emptyPackFeed } from "../lib/packs";
import { emptyHealthSnapshotFeed } from "../lib/health-snapshots";
import { emptyProviderOpsFeed } from "../lib/providerops";
import { emptyReceiptFeed } from "../lib/receipts";
import { emptyRoleFeed } from "../lib/roles";
import { emptyRoutingLogFeed } from "../lib/routing";
import { emptySelfImprovementFeed } from "../lib/self-improvement";
import { emptySkillFeed } from "../lib/skills";

describe("empty API feeds", () => {
  it("returns an unavailable artifact feed", () => {
    const feed = emptyArtifactFeed("artifact api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("artifact api down");
  });

  it("returns an unavailable activity feed", () => {
    const feed = emptyActivityFeed("event api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("event api down");
  });

  it("returns an unavailable deadletter feed", () => {
    const feed = emptyDeadletterFeed("deadletter api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("deadletter api down");
  });

  it("returns an unavailable pack feed", () => {
    const feed = emptyPackFeed("pack api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("pack api down");
  });

  it("returns an unavailable agent module feed", () => {
    const feed = emptyAgentModuleFeed("agent module api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("agent module api down");
  });

  it("returns an unavailable role feed", () => {
    const feed = emptyRoleFeed("role api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("role api down");
  });

  it("returns an unavailable entitlement feed", () => {
    const feed = emptyEntitlementFeed("entitlement api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("entitlement api down");
  });

  it("returns an unavailable commercial feed", () => {
    const feed = emptyCommercialFeed("commercial api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.entitlements).toEqual([]);
    expect(feed.blockedReason).toBe("commercial api down");
  });

  it("returns an unavailable enterprise feed", () => {
    const feed = emptyEnterpriseFeed("enterprise api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.clusters).toEqual([]);
    expect(feed.scimUsers).toEqual([]);
    expect(feed.summary.blockingGapCount).toBe(0);
    expect(feed.blockedReason).toBe("enterprise api down");
  });

  it("returns an unavailable ProviderOps feed", () => {
    const feed = emptyProviderOpsFeed("providerops api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.providerAccounts).toEqual([]);
    expect(feed.summary.resourceCount).toBe(0);
    expect(feed.blockedReason).toBe("providerops api down");
  });

  it("returns an unavailable health snapshot feed", () => {
    const feed = emptyHealthSnapshotFeed("health snapshot api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("health snapshot api down");
  });

  it("returns an unavailable routing log feed", () => {
    const feed = emptyRoutingLogFeed("routing api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("routing api down");
  });

  it("returns an unavailable skill feed", () => {
    const feed = emptySkillFeed("skill api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("skill api down");
  });

  it("returns an unavailable receipt feed", () => {
    const feed = emptyReceiptFeed("receipt api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("receipt api down");
  });

  it("returns an unavailable audit summary", () => {
    const summary = emptyAuditSummary("audit api down");

    expect(summary.apiReachable).toBe(false);
    expect(summary.counts.receipts).toBe(0);
    expect(summary.blockedReason).toBe("audit api down");
  });

  it("returns an unavailable outbox feed", () => {
    const feed = emptyOutboxFeed("outbox api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("outbox api down");
  });

  it("returns an unavailable inbox feed", () => {
    const feed = emptyInboxFeed("inbox api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("inbox api down");
  });

  it("returns an unavailable self-improvement feed", () => {
    const feed = emptySelfImprovementFeed("self improvement api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("self improvement api down");
  });

  it("returns self-improvement feed shape with proposal states for e2e (P2 SIM persist/states)", () => {
    // structure test for feed item states (proposal/canary/rollout/rollback) used in UI conditionals
    const feed = {
      projectId: "boss-dogfood",
      apiReachable: true,
      items: [
        {
          id: "imp1",
          taskId: "t1",
          title: "p",
          summary: "s",
          changeRef: "r",
          diffSummary: "d",
          status: "rolled_out",
          riskTier: 2,
          createdBy: "a",
          createdAt: "t",
          updatedAt: "t",
        },
      ],
    };
    expect(feed.items[0].status).toBe("rolled_out");
    expect(feed.apiReachable).toBe(true);
  });
});
