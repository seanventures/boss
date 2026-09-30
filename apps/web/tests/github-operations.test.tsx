import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/client", () => ({
  API_URL: "http://127.0.0.1:8000",
  API_KEY: undefined,
  PROJECT_ID: "boss-dogfood",
  authHeaders: () => ({ "Content-Type": "application/json" }),
  resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
  client: {
    listGitHubConnections: vi.fn(),
    listGitHubRepositoryBindings: vi.fn(),
    listGitHubBranchPolicies: vi.fn(),
    listGitHubBranchSnapshots: vi.fn(),
    listGitHubBranchLeases: vi.fn(),
    listGitHubCodeSyncRuns: vi.fn(),
    listGitHubConflictSets: vi.fn(),
    listGitHubResolutionPlans: vi.fn(),
    listGitHubMergeAuthorizations: vi.fn(),
    listGitHubReconciliationRuns: vi.fn(),
    listGitHubReconciliationSignals: vi.fn(),
    listGitHubRepositoryRefStates: vi.fn(),
  },
}));

describe("GitHub Mission Control", async () => {
  const { client } = await import("../lib/client");
  const {
    emptyGitHubOperationsFeed,
    fetchGitHubOperationsFeed,
  } = await import("../lib/github-operations");
  const { GitHubOperations } = await import("../app/github-operations-ui");

  const response = (items: unknown[]) => ({
    project_id: "boss-dogfood",
    items,
  });

  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(client.listGitHubConnections).mockResolvedValue(
      response([]) as never,
    );
    vi.mocked(client.listGitHubRepositoryBindings).mockResolvedValue(
      response([]) as never,
    );
    vi.mocked(client.listGitHubBranchPolicies).mockResolvedValue(
      response([]) as never,
    );
    vi.mocked(client.listGitHubBranchSnapshots).mockResolvedValue(
      response([]) as never,
    );
    vi.mocked(client.listGitHubBranchLeases).mockResolvedValue(
      response([]) as never,
    );
    vi.mocked(client.listGitHubCodeSyncRuns).mockResolvedValue(
      response([]) as never,
    );
    vi.mocked(client.listGitHubConflictSets).mockResolvedValue(
      response([]) as never,
    );
    vi.mocked(client.listGitHubResolutionPlans).mockResolvedValue(
      response([]) as never,
    );
    vi.mocked(client.listGitHubMergeAuthorizations).mockResolvedValue(
      response([]) as never,
    );
    vi.mocked(client.listGitHubReconciliationRuns).mockResolvedValue(
      response([]) as never,
    );
    vi.mocked(client.listGitHubReconciliationSignals).mockResolvedValue(
      response([]) as never,
    );
    vi.mocked(client.listGitHubRepositoryRefStates).mockResolvedValue(
      response([]) as never,
    );
  });

  it("summarizes canonical repository, branch, lease, conflict, merge, and reconciliation state", async () => {
    vi.mocked(client.listGitHubConnections).mockResolvedValue(
      response([{ id: "conn_1", status: "active" }]) as never,
    );
    vi.mocked(client.listGitHubRepositoryBindings).mockResolvedValue(
      response([{ id: "bind_1", status: "active", archived: false }]) as never,
    );
    vi.mocked(client.listGitHubBranchSnapshots).mockResolvedValue(
      response([
        {
          id: "snap_1",
          classification: "diverged",
          observed_at: "2026-07-26T03:00:00Z",
        },
      ]) as never,
    );
    vi.mocked(client.listGitHubBranchLeases).mockResolvedValue(
      response([{ id: "lease_1", status: "active" }]) as never,
    );
    vi.mocked(client.listGitHubConflictSets).mockResolvedValue(
      response([{ id: "conflict_1", status: "open" }]) as never,
    );
    vi.mocked(client.listGitHubMergeAuthorizations).mockResolvedValue(
      response([{ id: "merge_1", status: "pending_approval" }]) as never,
    );
    vi.mocked(client.listGitHubReconciliationRuns).mockResolvedValue(
      response([
        {
          id: "recon_1",
          status: "failed",
          updated_at: "2026-07-26T04:00:00Z",
        },
      ]) as never,
    );
    vi.mocked(client.listGitHubRepositoryRefStates).mockResolvedValue(
      response([
        {
          id: "ref_1",
          protected: true,
          observed_at: "2026-07-26T02:00:00Z",
        },
      ]) as never,
    );

    const feed = await fetchGitHubOperationsFeed("boss-dogfood");

    expect(feed.apiReachable).toBe(true);
    expect(feed.summary).toEqual({
      activeConnections: 1,
      activeRepositories: 1,
      protectedRefs: 1,
      activeLeases: 1,
      divergentSnapshots: 1,
      openConflicts: 1,
      pendingMergeAuthorizations: 1,
      reconciliationAttention: 1,
      lastObservedAt: "2026-07-26T04:00:00Z",
    });
  });

  it("keeps healthy streams visible when one evidence endpoint fails", async () => {
    vi.mocked(client.listGitHubReconciliationSignals).mockRejectedValue(
      new Error("signal store unavailable"),
    );
    vi.mocked(client.listGitHubRepositoryBindings).mockResolvedValue(
      response([{ id: "bind_1", status: "active", archived: false }]) as never,
    );

    const feed = await fetchGitHubOperationsFeed("boss-dogfood");

    expect(feed.apiReachable).toBe(true);
    expect(feed.bindings).toHaveLength(1);
    expect(feed.signals).toEqual([]);
    expect(feed.warnings).toEqual([
      "Signals: signal store unavailable",
    ]);
    expect(feed.blockedReason).toBe("Some GitHub evidence is unavailable.");
  });

  it("renders branch evidence, governed workflow state, receipts, and permission-aware controls", () => {
    const feed = {
      ...emptyGitHubOperationsFeed(undefined, "boss-dogfood"),
      apiReachable: true,
      connections: [
        {
          id: "conn_1",
          project_id: "boss-dogfood",
          connection_label: "BOSS GitHub App",
          auth_method: "github_app" as const,
          credential_configured: true,
          api_base_url: "https://api.github.com",
          status: "active" as const,
          required_permissions: ["contents:read"],
          granted_permissions: ["contents:read"],
          missing_permissions: [],
          metadata: {},
          last_test_status: "passed",
          created_by: "ceo",
          created_at: "2026-07-26T00:00:00Z",
          updated_at: "2026-07-26T00:00:00Z",
        },
      ],
      bindings: [
        {
          id: "bind_1",
          project_id: "boss-dogfood",
          connection_id: "conn_1",
          owner: "example-org",
          name: "boss",
          full_name: "example-org/boss",
          role: "primary" as const,
          default_branch: "main",
          visibility: "private",
          archived: false,
          status: "active" as const,
          permissions: { push: true },
          capabilities: ["contents"],
          metadata: {},
          created_by: "ceo",
          created_at: "2026-07-26T00:00:00Z",
          updated_at: "2026-07-26T00:00:00Z",
        },
      ],
      refStates: [
        {
          id: "ref_1",
          project_id: "boss-dogfood",
          repository_binding_id: "bind_1",
          ref_name: "main",
          exists: true,
          commit_sha: "1234567890abcdef",
          tree_sha: "tree1234567890",
          protected: true,
          protection: {},
          latest_observation_id: "observation_1",
          latest_signal_id: "signal_1",
          latest_receipt_id: "receipt_1",
          observed_at: "2026-07-26T02:00:00Z",
          created_at: "2026-07-26T02:00:00Z",
          updated_at: "2026-07-26T02:00:00Z",
        },
      ],
      syncRuns: [
        {
          id: "sync_1",
          repository_binding_id: "bind_1",
          source_ref: "ai/codex/boss-255",
          target_ref: "main",
          status: "planned",
          action: "fast_forward_target",
          receipt_ids: ["receipt_sync"],
        },
      ] as never,
      conflicts: [
        {
          id: "conflict_1",
          repository_binding_id: "bind_1",
          resolution_branch: "ai/codex/resolve-main",
          conflict_count: 2,
          status: "open",
        },
      ] as never,
      mergeAuthorizations: [
        {
          id: "merge_1",
          repository_binding_id: "bind_1",
          pull_request_number: 87,
          pull_request_url: "https://github.com/example-org/boss/pull/87",
          source_ref: "ai/codex/boss-255",
          target_ref: "main",
          status: "pending_approval",
          observed_checks: [{ name: "test" }],
          required_checks: ["test"],
          observed_reviews: [],
          required_approvals: 1,
          changeset_id: "changeset_1",
          receipt_ids: ["receipt_merge"],
          rollback_plan: { ref: "refs/boss/rollback/87" },
        },
      ] as never,
      reconciliationRuns: [
        {
          id: "recon_1",
          repository_binding_id: "bind_1",
          source: "poll",
          status: "succeeded",
          observation_ids: ["observation_1"],
          attempt_count: 1,
          max_attempts: 4,
          receipt_ids: ["receipt_recon"],
        },
      ] as never,
      summary: {
        activeConnections: 1,
        activeRepositories: 1,
        protectedRefs: 1,
        activeLeases: 0,
        divergentSnapshots: 0,
        openConflicts: 1,
        pendingMergeAuthorizations: 1,
        reconciliationAttention: 0,
        lastObservedAt: "2026-07-26T02:00:00Z",
      },
    };

    const managedHtml = renderToStaticMarkup(
      <GitHubOperations feed={feed} actor="ceo" canManage />,
    );
    const readOnlyHtml = renderToStaticMarkup(
      <GitHubOperations feed={feed} actor="auditor" canManage={false} />,
    );

    expect(managedHtml).toContain("GitHub Mission Control");
    expect(managedHtml).toContain("example-org/boss");
    expect(managedHtml).toContain("12345678");
    expect(managedHtml).toContain("Governed delivery flow");
    expect(managedHtml).toContain("Conflict resolution");
    expect(managedHtml).toContain("Authorize");
    expect(managedHtml).toContain("Receipts linked");
    expect(managedHtml).toContain("Reconcile now");
    expect(managedHtml).toContain("Easy connect");
    expect(managedHtml).toContain("env:BOSS_GITHUB_TOKEN");
    expect(managedHtml).toContain("Link repository to project");
    expect(readOnlyHtml).toContain("Read-only view");
    expect(readOnlyHtml).toContain("github_manage");
    expect(readOnlyHtml).not.toContain("Reconcile now");
    expect(readOnlyHtml).not.toContain("github-easy-connect");
  });

  it("surfaces easy connect CTA when the project has no GitHub connection", () => {
    const feed = {
      ...emptyGitHubOperationsFeed(undefined, "boss-dogfood"),
      apiReachable: true,
      connections: [],
      bindings: [],
      summary: {
        activeConnections: 0,
        activeRepositories: 0,
        protectedRefs: 0,
        activeLeases: 0,
        divergentSnapshots: 0,
        openConflicts: 0,
        pendingMergeAuthorizations: 0,
        reconciliationAttention: 0,
      },
    };
    const html = renderToStaticMarkup(
      <GitHubOperations feed={feed} actor="ceo" canManage />,
    );
    expect(html).toContain("github-easy-connect");
    expect(html).toContain("Connect GitHub to this project");
    expect(html).toContain("BOSS_GITHUB_TOKEN");
    expect(html).toContain("Connect GitHub");
  });
});
