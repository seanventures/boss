import { afterEach, describe, expect, it, vi } from "vitest";

import { BossApiError } from "@boss/api-client";

const fetchMock = vi.fn();
vi.stubGlobal("fetch", fetchMock);

vi.mock("../lib/client", () => ({
  PROJECT_ID: "boss-dogfood",
  API_URL: "http://127.0.0.1:8000",
  authHeaders: (extra?: Record<string, string>) => ({
    Authorization: "Bearer test",
    ...(extra ?? {}),
  }),
  resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
  client: {
    getEdition: vi.fn(),
    listSelfImprovementProposals: vi.fn(),
  },
}));

describe("self-improvement feed honesty", async () => {
  const { client } = await import("../lib/client");
  const {
    emptySelfImprovementFeed,
    fetchSelfImprovementFeed,
    isPaidSelfImprovementEdition,
    selfImprovementUnavailableReason,
  } = await import("../lib/self-improvement");

  afterEach(() => {
    vi.mocked(client.getEdition).mockReset();
    vi.mocked(client.listSelfImprovementProposals).mockReset();
    fetchMock.mockReset();
  });

  it("marks empty feed as surface-unavailable with no silent CTAs", () => {
    const feed = emptySelfImprovementFeed("self improvement api down");

    expect(feed.apiReachable).toBe(false);
    expect(feed.surfaceAvailable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.improvementPrograms).toEqual([]);
    expect(feed.blockedReason).toBe("self improvement api down");
  });

  it("classifies paid vs community editions for SIM gating", () => {
    expect(isPaidSelfImprovementEdition("community")).toBe(false);
    expect(isPaidSelfImprovementEdition(undefined)).toBe(false);
    expect(isPaidSelfImprovementEdition("cloud")).toBe(true);
    expect(isPaidSelfImprovementEdition("enterprise")).toBe(true);
    expect(isPaidSelfImprovementEdition("enterprise_self_hosted")).toBe(true);
  });

  it("builds enterprise-only copy for community + 404 (not a bare status code)", () => {
    const { reason, enterpriseOnly } = selfImprovementUnavailableReason({
      editionId: "community",
      status: 404,
    });

    expect(enterpriseOnly).toBe(true);
    expect(reason).toMatch(/Enterprise/i);
    expect(reason).toMatch(/community/i);
    expect(reason).not.toBe("API returned 404");
  });

  it("hides SIM surface on community when proposals 404 and lists improvement programs", async () => {
    vi.mocked(client.getEdition).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      edition_id: "community",
      display_name: "Community",
      status: "active",
      account_connect_required: false,
      account_connect_status: "not_required",
      local_auth_allowed: true,
      hosted_auth_required: false,
      connection_mode: "local",
      legal_review_required: false,
      included_capabilities: [],
      commercial_capabilities: [],
      source_pack: "community_edition.yaml",
    });
    vi.mocked(client.listSelfImprovementProposals).mockRejectedValueOnce(
      new BossApiError("not found", 404, { detail: "Not Found" }),
    );
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        project_id: "boss-dogfood",
        items: [
          {
            id: "imp_prog_1",
            name: "routing-baseline",
            objective: "Hold routing quality",
            status: "active",
            baseline_evaluation_id: "eval_1",
            updated_at: "2026-08-11T00:00:00Z",
          },
        ],
      }),
    });

    const feed = await fetchSelfImprovementFeed("boss-dogfood");

    expect(feed.surfaceAvailable).toBe(false);
    expect(feed.apiReachable).toBe(false);
    expect(feed.enterpriseOnly).toBe(true);
    expect(feed.editionId).toBe("community");
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toMatch(/Enterprise/i);
    expect(feed.blockedReason).toMatch(/hidden/i);
    expect(feed.programsApiReachable).toBe(true);
    expect(feed.improvementPrograms).toEqual([
      {
        id: "imp_prog_1",
        name: "routing-baseline",
        objective: "Hold routing quality",
        status: "active",
        baselineEvaluationId: "eval_1",
        updatedAt: "2026-08-11T00:00:00Z",
      },
    ]);
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/v1/improvement-programs?project_id=boss-dogfood"),
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer test" }),
      }),
    );
  });

  it("exposes SIM proposals when enterprise edition mounts the surface", async () => {
    vi.mocked(client.getEdition).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      edition_id: "enterprise_self_hosted",
      display_name: "Enterprise self-hosted",
      status: "active",
      account_connect_required: false,
      account_connect_status: "not_required",
      local_auth_allowed: true,
      hosted_auth_required: false,
      connection_mode: "local",
      legal_review_required: false,
      included_capabilities: [],
      commercial_capabilities: [],
      source_pack: "enterprise_edition.yaml",
    });
    vi.mocked(client.listSelfImprovementProposals).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      limit: 25,
      offset: 0,
      items: [
        {
          id: "imp_1",
          project_id: "boss-dogfood",
          task_id: "t1",
          title: "Tighten expiry",
          summary: "s",
          change_ref: "https://example.com/pr/1",
          diff_summary: "d",
          expected_impact: [],
          test_plan: [],
          canary_scope: [],
          rollback_plan: [],
          success_metrics: [],
          status: "proposed",
          risk_tier: 2,
          created_by: "ceo",
          created_at: "2026-08-11T00:00:00Z",
          updated_at: "2026-08-11T00:00:00Z",
        },
      ],
    } as never);
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ project_id: "boss-dogfood", items: [] }),
    });

    const feed = await fetchSelfImprovementFeed();

    expect(feed.surfaceAvailable).toBe(true);
    expect(feed.apiReachable).toBe(true);
    expect(feed.enterpriseOnly).toBeUndefined();
    expect(feed.editionId).toBe("enterprise_self_hosted");
    expect(feed.items).toHaveLength(1);
    expect(feed.items[0]).toMatchObject({
      id: "imp_1",
      title: "Tighten expiry",
      status: "proposed",
    });
  });

  it("surfaces non-404 SIM failures without pretending the feature is enterprise-only", async () => {
    vi.mocked(client.getEdition).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      edition_id: "enterprise",
      display_name: "Enterprise",
      status: "active",
      account_connect_required: false,
      account_connect_status: "not_required",
      local_auth_allowed: true,
      hosted_auth_required: false,
      connection_mode: "local",
      legal_review_required: false,
      included_capabilities: [],
      commercial_capabilities: [],
      source_pack: "enterprise_edition.yaml",
    });
    vi.mocked(client.listSelfImprovementProposals).mockRejectedValueOnce(
      new BossApiError("upstream down", 503, {}),
    );
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 503,
      json: async () => ({}),
    });

    const feed = await fetchSelfImprovementFeed();

    expect(feed.surfaceAvailable).toBe(false);
    expect(feed.enterpriseOnly).toBe(false);
    expect(feed.blockedReason).toBe("Self-improvement API returned 503.");
    expect(feed.programsApiReachable).toBe(false);
  });

  it("still hides CTAs when edition probe fails but SIM returns 404", async () => {
    vi.mocked(client.getEdition).mockRejectedValueOnce(new Error("edition offline"));
    vi.mocked(client.listSelfImprovementProposals).mockRejectedValueOnce(
      new BossApiError("not found", 404, {}),
    );
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ project_id: "boss-dogfood", items: [] }),
    });

    const feed = await fetchSelfImprovementFeed();

    expect(feed.surfaceAvailable).toBe(false);
    expect(feed.enterpriseOnly).toBe(true);
    expect(feed.blockedReason).toMatch(/404|not mounted|Enterprise/i);
    expect(feed.programsApiReachable).toBe(true);
    expect(feed.improvementPrograms).toEqual([]);
  });
});
