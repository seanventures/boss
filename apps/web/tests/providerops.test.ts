import { beforeEach, describe, expect, it, vi } from "vitest";

import { BossApiError } from "@boss/api-client";

vi.mock("../lib/client", () => ({
  PROJECT_ID: "boss-dogfood",
  resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
  client: {
    getEdition: vi.fn(),
    listProviderDefinitions: vi.fn(),
    listProviderAccounts: vi.fn(),
    listExternalResources: vi.fn(),
    listProviderCosts: vi.fn(),
    listProviderCostAnomalies: vi.fn(),
    listChangeSets: vi.fn(),
    listResourceReceipts: vi.fn(),
    listDeploymentTargets: vi.fn(),
    listDeploymentEnvironmentPolicies: vi.fn(),
    listDeploymentRuns: vi.fn(),
    listDeploymentPromotions: vi.fn(),
    listProviderHealth: vi.fn(),
    listDriftReports: vi.fn(),
    listReconciliationRuns: vi.fn(),
    listWorkerPools: vi.fn(),
    listScalePlans: vi.fn(),
  },
}));

describe("ProviderOps feed", async () => {
  const { client } = await import("../lib/client");
  const { describeSpendHonesty, fetchProviderOpsFeed } = await import(
    "../lib/providerops"
  );

  beforeEach(() => {
    vi.clearAllMocks();
  });

  function mockPaidEdition() {
    vi.mocked(client.getEdition).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      edition_id: "enterprise",
      display_name: "Enterprise",
      status: "active",
      account_connect_required: false,
      account_connect_status: "not_linked",
      local_auth_allowed: true,
      hosted_auth_required: false,
      connection_mode: "local",
      legal_review_required: false,
      included_capabilities: [],
      commercial_capabilities: [],
      source_pack: "enterprise_edition.yaml",
    } as never);
  }

  function mockCommunityEdition() {
    vi.mocked(client.getEdition).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      edition_id: "community",
      display_name: "Community",
      status: "active",
      account_connect_required: false,
      account_connect_status: "not_linked",
      local_auth_allowed: true,
      hosted_auth_required: false,
      connection_mode: "local",
      legal_review_required: false,
      included_capabilities: [],
      commercial_capabilities: [],
      source_pack: "community_edition.yaml",
    } as never);
  }

  function mockEmptySecondaryEndpoints() {
    vi.mocked(client.listExternalResources).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [],
      limit: 50,
      offset: 0,
    });
    vi.mocked(client.listProviderCosts).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [],
      limit: 50,
      offset: 0,
    });
    vi.mocked(client.listProviderCostAnomalies).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [],
      limit: 25,
      offset: 0,
    });
    vi.mocked(client.listResourceReceipts).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [],
      limit: 25,
      offset: 0,
    });
    vi.mocked(client.listDeploymentTargets).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [],
      limit: 25,
      offset: 0,
    });
    vi.mocked(client.listDeploymentEnvironmentPolicies).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [],
      limit: 25,
      offset: 0,
    });
    vi.mocked(client.listDeploymentRuns).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [],
    });
    vi.mocked(client.listDeploymentPromotions).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [],
    });
    vi.mocked(client.listProviderHealth).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [],
    });
    vi.mocked(client.listDriftReports).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [],
    });
    vi.mocked(client.listReconciliationRuns).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [],
    });
  }

  it("maps ProviderOps API payloads into dashboard summary data", async () => {
    mockPaidEdition();
    vi.mocked(client.listProviderDefinitions).mockResolvedValueOnce({
      items: [
        {
          provider: "aws",
          display_name: "Amazon Web Services",
          category: "infrastructure",
          adapter_status: "available",
          default_auth_method: "access_key",
          auth_methods: [
            {
              key: "access_key",
              runtime_status: "available",
              credential_slots: [
                {
                  key: "access_key_id",
                  secret_kind: "access_key_id",
                  required: true,
                },
                {
                  key: "secret_access_key",
                  secret_kind: "secret_access_key",
                  required: true,
                },
              ],
            },
          ],
          capabilities: ["resource.sync"],
        },
        {
          provider: "azure",
          display_name: "Microsoft Azure",
          category: "infrastructure",
          adapter_status: "planned",
          default_auth_method: "service_principal",
          auth_methods: [
            {
              key: "service_principal",
              runtime_status: "planned",
              credential_slots: [],
            },
          ],
          capabilities: [],
        },
      ],
    });
    vi.mocked(client.listProviderAccounts).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "pa_1",
          project_id: "boss-dogfood",
          provider: "cloudflare",
          account_label: "Cloudflare sandbox",
          auth_method: "api_token",
          credential_ref: "env:BOSS_CLOUDFLARE_API_TOKEN",
          credential_refs: { primary: "env:BOSS_CLOUDFLARE_API_TOKEN" },
          connection_metadata: {},
          scopes: ["read"],
          status: "active",
          credential_version: 1,
          read_only: true,
          deploy_capable: false,
          billing_readable: true,
          destructive_actions_disabled: true,
          capabilities: ["cloudflare.zone.list"],
          missing_permissions: [],
          last_test_status: "ok",
          last_tested_at: "2026-06-03T12:00:00Z",
          created_by: "ceo",
          created_at: "2026-06-03T11:00:00Z",
          updated_at: "2026-06-03T12:00:00Z",
        },
      ],
    });
    vi.mocked(client.listExternalResources).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "res_1",
          project_id: "boss-dogfood",
          provider_account_id: "pa_1",
          provider: "cloudflare",
          resource_type: "zone",
          external_id: "zone_123",
          name: "example.com",
          state: "active",
          ownership: "discovered_unmanaged",
          environment: "production",
          health_status: "ok",
          cost_confidence: "unknown",
          last_seen_at: "2026-06-03T12:00:00Z",
          tags: [],
          metadata: {},
          created_at: "2026-06-03T12:00:00Z",
          updated_at: "2026-06-03T12:00:00Z",
        },
      ],
      limit: 50,
      offset: 0,
    });
    vi.mocked(client.listProviderCosts).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "cost_1",
          project_id: "boss-dogfood",
          provider: "cloudflare",
          category: "dns",
          amount_usd_micros: 250_000,
          cost_layer: "observed",
          confidence: "provider_reported",
          metadata: {},
          created_at: "2026-06-03T12:00:00Z",
        },
      ],
      limit: 50,
      offset: 0,
    });
    vi.mocked(client.listProviderCostAnomalies).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "canom_1",
          project_id: "boss-dogfood",
          provider_account_id: "pa_1",
          cost_event_id: "cost_1",
          decision_id: "dec_1",
          provider: "cloudflare",
          anomaly_type: "provider_spend_freeze_threshold_crossed",
          severity: "orange",
          status: "open",
          amount_usd_micros: 250_000,
          threshold_usd_micros: 200_000,
          summary: "Cloudflare spend crossed the freeze threshold.",
          recommended_actions: [{ action: "provider_read_only_mode_applied" }],
          created_at: "2026-06-03T12:00:00Z",
        },
      ],
      limit: 50,
      offset: 0,
    });
    vi.mocked(client.listChangeSets).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "chg_1",
          project_id: "boss-dogfood",
          provider_account_id: "pa_1",
          title: "Upsert staging DNS",
          summary: "Create a staging record.",
          status: "pending_approval",
          risk_tier: 2,
          operations: [{ action: "cloudflare.dns.record.upsert" }],
          expected_cost_usd_micros: 1_000,
          cost_quote: {},
          rollback_plan: ["delete record"],
          expected_receipts: [],
          policy_result: {},
          created_by: "ceo",
          created_at: "2026-06-03T12:00:00Z",
          updated_at: "2026-06-03T12:00:00Z",
        },
      ],
    });
    vi.mocked(client.listResourceReceipts).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [],
      limit: 50,
      offset: 0,
    } as any);
    vi.mocked(client.listDeploymentTargets).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [],
      limit: 50,
      offset: 0,
    } as any);
    vi.mocked(client.listDeploymentEnvironmentPolicies).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "deppol_boss-dogfood_staging",
          project_id: "boss-dogfood",
          environment: "staging",
          runtime_mode: "staging_allowed",
          allowed_target_kinds: ["digitalocean_app"],
          requires_change_set: true,
          requires_budget_reservation: true,
          requires_approval: true,
          requires_health_check: false,
          requires_rollback_plan: true,
          rollout_notes: "Staging provider deployments enabled.",
          status: "active",
          created_by: "ceo",
          metadata: {},
          created_at: "2026-06-03T12:00:00Z",
          updated_at: "2026-06-03T12:00:00Z",
        },
      ],
      limit: 50,
      offset: 0,
    });
    vi.mocked(client.listDeploymentRuns).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "deprun_1",
          project_id: "boss-dogfood",
          deployment_target_id: "dt_1",
          changeset_id: "chg_1",
          release_artifact_id: "art_release",
          app_name: "BOSS",
          environment: "staging",
          repo: "seanheiney/boss",
          target_kind: "digitalocean_app",
          target_ref: "app",
          commit_ref: "abc123",
          status: "created",
          release_packet: {},
          health_check_results: [],
          rollback_plan: ["previous release"],
          release_receipt_ids: [],
          created_by: "ceo",
          created_at: "2026-06-03T12:00:00Z",
          updated_at: "2026-06-03T12:00:00Z",
        },
      ],
    });
    vi.mocked(client.listDeploymentPromotions).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "prom_1",
          project_id: "boss-dogfood",
          app_name: "BOSS",
          repo: "seanheiney/boss",
          commit_ref: "abc123",
          staging_target_id: "dt_staging",
          production_target_id: "dt_production",
          staging_deployment_run_id: "deprun_1",
          pipeline_provider: "github_actions",
          pipeline_descriptor: { configured: true },
          pipeline_verdict: { status: "passed" },
          gate_result: { status: "waiting" },
          status: "staging_planned",
          created_by: "release_manager",
          created_at: "2026-06-03T12:00:00Z",
          updated_at: "2026-06-03T12:00:00Z",
          staging_deployment_run: {
            id: "deprun_1",
            project_id: "boss-dogfood",
            deployment_target_id: "dt_1",
            app_name: "BOSS",
            environment: "staging",
            repo: "seanheiney/boss",
            target_kind: "digitalocean_app",
            target_ref: "app",
            status: "created",
            release_packet: {},
            health_check_results: [],
            rollback_plan: ["previous release"],
            release_receipt_ids: [],
            created_by: "ceo",
            created_at: "2026-06-03T12:00:00Z",
            updated_at: "2026-06-03T12:00:00Z",
          },
          timeline: [],
        },
      ],
    });
    vi.mocked(client.listProviderHealth).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "ph_1",
          project_id: "boss-dogfood",
          provider_account_id: "pa_1",
          provider: "cloudflare",
          status: "ok",
          api_available: true,
          rate_limit_status: "unknown",
          usage_cap_status: "unknown",
          last_checked_at: "2026-06-03T12:00:00Z",
          detail: {
            billing_status: "synced",
            billing_confidence: "provider_reported",
          },
          created_at: "2026-06-03T12:00:00Z",
        },
      ],
    });
    vi.mocked(client.listDriftReports).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "drift_1",
          project_id: "boss-dogfood",
          provider_account_id: "pa_1",
          classification: "cost_impacting",
          summary: "Unmanaged zone discovered.",
          unmanaged_count: 1,
          orphaned_count: 0,
          security_impacting_count: 0,
          production_impacting_count: 1,
          recommendations: [],
          created_at: "2026-06-03T12:00:00Z",
        },
      ],
    });
    vi.mocked(client.listReconciliationRuns).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "recon_1",
          project_id: "boss-dogfood",
          provider_account_id: "pa_1",
          provider: "cloudflare",
          status: "succeeded",
          resources_seen: 3,
          resources_changed: 1,
          costs_seen: 0,
          started_by: "ceo",
          started_at: "2026-06-03T12:00:00Z",
          finished_at: "2026-06-03T12:01:00Z",
          metadata: {
            inventory_snapshot_artifact_id: "art_providerops_snapshot_1",
            billing_status: "synced",
            billing_confidence: "provider_reported",
          },
        },
      ],
    });
    vi.mocked(client.listWorkerPools).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "wp_1",
          project_id: "boss-dogfood",
          pool_key: "local-default",
          kind: "local",
          status: "active",
          current_workers: 1,
          desired_workers: 2,
          min_workers: 0,
          max_workers: 3,
          max_queue_depth_per_worker: 5,
          cost_per_worker_hour_usd_micros: 0,
          capabilities: ["plan.command"],
          metadata: {},
          created_by: "ceo",
          created_at: "2026-06-03T12:00:00Z",
          updated_at: "2026-06-03T12:00:00Z",
        },
      ],
    });
    vi.mocked(client.listScalePlans).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "scp_1",
          project_id: "boss-dogfood",
          worker_pool_id: "wp_1",
          status: "applied",
          trigger_reason: "queue_pressure",
          queue_depth: 7,
          running_jobs: 0,
          leased_jobs: 0,
          retryable_jobs: 0,
          current_workers: 1,
          desired_workers_before: 1,
          desired_workers_after: 2,
          target_workers: 2,
          budget_state: "green",
          budget_available_usd_micros: 10_000_000,
          estimated_hourly_delta_usd_micros: 0,
          provider_health_status: "unknown",
          actions: [{ action: "scale_local_workers" }],
          recommendations: [],
          created_by: "ceo",
          created_at: "2026-06-03T12:00:00Z",
        },
      ],
    });

    const feed = await fetchProviderOpsFeed();

    expect(feed.apiReachable).toBe(true);
    expect(feed.partialFailures).toEqual([]);
    expect(feed.editionLimitations).toEqual([]);
    expect(feed.paidWriteAutomationAvailable).toBe(true);
    expect(feed.editionId).toBe("enterprise");
    expect(feed.providerDefinitions).toEqual([
      expect.objectContaining({
        provider: "aws",
        adapterStatus: "available",
        authMethods: [
          expect.objectContaining({
            key: "access_key",
            credentialSlots: [
              expect.objectContaining({ key: "access_key_id", required: true }),
              expect.objectContaining({
                key: "secret_access_key",
                required: true,
              }),
            ],
          }),
        ],
      }),
      expect.objectContaining({ provider: "azure", adapterStatus: "planned" }),
    ]);
    expect(feed.summary).toMatchObject({
      connectedProviders: 1,
      healthyProviders: 1,
      openChangeSets: 1,
      resourceCount: 1,
      unmanagedResourceCount: 1,
      spendUsdMicros: 250_000,
      openCostAnomalies: 1,
      desiredWorkerCount: 2,
      openScalePlans: 0,
      activeDeploymentRuns: 1,
    });
    expect(feed.providerAccounts[0]).toMatchObject({
      accountLabel: "Cloudflare sandbox",
      credentialRef: "env:BOSS_CLOUDFLARE_API_TOKEN",
      destructiveActionsDisabled: true,
    });
    expect(feed.workerPools[0]).toMatchObject({
      poolKey: "local-default",
      desiredWorkers: 2,
    });
    expect(feed.costAnomalies[0]).toMatchObject({
      provider: "cloudflare",
      severity: "orange",
      decisionId: "dec_1",
    });
    expect(feed.scalePlans[0]).toMatchObject({
      triggerReason: "queue_pressure",
      queueDepth: 7,
    });
    expect(feed.providerHealth[0]).toMatchObject({
      provider: "cloudflare",
      billingStatus: "synced",
      billingConfidence: "provider_reported",
    });
    expect(feed.reconciliationRuns[0]).toMatchObject({
      provider: "cloudflare",
      inventorySnapshotArtifactId: "art_providerops_snapshot_1",
      billingStatus: "synced",
      billingConfidence: "provider_reported",
    });
    expect(feed.deploymentRuns[0]).toMatchObject({
      appName: "BOSS",
      changesetId: "chg_1",
    });
    expect(feed.deploymentPolicies[0]).toMatchObject({
      environment: "staging",
      runtimeMode: "staging_allowed",
      requiresBudgetReservation: true,
    });
    expect(feed.deploymentPromotions[0]).toMatchObject({
      appName: "BOSS",
      pipelineStatus: "passed",
      gateStatus: "waiting",
      stagingRunStatus: "created",
    });
  });

  it("maps billing unavailable/unknown without inventing cost events", async () => {
    mockPaidEdition();
    vi.mocked(client.listProviderDefinitions).mockResolvedValueOnce({ items: [] });
    vi.mocked(client.listProviderAccounts).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [],
    });
    mockEmptySecondaryEndpoints();
    vi.mocked(client.listProviderHealth).mockReset();
    vi.mocked(client.listReconciliationRuns).mockReset();
    vi.mocked(client.listProviderHealth).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "ph_cf",
          project_id: "boss-dogfood",
          provider: "cloudflare",
          status: "healthy",
          api_available: true,
          rate_limit_status: "ok",
          usage_cap_status: "ok",
          last_checked_at: "2026-08-13T00:00:00Z",
          detail: {
            billing_status: "unavailable",
            billing_confidence: "unknown",
            billing_message: "Exact billing sync is not available for cloudflare",
          },
          created_at: "2026-08-13T00:00:00Z",
        },
      ],
    });
    vi.mocked(client.listReconciliationRuns).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [
        {
          id: "recon_cf",
          project_id: "boss-dogfood",
          provider: "cloudflare",
          status: "succeeded",
          resources_seen: 4,
          resources_changed: 0,
          costs_seen: 0,
          started_by: "ceo",
          started_at: "2026-08-13T00:00:00Z",
          metadata: {
            billing_status: "unavailable",
            billing_message: "Exact billing sync is not available for cloudflare",
          },
        },
      ],
    });
    vi.mocked(client.listChangeSets).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [],
    });
    vi.mocked(client.listWorkerPools).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [],
    });
    vi.mocked(client.listScalePlans).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [],
    });

    const feed = await fetchProviderOpsFeed();
    expect(feed.costs).toEqual([]);
    expect(feed.summary.spendUsdMicros).toBe(0);
    expect(feed.providerHealth[0]).toMatchObject({
      billingStatus: "unavailable",
      billingConfidence: "unknown",
    });
    expect(feed.reconciliationRuns[0]).toMatchObject({
      costsSeen: 0,
      billingStatus: "unavailable",
    });
    expect(
      describeSpendHonesty({
        costs: feed.costs,
        health: feed.providerHealth,
        runs: feed.reconciliationRuns,
      }),
    ).toMatchObject({
      kind: "unavailable",
      amountUsdMicros: null,
      label: "Billing unavailable",
    });
  });

  it("preserves a partial provider workspace when a secondary endpoint fails", async () => {
    mockPaidEdition();
    vi.mocked(client.listProviderDefinitions).mockResolvedValueOnce({ items: [] });
    vi.mocked(client.listProviderAccounts).mockRejectedValueOnce(
      new BossApiError("request failed", 503, {}),
    );
    vi.mocked(client.listExternalResources).mockRejectedValueOnce(
      new BossApiError("request failed", 503, {}),
    );
    vi.mocked(client.listProviderCosts).mockRejectedValueOnce(new Error("costs down"));
    vi.mocked(client.listProviderCostAnomalies).mockRejectedValueOnce(
      new Error("anomalies down"),
    );
    vi.mocked(client.listChangeSets).mockRejectedValueOnce(new Error("changes down"));
    vi.mocked(client.listResourceReceipts).mockRejectedValueOnce(
      new Error("receipts down"),
    );
    vi.mocked(client.listDeploymentTargets).mockRejectedValueOnce(
      new Error("targets down"),
    );
    vi.mocked(client.listDeploymentEnvironmentPolicies).mockRejectedValueOnce(
      new Error("policies down"),
    );
    vi.mocked(client.listDeploymentRuns).mockRejectedValueOnce(
      new Error("deployments down"),
    );
    vi.mocked(client.listDeploymentPromotions).mockRejectedValueOnce(
      new Error("promotions down"),
    );
    vi.mocked(client.listProviderHealth).mockRejectedValueOnce(
      new Error("health down"),
    );
    vi.mocked(client.listDriftReports).mockRejectedValueOnce(
      new Error("drift down"),
    );
    vi.mocked(client.listReconciliationRuns).mockRejectedValueOnce(
      new Error("reconciliation down"),
    );
    vi.mocked(client.listWorkerPools).mockRejectedValueOnce(
      new Error("pools down"),
    );
    vi.mocked(client.listScalePlans).mockRejectedValueOnce(
      new Error("scale plans down"),
    );

    const feed = await fetchProviderOpsFeed();

    expect(feed.apiReachable).toBe(true);
    expect(feed.projectId).toBe("boss-dogfood");
    expect(feed.summary.connectedProviders).toBe(0);
    expect(feed.partialFailures).toContain("Provider accounts: HTTP 503");
    expect(feed.partialFailures).toContain("Cloud costs: costs down");
    expect(feed.partialFailures).toContain("ChangeSets: changes down");
    expect(feed.partialFailures).toContain("Worker pools: pools down");
  });

  it("returns unavailable only when both configuration endpoints fail", async () => {
    mockPaidEdition();
    vi.mocked(client.listProviderDefinitions).mockRejectedValueOnce(
      new BossApiError("request failed", 503, {}),
    );
    vi.mocked(client.listProviderAccounts).mockRejectedValueOnce(
      new BossApiError("request failed", 503, {}),
    );
    mockEmptySecondaryEndpoints();
    vi.mocked(client.listChangeSets).mockRejectedValueOnce(
      new BossApiError("not found", 404, {}),
    );
    vi.mocked(client.listWorkerPools).mockRejectedValueOnce(
      new BossApiError("not found", 404, {}),
    );
    vi.mocked(client.listScalePlans).mockRejectedValueOnce(
      new BossApiError("not found", 404, {}),
    );

    const feed = await fetchProviderOpsFeed();

    expect(feed.apiReachable).toBe(false);
    expect(feed.blockedReason).toContain("Provider catalog: HTTP 503");
    expect(feed.blockedReason).toContain("Provider accounts: HTTP 503");
  });

  it("loads full ProviderOps kernel on community (BYOK + self-managed infra)", async () => {
    mockCommunityEdition();
    vi.mocked(client.listProviderDefinitions).mockResolvedValueOnce({
      items: [
        {
          provider: "openai",
          display_name: "OpenAI",
          category: "model",
          adapter_status: "available",
          default_auth_method: "api_key",
          auth_methods: [
            {
              key: "api_key",
              runtime_status: "available",
              credential_slots: [
                { key: "api_key", secret_kind: "api_key", required: true },
              ],
            },
          ],
          capabilities: ["model.invoke"],
        },
      ],
    });
    vi.mocked(client.listProviderAccounts).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [],
    });
    mockEmptySecondaryEndpoints();
    vi.mocked(client.listChangeSets).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [],
    });
    vi.mocked(client.listWorkerPools).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [],
    });
    vi.mocked(client.listScalePlans).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [],
    });

    const feed = await fetchProviderOpsFeed();

    expect(feed.apiReachable).toBe(true);
    expect(feed.editionId).toBe("community");
    expect(feed.paidWriteAutomationAvailable).toBe(true);
    expect(feed.partialFailures).toEqual([]);
    expect(feed.editionLimitations).toEqual([]);
    expect(feed.providerDefinitions).toHaveLength(1);
    expect(client.listChangeSets).toHaveBeenCalled();
    expect(client.listWorkerPools).toHaveBeenCalled();
    expect(client.listScalePlans).toHaveBeenCalled();
  });

  it("treats ChangeSet/pool/scale failures as partialFailures, not edition blocks", async () => {
    mockCommunityEdition();
    vi.mocked(client.listProviderDefinitions).mockResolvedValueOnce({
      items: [
        {
          provider: "aws",
          display_name: "Amazon Web Services",
          category: "infrastructure",
          adapter_status: "available",
          default_auth_method: "access_key",
          auth_methods: [],
          capabilities: [],
        },
      ],
    });
    vi.mocked(client.listProviderAccounts).mockResolvedValueOnce({
      project_id: "boss-dogfood",
      items: [],
    });
    mockEmptySecondaryEndpoints();
    vi.mocked(client.listChangeSets).mockRejectedValueOnce(
      new BossApiError("upstream", 503, {}),
    );
    vi.mocked(client.listWorkerPools).mockRejectedValueOnce(
      new BossApiError("upstream", 503, {}),
    );
    vi.mocked(client.listScalePlans).mockRejectedValueOnce(
      new BossApiError("upstream", 503, {}),
    );

    const feed = await fetchProviderOpsFeed();

    expect(feed.apiReachable).toBe(true);
    expect(feed.paidWriteAutomationAvailable).toBe(true);
    expect(feed.editionLimitations).toEqual([]);
    expect(feed.partialFailures).toContain("ChangeSets: HTTP 503");
    expect(feed.partialFailures).toContain("Worker pools: HTTP 503");
    expect(feed.partialFailures).toContain("Scale plans: HTTP 503");
    expect(feed.providerDefinitions[0]?.provider).toBe("aws");
  });
});
