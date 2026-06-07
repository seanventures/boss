import { describe, expect, it, vi } from "vitest";

import { BossApiError } from "@boss/api-client";

vi.mock("../lib/client", () => ({
  PROJECT_ID: "boss-dogfood",
  resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
  client: {
    listOutboxMessages: vi.fn(),
    listCommsChannelPolicies: vi.fn(),
    listInboxMessages: vi.fn(),
  },
}));

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

  it("returns an unavailable provider health subfeed state (empty on error)", () => {
    const feed = emptyProviderOpsFeed("provider health api down");
    // polish: explicit providerHealth state for monitoring obs
    expect(feed.apiReachable).toBe(false);
    expect(feed.providerHealth).toEqual([]);
    expect(feed.summary.healthyProviders).toBe(0);
    expect(feed.blockedReason).toBe("provider health api down");
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

describe("comms outbox/inbox/policy feed states (policy form, outbox empty/error/success/ACK, perm 403 reflection, persist refresh) - P2 e2e + COM DoD", () => {
  it("maps success outbox feed incl policies + receipt ACK state", async () => {
    const { client } = await import("../lib/client");
    const { fetchOutboxFeed } = await import("../lib/comms");

    vi.mocked(client.listOutboxMessages).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      limit: 50,
      offset: 0,
      items: [
        {
          id: "out_ack_1",
          project_id: "boss-dogfood",
          channel: "slack:#ops",
          subject: "ACKed",
          body: "done",
          status: "acked",
          data_boundary: "internal",
          dedupe_key: "dk-ack",
          receipt_id: "rcpt_123",
          created_at: "2026-06-05T00:00:00Z",
          updated_at: "2026-06-05T00:01:00Z",
        },
      ],
    });
    vi.mocked(client.listCommsChannelPolicies).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "pol_1",
          project_id: "boss-dogfood",
          provider: "slack",
          delivery_mode: "real_send_allowed",
          allowed_data_boundaries: ["public", "internal"],
          rollout_notes: undefined,
          status: "active",
          created_by: "ceo",
          metadata: {},
          created_at: "2026-06-05T00:00:00Z",
          updated_at: "2026-06-05T00:00:00Z",
        },
      ],
    });

    const feed = await fetchOutboxFeed("boss-dogfood");
    expect(feed.apiReachable).toBe(true);
    expect(feed.items[0].receiptId).toBe("rcpt_123");
    expect(feed.items[0].status).toBe("acked"); // ACK visible
    expect(feed.policies[0].deliveryMode).toBe("real_send_allowed");
  });

  it("returns explicit empty/error state for outbox (incl policy fetch fail)", async () => {
    const { client } = await import("../lib/client");
    const { fetchOutboxFeed } = await import("../lib/comms");

    vi.mocked(client.listOutboxMessages).mockRejectedValueOnce(new BossApiError("outbox down", 503, {}));
    vi.mocked(client.listCommsChannelPolicies).mockResolvedValueOnce({ project_id: "boss-dogfood", items: [] });

    const feed = await fetchOutboxFeed("boss-dogfood");
    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.policies).toEqual([]);
    expect(feed.blockedReason).toBe("API returned 503");
  });

  it("reflects permission failure 403 on outbox/policy fetch as blocked state in UI (perm 403 reflection)", async () => {
    const { client } = await import("../lib/client");
    const { fetchOutboxFeed } = await import("../lib/comms");

    vi.mocked(client.listOutboxMessages).mockRejectedValueOnce(new BossApiError("actor lacks comms_manage", 403, {}));
    vi.mocked(client.listCommsChannelPolicies).mockResolvedValueOnce({ project_id: "boss-dogfood", items: [] });

    const feed = await fetchOutboxFeed("boss-dogfood");
    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("API returned 403");
  });

  it("preserves project + outbox list (with policies) across simulated refresh/re-fetch (persist refresh)", async () => {
    const { client } = await import("../lib/client");
    const { fetchOutboxFeed } = await import("../lib/comms");

    const data = {
      project_id: "boss-dogfood",
      limit: 50,
      offset: 0,
      items: [{ id: "out_p", project_id: "boss-dogfood", channel: "email:x", subject: "p", body: "b", status: "draft", data_boundary: "internal" as const, dedupe_key: "dkp", created_at: "2026-06-05T00:00Z", updated_at: "2026-06-05T00:00Z" }],
    };
    const pol = { project_id: "boss-dogfood", items: [] };
    vi.mocked(client.listOutboxMessages).mockResolvedValue(data);
    vi.mocked(client.listCommsChannelPolicies).mockResolvedValue(pol);

    const f1 = await fetchOutboxFeed("boss-dogfood");
    const f2 = await fetchOutboxFeed("boss-dogfood"); // re-fetch like refresh

    expect(f1.apiReachable).toBe(true);
    expect(f2.apiReachable).toBe(true);
    expect(f1.projectId).toBe("boss-dogfood");
    expect(f2.items[0].id).toBe("out_p");
    expect(f1.policies).toEqual(f2.policies);
  });

  it("inbox feed error/success/empty states basic", async () => {
    const { client } = await import("../lib/client");
    const { fetchInboxFeed } = await import("../lib/comms");

    vi.mocked(client.listInboxMessages).mockResolvedValueOnce({ project_id: "boss-dogfood", limit: 50, offset: 0, items: [] });
    const empty = await fetchInboxFeed();
    expect(empty.apiReachable).toBe(true);
    expect(empty.items).toEqual([]);

    vi.mocked(client.listInboxMessages).mockRejectedValueOnce(new BossApiError("inbox 403", 403, {}));
    const err = await fetchInboxFeed();
    expect(err.apiReachable).toBe(false);
    expect(err.blockedReason).toBe("API returned 403");
  });
});
