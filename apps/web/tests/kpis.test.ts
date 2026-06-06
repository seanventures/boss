import { describe, expect, it, vi, beforeEach } from "vitest";

import { BossApiError } from "@boss/api-client";

// Mock next/cache revalidate for action tests (integrates with client toasts + router.refresh in forms)
vi.mock("next/cache", () => ({
  revalidatePath: vi.fn(),
}));

vi.mock("../lib/client", () => ({
  PROJECT_ID: "boss-dogfood",
  resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
  client: {
    listKpis: vi.fn(),
    createKpi: vi.fn(),
    proposeCommand: vi.fn(),
    createProviderAccount: vi.fn(),
    createSelfImprovementProposal: vi.fn(),
    listSelfImprovementProposals: vi.fn(),
    queueOutboxDelivery: vi.fn(),
    reserveBudget: vi.fn(),
    // ensure all used in action tests
  },
}));

describe("KPI feed", async () => {
  const { client } = await import("../lib/client");
  const { emptyKpiFeed, fetchKpiFeed } = await import("../lib/kpis");

  it("maps persisted KPI records into Mission Control items", async () => {
    vi.mocked(client.listKpis).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      limit: 50,
      offset: 0,
      items: [
        {
          id: "kpi_1",
          project_id: "boss-dogfood",
          kpi_key: "verified_work_throughput",
          name: "Verified Work Throughput",
          description: "Completed artifacts that pass required gates.",
          target_value: 10,
          current_value: 2,
          unit: "artifacts/week",
          status: "active",
          created_by: "ceo",
          created_at: "2026-06-05T00:00:00Z",
          updated_at: "2026-06-05T00:01:00Z",
        },
      ],
    });

    const feed = await fetchKpiFeed("boss-dogfood");

    expect(feed.apiReachable).toBe(true);
    expect(feed.items[0]).toMatchObject({
      id: "kpi_1",
      kpiKey: "verified_work_throughput",
      targetValue: 10,
      currentValue: 2,
    });
  });

  it("returns an explicit empty/error state when the KPI API is unavailable", async () => {
    vi.mocked(client.listKpis).mockRejectedValueOnce(
      new BossApiError("request failed", 503, {}),
    );

    const feed = await fetchKpiFeed("boss-dogfood");

    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("API returned 503");
  });

  it("builds a local empty state for inaccessible projects", () => {
    const feed = emptyKpiFeed("network down", "boss-dogfood");

    expect(feed.apiReachable).toBe(false);
    expect(feed.blockedReason).toBe("network down");
    expect(feed.items).toEqual([]);
  });

  it("reflects permission failure (403) for KPI list as blocked state in UI (GAP-AUTO-002 perm failure reflection)", async () => {
    vi.mocked(client.listKpis).mockRejectedValueOnce(
      new BossApiError("actor lacks pack_manage", 403, {}),
    );

    const feed = await fetchKpiFeed("boss-dogfood");

    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toBe("API returned 403");
  });

  it("preserves project context and KPI list across simulated page refresh/re-fetch (GAP-AUTO-002 refresh-persistence for project+KPI)", async () => {
    const kpiData = {
      project_id: "boss-dogfood",
      limit: 50,
      offset: 0,
      items: [
        {
          id: "kpi_persist",
          project_id: "boss-dogfood",
          kpi_key: "persist_kpi",
          name: "Persist Through Refresh",
          description: "should survive reload",
          target_value: 5,
          current_value: 1,
          unit: "x",
          status: "active",
          created_by: "ceo",
          created_at: "2026-06-05T00:00:00Z",
          updated_at: "2026-06-05T00:00:00Z",
        },
      ],
    };
    vi.mocked(client.listKpis).mockResolvedValueOnce(kpiData);

    const feed1 = await fetchKpiFeed("boss-dogfood");

    // simulate page refresh / re-mount with same project context
    vi.mocked(client.listKpis).mockResolvedValueOnce(kpiData);

    const feed2 = await fetchKpiFeed("boss-dogfood");

    expect(feed1.apiReachable).toBe(true);
    expect(feed1.items).toHaveLength(1);
    expect(feed1.items[0].id).toBe("kpi_persist");

    expect(feed2.apiReachable).toBe(true);
    expect(feed2.items[0].id).toBe("kpi_persist");

    // also empty for alternate project context (e.g. switched then refreshed)
    const feedOther = emptyKpiFeed("after refresh on switched project", "other-proj-123");
    expect(feedOther.blockedReason).toBe("after refresh on switched project");
  });
});

// Action tests for client feedback toasts (loading/success/error states + revalidate integration)
// Covers 5+ key actions per ux-review P2 + DoD + CAN-006 / BUD states + GAP-AUTO-001
describe("client action feedback (toasts + revalidate)", async () => {
  const { revalidatePath } = await import("next/cache");
  const { client } = await import("../lib/client");
  const {
    createKpiAction,
    submitCommandAction,
    createProviderAccountAction,
    submitSelfImprovementProposalAction,
    queueOutboxMessageAction,
  } = await import("../app/actions");

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("createKpiAction succeeds, calls revalidatePath, no throw (success toast path in client form)", async () => {
    vi.mocked(client.createKpi).mockResolvedValueOnce({
      id: "kpi_new",
      project_id: "boss-dogfood",
      kpi_key: "test_kpi",
      name: "Test",
      target_value: 5,
      current_value: 1,
      unit: "x",
      status: "active",
      created_by: "exec",
      created_at: "2026-06-05T00:00:00Z",
      updated_at: "2026-06-05T00:00:00Z",
    });

    const fd = new FormData();
    fd.set("project_id", "boss-dogfood");
    fd.set("actor", "exec");
    fd.set("kpi_key", "test_kpi");
    fd.set("name", "Test");
    fd.set("target_value", "5");
    fd.set("current_value", "1");
    fd.set("unit", "x");

    await expect(createKpiAction(fd)).resolves.toBeUndefined();
    expect(client.createKpi).toHaveBeenCalledTimes(1);
    expect(revalidatePath).toHaveBeenCalledWith("/");
  });

  it("createKpiAction throws on invalid (error banner + toast path)", async () => {
    const fd = new FormData();
    fd.set("project_id", "boss-dogfood");
    fd.set("actor", "exec");
    fd.set("kpi_key", "bad");
    fd.set("name", "");
    fd.set("target_value", "0"); // triggers <=0 validation
    fd.set("current_value", "0");
    fd.set("unit", "x");

    await expect(createKpiAction(fd)).rejects.toThrow("target_value must be greater than zero");
    expect(revalidatePath).not.toHaveBeenCalled();
  });

  it("submitCommandAction + queueOutbox (budget) call revalidate (command + budget feedback states)", async () => {
    vi.mocked(client.proposeCommand).mockResolvedValueOnce({ status: "proposed", task_id: "t1", job_id: "j_cmd", artifact_id: "a_cmd" } as any);
    vi.mocked(client.reserveBudget).mockResolvedValueOnce({ status: "reserved", reservation_id: "br1", budget_state: "green", message: "" } as any);
    vi.mocked(client.queueOutboxDelivery).mockResolvedValueOnce({ id: "j1", project_id: "boss-dogfood", kind: "outbox_delivery", status: "queued", data_boundary: "internal", priority: 0, max_attempts: 3, attempts: 0, created_at: "2026-06-05T00:00:00Z" } as any);

    const cmdFd = new FormData();
    cmdFd.set("project_id", "boss-dogfood");
    cmdFd.set("actor", "exec");
    cmdFd.set("command", "Do the thing");
    await submitCommandAction(cmdFd);
    expect(revalidatePath).toHaveBeenCalledWith("/");

    const qFd = new FormData();
    qFd.set("message_id", "m1");
    qFd.set("project_id", "boss-dogfood");
    qFd.set("actor", "exec");
    qFd.set("urgency", "normal");
    await queueOutboxMessageAction(qFd);
    expect(client.reserveBudget).toHaveBeenCalled();
    expect(revalidatePath).toHaveBeenCalledWith("/");
  });

  it("provider and self-imp actions succeed with revalidate (covers provider/self-imp toast states)", async () => {
    vi.mocked(client.createProviderAccount).mockResolvedValueOnce({ id: "pa1", project_id: "boss-dogfood", provider: "digitalocean", account_label: "sandbox", credential_ref: "env:DO", scopes: [], status: "active", read_only: true, deploy_capable: false, billing_readable: true, destructive_actions_disabled: true, capabilities: [], missing_permissions: [], created_by: "ceo", created_at: "2026-06-05T00:00:00Z", updated_at: "2026-06-05T00:00:00Z" } as any);
    vi.mocked(client.createSelfImprovementProposal).mockResolvedValueOnce({ id: "sip1", project_id: "boss-dogfood", task_id: "t_sip", title: "Improve", summary: "s", change_ref: "https://ex", diff_summary: "d", expected_impact: [], test_plan: [], canary_scope: [], rollback_plan: [], success_metrics: [], status: "proposed", risk_tier: 2, created_at: "2026-06-05T00:00:00Z", updated_at: "2026-06-05T00:00:00Z" } as any);

    const provFd = new FormData();
    provFd.set("project_id", "boss-dogfood");
    provFd.set("actor", "exec");
    provFd.set("provider", "digitalocean");
    provFd.set("account_label", "sandbox");
    await createProviderAccountAction(provFd);
    expect(revalidatePath).toHaveBeenCalledWith("/");

    const impFd = new FormData();
    impFd.set("project_id", "boss-dogfood");
    impFd.set("actor", "exec");
    impFd.set("title", "Improve");
    impFd.set("summary", "s");
    impFd.set("change_ref", "https://ex");
    impFd.set("diff_summary", "d");
    await submitSelfImprovementProposalAction(impFd);
    expect(revalidatePath).toHaveBeenCalledWith("/");
  });
});
