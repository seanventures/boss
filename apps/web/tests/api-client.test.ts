import { describe, expect, it } from "vitest";

import { createBossApiClient } from "@boss/api-client";

describe("createBossApiClient", () => {
  it("sends bearer API key headers when configured", async () => {
    const calls: RequestInit[] = [];
    const fetchImpl = async (_input: RequestInfo | URL, init?: RequestInit) => {
      calls.push(init ?? {});
      return new Response(JSON.stringify({ items: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    };
    const client = createBossApiClient({
      baseUrl: "http://127.0.0.1:8000",
      apiKey: "client-secret",
      fetchImpl,
    });

    await client.listProjects();

    expect(calls).toHaveLength(1);
    expect(calls[0].headers).toMatchObject({
      Authorization: "Bearer client-secret",
    });
  });

  it("fetches auth session state through the API contract", async () => {
    const urls: string[] = [];
    const fetchImpl = async (input: RequestInfo | URL) => {
      urls.push(String(input));
      return new Response(
        JSON.stringify({
          auth_required: true,
          transport_authenticated: true,
          actor_binding_required: true,
          principal_member_id: "ceo",
          auth_method: "api_key",
          oidc_enabled: false,
          message: "Authenticated as project member 'ceo'.",
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      );
    };
    const client = createBossApiClient({
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl,
    });

    const session = await client.getAuthSession();

    expect(urls).toEqual(["http://127.0.0.1:8000/v1/auth/session"]);
    expect(session.principal_member_id).toBe("ceo");
    expect(session.actor_binding_required).toBe(true);
  });

  it("manages org members through the API contract", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const orgMember = {
      id: "om_org_boss_delegate",
      org_id: "org_boss",
      project_id: "boss-dogfood",
      member_id: "org_delegate",
      display_name: "Org Delegate",
      member_type: "human",
      role_keys: ["operator"],
      permissions: ["command_submit"],
      status: "active",
      created_by: "ceo",
      created_at: "2026-06-03T00:00:00Z",
      updated_at: "2026-06-03T00:00:00Z",
    };
    const responses = [
      { status: 200, body: { org_id: "org_boss", items: [orgMember] } },
      { status: 201, body: orgMember },
    ];
    const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      const response = responses.shift();
      return new Response(JSON.stringify(response?.body ?? {}), {
        status: response?.status ?? 500,
        headers: { "Content-Type": "application/json" },
      });
    };
    const client = createBossApiClient({
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl,
    });

    const listed = await client.listOrgMembers("org_boss", "boss-dogfood", "ceo");
    const upserted = await client.upsertOrgMember({
      org_id: "org_boss",
      project_id: "boss-dogfood",
      member_id: "org_delegate",
      display_name: "Org Delegate",
      role_keys: ["operator"],
      permissions: ["command_submit"],
      actor: "ceo",
    });

    expect(listed.items[0].member_id).toBe("org_delegate");
    expect(upserted.org_id).toBe("org_boss");
    expect(calls[0].url).toContain("/v1/org-members?org_id=org_boss");
    expect(calls[1].url).toBe("http://127.0.0.1:8000/v1/org-members");
    expect(calls[1].init?.method).toBe("POST");
  });

  it("creates compliance evidence bundles through the API contract", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      return new Response(
        JSON.stringify({
          status: "created",
          artifact_id: "art_compliance",
          project_id: "boss-dogfood",
          artifact_type: "compliance_evidence_bundle",
          created_at: "2026-06-02T00:00:00Z",
          period: {},
          counts: {
            project_members: 1,
            roles: 1,
            policy_records: 0,
            break_glass_grants: 0,
            decisions: 0,
            receipts: 0,
            budget_reservations: 0,
            cost_receipts: 0,
            deadletters: 0,
            compliance_events: 0,
          },
        }),
        {
          status: 201,
          headers: { "Content-Type": "application/json" },
        },
      );
    };
    const client = createBossApiClient({
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl,
    });

    const bundle = await client.createComplianceEvidenceBundle({
      project_id: "boss-dogfood",
      actor: "ceo",
    });

    expect(calls[0].url).toBe(
      "http://127.0.0.1:8000/v1/compliance/evidence-bundles",
    );
    expect(calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init?.body))).toMatchObject({
      project_id: "boss-dogfood",
      actor: "ceo",
    });
    expect(bundle.artifact_type).toBe("compliance_evidence_bundle");
  });

  it("creates decision digests and passes outbox queue urgency", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const responses = [
      {
        status: 201,
        body: {
          status: "created",
          artifact_id: "art_digest",
          outbox_message_id: "out_digest",
          decision_count: 3,
          urgent_count: 1,
          batched_count: 1,
          body: "Decision Digest\n- dec_123",
        },
      },
      {
        status: 201,
        body: {
          id: "job_comms",
          project_id: "boss-dogfood",
          kind: "comms.outbox.deliver",
          status: "queued",
          data_boundary: "internal",
          priority: 25,
          max_attempts: 3,
          attempt: 0,
          run_after: "2026-06-03T00:00:00Z",
        },
      },
    ];
    const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      const next = responses.shift();
      if (!next) {
        throw new Error("unexpected extra request");
      }
      return new Response(JSON.stringify(next.body), {
        status: next.status,
        headers: { "Content-Type": "application/json" },
      });
    };
    const client = createBossApiClient({
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl,
    });

    const digest = await client.createDecisionDigest({
      project_id: "boss-dogfood",
      actor: "ceo",
      channel: "email:founder@example.com",
      max_items: 10,
      dedupe_key: "digest-client",
    });
    await client.queueOutboxDelivery("out_digest", {
      project_id: "boss-dogfood",
      reservation_id: "br_comms",
      dedupe_key: "outbox-queue-client",
      actor: "ceo",
      urgency: "urgent",
    });

    expect(calls[0].url).toBe(
      "http://127.0.0.1:8000/v1/decisions/digests",
    );
    expect(JSON.parse(String(calls[0].init?.body))).toMatchObject({
      project_id: "boss-dogfood",
      channel: "email:founder@example.com",
      max_items: 10,
    });
    expect(calls[1].url).toBe(
      "http://127.0.0.1:8000/v1/comms/outbox/out_digest/queue",
    );
    expect(JSON.parse(String(calls[1].init?.body))).toMatchObject({
      urgency: "urgent",
    });
    expect(digest.outbox_message_id).toBe("out_digest");
  });

  it("lists deployment rollouts through the API contract", async () => {
    const urls: string[] = [];
    const fetchImpl = async (input: RequestInfo | URL) => {
      urls.push(String(input));
      return new Response(
        JSON.stringify({
          project_id: "boss-dogfood",
          items: [
            {
              id: "rollout_1",
              project_id: "boss-dogfood",
              title: "Deploy Demo",
              environment: "staging",
              merged_sha: "abc123",
              pull_request_url: "https://github.com/seanheiney/boss-sandbox/pull/1",
              status: "completed",
              current_stage: "release_readiness",
              stages: [],
              created_at: "2026-06-02T00:00:00Z",
              updated_at: "2026-06-02T00:00:00Z",
            },
          ],
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      );
    };
    const client = createBossApiClient({
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl,
    });

    const rollouts = await client.listDeploymentRollouts("boss-dogfood", 5);

    expect(urls).toEqual([
      "http://127.0.0.1:8000/v1/software-factory/rollouts?project_id=boss-dogfood&limit=5",
    ]);
    expect(rollouts.items[0].status).toBe("completed");
  });

  it("preserves data-boundary classifications on task and job requests", async () => {
    const bodies: unknown[] = [];
    const fetchImpl = async (_input: RequestInfo | URL, init?: RequestInit) => {
      bodies.push(JSON.parse(String(init?.body)));
      return new Response(
        JSON.stringify({
          id: "created",
          project_id: "boss-dogfood",
          title: "Boundary task",
          status: "open",
          priority: "p1",
          risk_tier: 2,
          data_boundary: "restricted",
          dedupe_key: "boundary-client",
          created_at: "2026-06-02T00:00:00Z",
          updated_at: "2026-06-02T00:00:00Z",
        }),
        {
          status: 201,
          headers: { "Content-Type": "application/json" },
        },
      );
    };
    const client = createBossApiClient({
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl,
    });

    await client.createTask({
      project_id: "boss-dogfood",
      title: "Boundary task",
      risk_tier: 2,
      data_boundary: "restricted",
      dedupe_key: "boundary-client",
    });
    await client.createJob({
      project_id: "boss-dogfood",
      kind: "plan.command",
      task_id: "tsk_boundary",
      data_boundary: "local-only",
    });

    expect(bodies).toEqual([
      expect.objectContaining({ data_boundary: "restricted" }),
      expect.objectContaining({ data_boundary: "local-only" }),
    ]);
  });

  it("routes infrastructure lifecycle calls through the shared API contract", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const responses = [
      {
        status: 201,
        body: {
          status: "pending_approval",
          artifact_id: "art_infra_request",
          decision_id: "dec_infra_request",
          data_boundary: "internal",
        },
      },
      {
        status: 200,
        body: {
          project_id: "boss-dogfood",
          items: [
            {
              id: "infra_1",
              project_id: "boss-dogfood",
              provider: "digitalocean",
              resource_type: "droplet",
              resource_name: "boss-preview",
              external_resource_id: "123456",
              region: "nyc3",
              size: "s-1vcpu-1gb",
              image: "ubuntu-24-04-x64",
              tags: ["preview"],
              status: "active",
              ttl_minutes: 60,
              expires_at: "2026-06-03T12:00:00Z",
              data_boundary: "internal",
              metadata: {},
              created_at: "2026-06-03T11:00:00Z",
              updated_at: "2026-06-03T11:00:00Z",
            },
          ],
        },
      },
      {
        status: 200,
        body: {
          status: "terminated",
          resource_id: "infra_1",
          receipt_id: "rcpt_infra_term",
          artifact_id: "art_infra_term",
          provider_ref: "https://api.digitalocean.com/v2/droplets/123456",
        },
      },
    ];
    const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      const next = responses.shift();
      if (!next) {
        throw new Error("unexpected extra request");
      }
      return new Response(JSON.stringify(next.body), {
        status: next.status,
        headers: { "Content-Type": "application/json" },
      });
    };
    const client = createBossApiClient({
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl,
    });

    const review = await client.requestInfraProvisionReview({
      project_id: "boss-dogfood",
      reservation_id: "res_infra",
      title: "Preview worker",
      region: "nyc3",
      size: "s-1vcpu-1gb",
      ttl_minutes: 60,
      tags: ["preview"],
      actor: "ceo",
    });
    const resources = await client.listInfraResources("boss-dogfood", 5);
    const termination = await client.terminateInfraResource("infra_1", {
      project_id: "boss-dogfood",
      dedupe_key: "terminate-infra-1",
      actor: "release_manager",
      reason: "cleanup",
    });

    expect(calls[0].url).toBe("http://127.0.0.1:8000/v1/infra/droplets/requests");
    expect(calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init?.body))).toMatchObject({
      reservation_id: "res_infra",
      title: "Preview worker",
    });
    expect(calls[1].url).toBe(
      "http://127.0.0.1:8000/v1/infra/resources?project_id=boss-dogfood&limit=5",
    );
    expect(calls[2].url).toBe(
      "http://127.0.0.1:8000/v1/infra/resources/infra_1/terminate",
    );
    expect(review.decision_id).toBe("dec_infra_request");
    expect(resources.items[0].provider).toBe("digitalocean");
    expect(termination.status).toBe("terminated");
  });

  it("routes ProviderOps lifecycle calls through the shared API contract", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const providerAccount = {
      id: "pa_1",
      project_id: "boss-dogfood",
      provider: "digitalocean",
      account_label: "DO sandbox",
      scopes: ["read"],
      status: "needs_test",
      read_only: true,
      deploy_capable: false,
      billing_readable: true,
      destructive_actions_disabled: true,
      capabilities: [],
      missing_permissions: [],
      created_by: "ceo",
      created_at: "2026-06-03T00:00:00Z",
      updated_at: "2026-06-03T00:00:00Z",
    };
    const providerHealth = {
      id: "ph_1",
      project_id: "boss-dogfood",
      provider_account_id: "pa_1",
      provider: "digitalocean",
      status: "ok",
      api_available: true,
      rate_limit_status: "unknown",
      usage_cap_status: "unknown",
      last_checked_at: "2026-06-03T00:00:00Z",
      detail: {},
      created_at: "2026-06-03T00:00:00Z",
    };
    const changeSet = {
      id: "chg_1",
      project_id: "boss-dogfood",
      provider_account_id: "pa_1",
      title: "Create staging DNS",
      summary: "Upsert staging record",
      status: "pending_approval",
      risk_tier: 2,
      operations: [],
      expected_cost_usd_micros: 1000,
      cost_quote: {},
      rollback_plan: ["delete DNS record"],
      expected_receipts: [],
      policy_result: {},
      created_by: "ceo",
      created_at: "2026-06-03T00:00:00Z",
      updated_at: "2026-06-03T00:00:00Z",
    };
    const resourceReceipt = {
      id: "rr_1",
      project_id: "boss-dogfood",
      changeset_id: "chg_1",
      dedupe_key: "resource:chg_1:op_1",
      provider: "digitalocean",
      action: "digitalocean.droplet.create",
      resource_type: "droplet",
      status: "failed",
      request_hash: "hash",
      created_at: "2026-06-03T00:00:00Z",
      updated_at: "2026-06-03T00:00:00Z",
    };
    const workerPool = {
      id: "wp_1",
      project_id: "boss-dogfood",
      pool_key: "local-default",
      kind: "local",
      status: "active",
      current_workers: 0,
      desired_workers: 1,
      min_workers: 0,
      max_workers: 2,
      max_queue_depth_per_worker: 5,
      cost_per_worker_hour_usd_micros: 0,
      capabilities: ["plan.command"],
      metadata: {},
      created_by: "ceo",
      created_at: "2026-06-03T00:00:00Z",
      updated_at: "2026-06-03T00:00:00Z",
    };
    const scalePlan = {
      id: "scp_1",
      project_id: "boss-dogfood",
      worker_pool_id: "wp_1",
      status: "applied",
      trigger_reason: "queue_pressure",
      queue_depth: 6,
      running_jobs: 0,
      leased_jobs: 0,
      retryable_jobs: 0,
      current_workers: 0,
      desired_workers_before: 0,
      desired_workers_after: 1,
      target_workers: 1,
      budget_state: "green",
      budget_available_usd_micros: 10_000_000,
      estimated_hourly_delta_usd_micros: 0,
      provider_health_status: "unknown",
      actions: [{ action: "scale_local_workers" }],
      recommendations: [],
      created_by: "ceo",
      created_at: "2026-06-03T00:00:00Z",
    };
    const deploymentRun = {
      id: "deprun_1",
      project_id: "boss-dogfood",
      deployment_target_id: "dt_1",
      changeset_id: "chg_1",
      release_artifact_id: "art_release",
      health_artifact_id: "art_health",
      app_name: "BOSS",
      environment: "staging",
      repo: "seanheiney/boss",
      target_kind: "digitalocean_app",
      target_ref: "app",
      commit_ref: "abc123",
      status: "healthy",
      release_packet: {},
      health_check_results: [{ status: "passed" }],
      rollback_plan: ["previous app spec"],
      release_receipt_ids: [],
      created_by: "ceo",
      created_at: "2026-06-03T00:00:00Z",
      updated_at: "2026-06-03T00:00:00Z",
    };
    const responses = [
      { status: 200, body: { project_id: "boss-dogfood", items: [providerAccount] } },
      { status: 201, body: providerAccount },
      {
        status: 200,
        body: {
          status: "ok",
          provider_account: providerAccount,
          provider_health: providerHealth,
          detail: {},
        },
      },
      {
        status: 201,
        body: {
          project_id: "boss-dogfood",
          runs: [
            {
              id: "rrun_1",
              status: "completed",
              resources_seen: 1,
              resources_changed: 1,
              costs_seen: 1,
            },
          ],
          resources_seen: 1,
          resources_changed: 1,
          costs_seen: 1,
        },
      },
      {
        status: 200,
        body: {
          project_id: "boss-dogfood",
          items: [
            {
              id: "res_1",
              project_id: "boss-dogfood",
              provider_account_id: "pa_1",
              provider: "digitalocean",
              resource_type: "droplet",
              external_id: "123",
              name: "boss-staging",
              state: "active",
              ownership: "discovered_unmanaged",
              environment: "staging",
              health_status: "unknown",
              cost_confidence: "unknown",
              last_seen_at: "2026-06-03T00:00:00Z",
              tags: [],
              metadata: {},
              created_at: "2026-06-03T00:00:00Z",
              updated_at: "2026-06-03T00:00:00Z",
            },
          ],
        },
      },
      { status: 200, body: { project_id: "boss-dogfood", items: [] } },
      {
        status: 200,
        body: {
          project_id: "boss-dogfood",
          items: [
            {
              id: "canom_1",
              project_id: "boss-dogfood",
              provider_account_id: "pa_1",
              cost_event_id: "cost_1",
              decision_id: "dec_1",
              provider: "digitalocean",
              anomaly_type: "provider_spend_freeze_threshold_crossed",
              severity: "orange",
              status: "open",
              amount_usd_micros: 250000,
              threshold_usd_micros: 200000,
              summary: "Provider spend crossed threshold.",
              recommended_actions: [],
              created_at: "2026-06-03T00:00:00Z",
            },
          ],
        },
      },
      { status: 201, body: changeSet },
      { status: 200, body: { ...changeSet, status: "approved", approved_by: "ceo" } },
      {
        status: 200,
        body: {
          status: "failed",
          changeset: { ...changeSet, status: "failed" },
          resource_receipts: [resourceReceipt],
        },
      },
      { status: 200, body: { project_id: "boss-dogfood", items: [resourceReceipt] } },
      { status: 200, body: { project_id: "boss-dogfood", items: [workerPool] } },
      { status: 201, body: workerPool },
      { status: 200, body: { project_id: "boss-dogfood", items: [scalePlan] } },
      {
        status: 200,
        body: {
          scale_plan: scalePlan,
          worker_pool: workerPool,
          budget_state: "green",
          queue: { queue_depth: 6 },
          provider_health: { status: "unknown" },
        },
      },
      {
        status: 200,
        body: {
          status: "completed",
          project_id: "boss-dogfood",
          artifact_id: "art_janitor",
          candidate_count: 1,
          skipped_count: 0,
          estimated_monthly_savings_usd_micros: 250000,
          candidates: [
            {
              external_resource_id: "res_1",
              provider_account_id: "pa_1",
              provider: "digitalocean",
              resource_type: "droplet",
              external_id: "123",
              name: "boss-staging",
              environment: "staging",
              ownership: "orphaned",
              health_status: "active",
              estimated_monthly_savings_usd_micros: 250000,
              cost_confidence: "estimated_from_price_catalog",
              cleanup_skill_id: "digitalocean.droplet.destroy",
              destructive: true,
            },
          ],
          skipped: [],
          changesets: [changeSet],
        },
      },
      {
        status: 201,
        body: {
          id: "dt_1",
          project_id: "boss-dogfood",
          app_name: "BOSS",
          repo: "seanheiney/boss",
          environment: "staging",
          provider_account_id: "pa_1",
          target_kind: "digitalocean_app",
          target_ref: "app",
          domains: ["staging.example.com"],
          required_resources: [],
          secret_refs: ["sec_db"],
          health_checks: [],
          rollback_points: [],
          status: "created",
          created_by: "ceo",
          created_at: "2026-06-03T00:00:00Z",
          updated_at: "2026-06-03T00:00:00Z",
        },
      },
      { status: 200, body: { project_id: "boss-dogfood", items: [] } },
      { status: 200, body: { project_id: "boss-dogfood", items: [deploymentRun] } },
      { status: 201, body: deploymentRun },
      {
        status: 200,
        body: {
          deployment_run: deploymentRun,
          health_artifact_id: "art_health",
        },
      },
      { status: 200, body: { project_id: "boss-dogfood", items: [providerHealth] } },
      { status: 200, body: { project_id: "boss-dogfood", items: [] } },
      { status: 200, body: { project_id: "boss-dogfood", items: [] } },
    ];
    const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      const next = responses.shift();
      if (!next) {
        throw new Error("unexpected extra request");
      }
      return new Response(JSON.stringify(next.body), {
        status: next.status,
        headers: { "Content-Type": "application/json" },
      });
    };
    const client = createBossApiClient({
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl,
    });

    await client.listProviderAccounts("boss-dogfood");
    await client.createProviderAccount({
      project_id: "boss-dogfood",
      provider: "digitalocean",
      account_label: "DO sandbox",
      read_only: true,
      actor: "ceo",
    });
    await client.testProviderAccount("pa_1", "boss-dogfood", "ceo");
    await client.syncExternalResources({ project_id: "boss-dogfood", provider_account_id: "pa_1" });
    await client.listExternalResources("boss-dogfood", {
      provider: "digitalocean",
      ownership: "discovered_unmanaged",
      limit: 25,
    });
    await client.listProviderCosts("boss-dogfood", 10);
    await client.listProviderCostAnomalies("boss-dogfood", 10);
    await client.createChangeSet({
      project_id: "boss-dogfood",
      provider_account_id: "pa_1",
      title: "Create staging DNS",
      summary: "Upsert staging record",
      operations: [
        {
          skill_id: "cloudflare.dns.record.upsert",
          action: "cloudflare.dns.record.upsert",
          resource_type: "dns_record",
          risk_tier: 2,
          production_affecting: false,
        },
      ],
      expected_cost_usd_micros: 1000,
      rollback_plan: ["delete DNS record"],
      actor: "ceo",
    });
    await client.approveChangeSet("chg_1", { actor: "ceo" });
    await client.applyChangeSet("chg_1", { actor: "system" });
    await client.listResourceReceipts("boss-dogfood", 10);
    await client.listWorkerPools("boss-dogfood");
    await client.upsertWorkerPool({
      project_id: "boss-dogfood",
      pool_key: "local-default",
      kind: "local",
      actor: "ceo",
    });
    await client.listScalePlans("boss-dogfood", 5);
    await client.runAutoscalerPlan({
      project_id: "boss-dogfood",
      worker_pool_id: "wp_1",
      actor: "ceo",
    });
    await client.runResourceJanitor({
      project_id: "boss-dogfood",
      provider_account_id: "pa_1",
      max_candidates: 5,
      actor: "ceo",
    });
    await client.createDeploymentTarget({
      project_id: "boss-dogfood",
      app_name: "BOSS",
      repo: "seanheiney/boss",
      environment: "staging",
      provider_account_id: "pa_1",
      target_kind: "digitalocean_app",
      target_ref: "app",
      domains: ["staging.example.com"],
      secret_refs: ["sec_db"],
      actor: "ceo",
    });
    await client.listDeploymentTargets("boss-dogfood");
    await client.listDeploymentRuns("boss-dogfood", 5);
    await client.createDeploymentRun({
      project_id: "boss-dogfood",
      deployment_target_id: "dt_1",
      commit_ref: "abc123",
      actor: "ceo",
    });
    await client.runDeploymentHealthCheck("deprun_1", { actor: "ceo" });
    await client.listProviderHealth("boss-dogfood", { providerAccountId: "pa_1", limit: 5 });
    await client.listDriftReports("boss-dogfood", 5);
    await client.listReconciliationRuns("boss-dogfood", 5);

    expect(calls.map((call) => call.url)).toEqual([
      "http://127.0.0.1:8000/v1/providers?project_id=boss-dogfood",
      "http://127.0.0.1:8000/v1/providers",
      "http://127.0.0.1:8000/v1/providers/pa_1/test?project_id=boss-dogfood&actor=ceo",
      "http://127.0.0.1:8000/v1/resources/sync",
      "http://127.0.0.1:8000/v1/resources?project_id=boss-dogfood&provider=digitalocean&ownership=discovered_unmanaged&limit=25",
      "http://127.0.0.1:8000/v1/costs?project_id=boss-dogfood&limit=10",
      "http://127.0.0.1:8000/v1/cost-anomalies?project_id=boss-dogfood&limit=10",
      "http://127.0.0.1:8000/v1/changesets",
      "http://127.0.0.1:8000/v1/changesets/chg_1/approve",
      "http://127.0.0.1:8000/v1/changesets/chg_1/apply",
      "http://127.0.0.1:8000/v1/resource-receipts?project_id=boss-dogfood&limit=10",
      "http://127.0.0.1:8000/v1/worker-pools?project_id=boss-dogfood",
      "http://127.0.0.1:8000/v1/worker-pools",
      "http://127.0.0.1:8000/v1/autoscaler/plans?project_id=boss-dogfood&limit=5",
      "http://127.0.0.1:8000/v1/autoscaler/plans",
      "http://127.0.0.1:8000/v1/resource-janitor/run",
      "http://127.0.0.1:8000/v1/deployments",
      "http://127.0.0.1:8000/v1/deployments?project_id=boss-dogfood",
      "http://127.0.0.1:8000/v1/deployment-runs?project_id=boss-dogfood&limit=5",
      "http://127.0.0.1:8000/v1/deployment-runs",
      "http://127.0.0.1:8000/v1/deployment-runs/deprun_1/health-check",
      "http://127.0.0.1:8000/v1/provider-health?project_id=boss-dogfood&provider_account_id=pa_1&limit=5",
      "http://127.0.0.1:8000/v1/drift-reports?project_id=boss-dogfood&limit=5",
      "http://127.0.0.1:8000/v1/reconciliation-runs?project_id=boss-dogfood&limit=5",
    ]);
    expect(JSON.parse(String(calls[7].init?.body))).toMatchObject({
      provider_account_id: "pa_1",
      operations: [
        expect.objectContaining({
          action: "cloudflare.dns.record.upsert",
          production_affecting: false,
        }),
      ],
    });
  });

  it("passes task hierarchy fields through task creation", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      return new Response(
        JSON.stringify({
          id: "tsk_child",
          project_id: "boss-dogfood",
          title: "Child task",
          status: "open",
          owner_role_id: "software_engineer",
          parent_task_id: "tsk_parent",
          dependency_task_ids: ["tsk_dependency"],
          due_at: "2026-06-30T12:00:00Z",
          priority: "p1",
          risk_tier: 2,
          data_boundary: "internal",
          dedupe_key: "task-hierarchy-client",
          created_at: "2026-06-03T00:00:00Z",
          updated_at: "2026-06-03T00:00:00Z",
        }),
        {
          status: 201,
          headers: { "Content-Type": "application/json" },
        },
      );
    };
    const client = createBossApiClient({
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl,
    });

    const created = await client.createTask({
      project_id: "boss-dogfood",
      title: "Child task",
      owner_role_id: "software_engineer",
      parent_task_id: "tsk_parent",
      dependency_task_ids: ["tsk_dependency"],
      due_at: "2026-06-30T12:00:00Z",
      priority: "p1",
      risk_tier: 2,
      dedupe_key: "task-hierarchy-client",
    });

    expect(calls[0].url).toBe("http://127.0.0.1:8000/v1/tasks");
    expect(JSON.parse(String(calls[0].init?.body))).toMatchObject({
      owner_role_id: "software_engineer",
      parent_task_id: "tsk_parent",
      dependency_task_ids: ["tsk_dependency"],
      due_at: "2026-06-30T12:00:00Z",
    });
    expect(created.parent_task_id).toBe("tsk_parent");
  });

  it("lists change journal entries and checks RoleSpec scope through API routes", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const responses = [
      {
        status: 200,
        body: {
          project_id: "boss-dogfood",
          items: [
            {
              id: "art_diff",
              project_id: "boss-dogfood",
              artifact_type: "diff_manifest",
              change_kind: "software_factory",
              title: "Diff manifest",
              summary: "Opened PR",
              diff_summary: "Changed API and UI",
              files_changed: ["apps/api/app/main.py"],
              rollback_plan: "Close PR",
              data_boundary: "internal",
              created_at: "2026-06-03T00:00:00Z",
            },
          ],
        },
      },
      {
        status: 200,
        body: {
          id: "art_diff",
          project_id: "boss-dogfood",
          artifact_type: "diff_manifest",
          change_kind: "software_factory",
          title: "Diff manifest",
          files_changed: ["apps/api/app/main.py"],
          data_boundary: "internal",
          created_at: "2026-06-03T00:00:00Z",
          content: { summary: "Opened PR" },
        },
      },
      {
        status: 200,
        body: {
          project_id: "boss-dogfood",
          role_key: "software_engineer",
          allowed: false,
          reason: "forbidden_by_rolespec",
          action: "infra.create_node_real",
          skill_id: "infra.create_node_real",
          allowed_skills: ["code.edit"],
          forbidden_skills: ["infra.*"],
          trust_score_before: 50,
          trust_score_after: 40,
          autonomy_level_before: 1,
          autonomy_level_after: 0,
          scope_violation_count: 1,
          artifact_id: "art_scope",
          autonomy_change_id: "rac_scope",
        },
      },
    ];
    const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      const next = responses.shift();
      if (!next) {
        throw new Error("unexpected extra request");
      }
      return new Response(JSON.stringify(next.body), {
        status: next.status,
        headers: { "Content-Type": "application/json" },
      });
    };
    const client = createBossApiClient({
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl,
    });

    const journal = await client.listChangeJournal("boss-dogfood", 3);
    const detail = await client.getChangeJournalItem("boss-dogfood", "art_diff");
    const scope = await client.checkRoleScope("software_engineer", {
      project_id: "boss-dogfood",
      actor: "vp_agents",
      action: "infra.create_node_real",
      evidence_ref: "event:scope",
    });

    expect(calls[0].url).toBe(
      "http://127.0.0.1:8000/v1/change-journal?project_id=boss-dogfood&limit=3",
    );
    expect(calls[1].url).toBe(
      "http://127.0.0.1:8000/v1/change-journal/art_diff?project_id=boss-dogfood",
    );
    expect(calls[2].url).toBe(
      "http://127.0.0.1:8000/v1/roles/software_engineer/scope/check",
    );
    expect(JSON.parse(String(calls[2].init?.body))).toMatchObject({
      action: "infra.create_node_real",
      evidence_ref: "event:scope",
    });
    expect(journal.items[0].diff_summary).toBe("Changed API and UI");
    expect(detail.content.summary).toBe("Opened PR");
    expect(scope.allowed).toBe(false);
  });
});
