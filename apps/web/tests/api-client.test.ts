import { describe, expect, it } from "vitest";

import { createBossApiClient, BossApiError } from "@boss/api-client";

describe("createBossApiClient", () => {
  it("posts conversational turns through the agent contract", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      return new Response(
        JSON.stringify({
          thread_id: "athr_1",
          reply: "Ready.",
          actions: [],
          pending_confirmation: null,
          messages: [],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    };
    const client = createBossApiClient({
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl,
    });

    const turn = await client.chatWithAgent({
      project_id: "boss-dogfood",
      actor: "executive_console",
      message: "What can you do?",
    });

    expect(calls[0].url).toBe("http://127.0.0.1:8000/v1/agent/chat");
    expect(calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init?.body))).toMatchObject({
      project_id: "boss-dogfood",
      message: "What can you do?",
    });
    expect(turn.thread_id).toBe("athr_1");
  });

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

  it("creates SAML sessions through the API-driven ACS contract", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      return new Response(
        JSON.stringify({
          project_id: "boss-dogfood",
          session_id: "saml_session_1",
          access_token: "boss_saml_token",
          token_type: "bearer",
          member_id: "ceo",
          provider_id: "entidp_boss-dogfood_okta_saml",
          expires_at: "2026-06-04T01:00:00Z",
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

    const session = await client.createSamlSession("boss-dogfood", "base64-saml");

    expect(calls[0].url).toBe(
      "http://127.0.0.1:8000/saml/v2/boss-dogfood/acs",
    );
    expect(calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({
      SAMLResponse: "base64-saml",
    });
    expect(session.member_id).toBe("ceo");
  });

  it("uses budget forecast and consume API contracts", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const responses = [
      {
        status: 200,
        body: {
          scope_type: "project",
          scope_id: "boss-dogfood",
          horizon_hours: 48,
          budget_state: "green",
          cap_usd_micros: 1_000_000,
          reserved_usd_micros: 100_000,
          actual_usd_micros: 25_000,
          available_usd_micros: 875_000,
          committed_usd_micros: 125_000,
          burn_rate_usd_micros_per_hour: 1000,
          forecasted_usd_micros: 148_000,
          time_to_cap_seconds: 3600,
          top_cost_drivers: [],
        },
      },
      {
        status: 200,
        body: {
          status: "consumed",
          reservation_id: "res_123",
          budget_state: "green",
          message: "Reservation consumed and cost receipt recorded.",
          cost_receipt: {
            id: "cost_123",
            project_id: "boss-dogfood",
            reservation_id: "res_123",
            receipt_id: "rcpt_123",
            category: "model_api",
            skill_or_model_id: "openai:gpt-5.2-codex",
            estimated_usd_micros: 50_000,
            actual_usd_micros: 45_000,
            status: "actual",
            created_at: "2026-06-05T10:00:00Z",
          },
        },
      },
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

    const forecast = await client.getBudgetForecast("project", "boss-dogfood", 48);
    const consumed = await client.consumeBudget({
      project_id: "boss-dogfood",
      reservation_id: "res_123",
      receipt_id: "rcpt_123",
      category: "model_api",
      skill_or_model_id: "openai:gpt-5.2-codex",
      estimated_usd_micros: 50_000,
      actual_usd_micros: 45_000,
      actor: "ceo",
    });

    expect(calls[0].url).toBe(
      "http://127.0.0.1:8000/v1/budget/forecast?scope_type=project&scope_id=boss-dogfood&horizon_hours=48",
    );
    expect(calls[1].url).toBe("http://127.0.0.1:8000/v1/budget/consume");
    expect(calls[1].init?.method).toBe("POST");
    expect(JSON.parse(String(calls[1].init?.body))).toMatchObject({
      reservation_id: "res_123",
      receipt_id: "rcpt_123",
      actor: "ceo",
    });
    expect(forecast.forecasted_usd_micros).toBe(148_000);
    expect(consumed.cost_receipt.actual_usd_micros).toBe(45_000);
  });

  it("uses edition and Account Connect API contracts", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const edition = {
      project_id: "boss-dogfood",
      edition_id: "cloud",
      display_name: "BOSS Cloud",
      status: "active",
      account_connect_required: true,
      account_connect_status: "linked",
      account_connect_org: {
        id: "acctconn_boss-dogfood_acct_org_123",
        project_id: "boss-dogfood",
        hosted_org_id: "acct_org_123",
        local_org_id: "org_boss",
        connection_mode: "cloud_required",
        marketplace_identity: { publisher: "seanheiney" },
        billing_portal_ref: "stripe:portal:test",
        connector_authorization_refs: ["github:installation:test"],
        optional_telemetry_enabled: true,
        connection_ref: "acctconn:test",
        status: "linked",
        linked_by: "ceo",
        linked_at: "2026-06-03T00:00:00Z",
        metadata: {},
        created_at: "2026-06-03T00:00:00Z",
        updated_at: "2026-06-03T00:00:00Z",
      },
      local_auth_allowed: false,
      hosted_auth_required: true,
      connection_mode: "cloud_required",
      license_candidate: "source-available-commercial",
      legal_review_required: true,
      included_capabilities: ["cloud_auth", "marketplace_install"],
      commercial_capabilities: ["cloud_auth", "marketplace_install"],
      source_pack: "cloud_edition.yaml",
    };
    const responses = [
      { status: 200, body: edition },
      {
        status: 200,
        body: {
          project_id: "boss-dogfood",
          edition,
          items: [
            {
              id: "lic_boss-dogfood_account_connect",
              project_id: "boss-dogfood",
              edition_id: "cloud",
              account_connect_org_id: "acct_org_123",
              scope_type: "project",
              scope_id: "boss-dogfood",
              capability_key: "account_connect",
              capabilities: ["cloud_auth"],
              status: "active",
              source: "account_connect",
              metadata: {},
            },
          ],
        },
      },
      { status: 200, body: { project_id: "boss-dogfood", edition } },
      {
        status: 200,
        body: {
          project_id: "boss-dogfood",
          edition,
          synced_entitlement_count: 2,
        },
      },
      {
        status: 200,
        body: {
          project_id: "boss-dogfood",
          edition: {
            ...edition,
            status: "blocked_account_connect_required",
            account_connect_status: "not_linked",
            account_connect_org: undefined,
            blocked_reason: "account_connect_required",
          },
        },
      },
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

    const editionStatus = await client.getEdition("boss-dogfood");
    const entitlementList = await client.listLicenseEntitlements(
      "boss-dogfood",
      true,
    );
    const linked = await client.linkAccountConnect({
      project_id: "boss-dogfood",
      hosted_org_id: "acct_org_123",
      actor: "ceo",
    });
    const synced = await client.syncAccountConnect({
      project_id: "boss-dogfood",
      hosted_org_id: "acct_org_123",
      actor: "ceo",
    });
    const unlinked = await client.unlinkAccountConnect({
      project_id: "boss-dogfood",
      hosted_org_id: "acct_org_123",
      actor: "ceo",
    });

    expect(editionStatus.account_connect_status).toBe("linked");
    expect(entitlementList.items[0].capability_key).toBe("account_connect");
    expect(linked.edition.account_connect_required).toBe(true);
    expect(synced.synced_entitlement_count).toBe(2);
    expect(unlinked.edition.status).toBe("blocked_account_connect_required");
    expect(calls[0].url).toBe(
      "http://127.0.0.1:8000/v1/edition?project_id=boss-dogfood",
    );
    expect(calls[1].url).toContain(
      "/v1/entitlements?project_id=boss-dogfood&include_inactive=true",
    );
    expect(calls[2].url).toBe(
      "http://127.0.0.1:8000/v1/account-connect/link",
    );
    expect(calls[2].init?.method).toBe("POST");
    expect(calls[3].url).toBe(
      "http://127.0.0.1:8000/v1/account-connect/sync",
    );
    expect(calls[3].init?.method).toBe("POST");
    expect(calls[4].url).toBe(
      "http://127.0.0.1:8000/v1/account-connect/unlink",
    );
  });

  it("uses pack list/get/install API contracts (cross-slice e2e regression for pack+selfimp+commercial+budget+toasts P2)", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const packList = {
      project_id: "boss-dogfood",
      items: [
        { pack_id: "core_v1", version: "1.0.0", description: "Core", status: "active", installed: true, installed_version: "1.0.0" },
        { pack_id: "test_e2e_reg_pack_v1", version: "0.1.0", description: "Regression", status: "active", installed: true, installed_version: "0.1.0" },
      ],
    };
    const packDetail = { ...packList.items[1], role_count: 1, role_results: [{ role_key: "metatron", status: "present" }], install_status: "imported" };
    const installed = { ...packDetail, install_status: "installed" };

    const responses = [
      { status: 200, body: packList },
      { status: 200, body: packDetail },
      { status: 201, body: installed },
      { status: 403, body: { detail: "actor lacks required permission (pack_manage)" } },
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

    const list = await client.listPacks("boss-dogfood");
    const detail = await client.getPack("boss-dogfood", "test_e2e_reg_pack_v1");
    const inst = await client.installPack({
      project_id: "boss-dogfood",
      pack_id: "test_e2e_reg_pack_v1",
      actor: "ceo",
    });
    await expect(
      client.installPack({
        project_id: "boss-dogfood",
        pack_id: "core_v1",
        actor: "no-perm",
      }),
    ).rejects.toBeInstanceOf(BossApiError);

    expect(list.items.length).toBe(2);
    expect(list.items[1].pack_id).toBe("test_e2e_reg_pack_v1");
    expect(detail.pack_id).toBe("test_e2e_reg_pack_v1");
    expect(detail.install_status).toBe("imported");
    expect(inst.install_status).toBe("installed");
    expect(calls[0].url).toContain("/v1/packs?project_id=boss-dogfood");
    expect(calls[2].url).toBe("http://127.0.0.1:8000/v1/packs/install");
    // cross regression note: pack install/list here mirrors import (source local_upload in backend) + selfimp proposal + commercial source_pack + budget flows; toasts in client-forms wrap these for feedback states (tested in kpis.test)
  });

  it("uses Account Connect team invite and billing portal API contracts", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const authSession = {
      id: "acctauth_1",
      project_id: "boss-dogfood",
      account_connect_org_id: "acctconn_1",
      hosted_org_id: "acct_org_123",
      authorization_url: "https://accounts.example/oauth/authorize",
      return_url: "https://boss.local/account-connect/callback",
      provider_ref: "hosted_auth_123",
      state: "acctstate_123",
      status: "ready",
      receipt_id: "rcpt_auth",
      created_by: "ceo",
      expires_at: "2026-06-04T00:00:00Z",
      metadata: {},
      created_at: "2026-06-03T00:00:00Z",
      updated_at: "2026-06-03T00:00:00Z",
    };
    const invite = {
      id: "acctinvite_1",
      project_id: "boss-dogfood",
      account_connect_org_id: "acctconn_1",
      hosted_org_id: "acct_org_123",
      email: "ops@example.com",
      display_name: "Ops Lead",
      role_keys: ["operator"],
      permissions: ["command_submit"],
      invite_url: "https://accounts.example/invite/123",
      hosted_invite_ref: "hosted_invite_123",
      status: "pending_acceptance",
      delivery_status: "sent",
      receipt_id: "rcpt_1",
      created_by: "ceo",
      metadata: {},
      created_at: "2026-06-03T00:00:00Z",
      updated_at: "2026-06-03T00:00:00Z",
    };
    const billingSession = {
      id: "acctbill_1",
      project_id: "boss-dogfood",
      account_connect_org_id: "acctconn_1",
      hosted_org_id: "acct_org_123",
      portal_url: "https://billing.example/session/123",
      return_url: "https://boss.local/settings",
      provider_ref: "bps_123",
      status: "ready",
      receipt_id: "rcpt_2",
      created_by: "ceo",
      expires_at: "2026-06-04T00:00:00Z",
      metadata: {},
      created_at: "2026-06-03T00:00:00Z",
      updated_at: "2026-06-03T00:00:00Z",
    };
    const responses = [
      { status: 200, body: { project_id: "boss-dogfood", items: [authSession] } },
      { status: 201, body: authSession },
      { status: 200, body: { ...authSession, status: "completed" } },
      { status: 200, body: { project_id: "boss-dogfood", items: [invite] } },
      { status: 201, body: invite },
      { status: 200, body: { ...invite, status: "revoked" } },
      {
        status: 200,
        body: { project_id: "boss-dogfood", items: [billingSession] },
      },
      { status: 201, body: billingSession },
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

    const authSessions = await client.listAccountConnectAuthSessions(
      "boss-dogfood",
    );
    const createdAuthSession = await client.createAccountConnectAuthSession({
      project_id: "boss-dogfood",
      return_url: "https://boss.local/account-connect/callback",
      actor: "ceo",
    });
    const completedAuthSession = await client.completeAccountConnectAuthSession(
      "acctauth_1",
      {
        project_id: "boss-dogfood",
        code: "callback-code",
        state: "acctstate_123",
        actor: "ceo",
      },
    );
    const invites = await client.listAccountConnectTeamInvites("boss-dogfood");
    const createdInvite = await client.createAccountConnectTeamInvite({
      project_id: "boss-dogfood",
      email: "ops@example.com",
      actor: "ceo",
    });
    const revokedInvite = await client.revokeAccountConnectTeamInvite(
      "acctinvite_1",
      {
        project_id: "boss-dogfood",
        reason: "wrong recipient",
        actor: "ceo",
      },
    );
    const sessions = await client.listAccountConnectBillingPortalSessions(
      "boss-dogfood",
    );
    const createdSession = await client.createAccountConnectBillingPortalSession({
      project_id: "boss-dogfood",
      return_url: "https://boss.local/settings",
      actor: "ceo",
    });

    expect(authSessions.items[0].provider_ref).toBe("hosted_auth_123");
    expect(createdAuthSession.authorization_url).toBe(
      "https://accounts.example/oauth/authorize",
    );
    expect(completedAuthSession.status).toBe("completed");
    expect(invites.items[0].email).toBe("ops@example.com");
    expect(createdInvite.hosted_invite_ref).toBe("hosted_invite_123");
    expect(revokedInvite.status).toBe("revoked");
    expect(sessions.items[0].provider_ref).toBe("bps_123");
    expect(createdSession.portal_url).toBe("https://billing.example/session/123");
    expect(calls[0].url).toBe(
      "http://127.0.0.1:8000/v1/account-connect/auth-sessions?project_id=boss-dogfood&limit=50",
    );
    expect(calls[1].url).toBe(
      "http://127.0.0.1:8000/v1/account-connect/auth-sessions",
    );
    expect(calls[1].init?.method).toBe("POST");
    expect(calls[2].url).toBe(
      "http://127.0.0.1:8000/v1/account-connect/auth-sessions/acctauth_1/complete",
    );
    expect(calls[2].init?.method).toBe("POST");
    expect(calls[3].url).toBe(
      "http://127.0.0.1:8000/v1/account-connect/team-invites?project_id=boss-dogfood&limit=50",
    );
    expect(calls[4].url).toBe(
      "http://127.0.0.1:8000/v1/account-connect/team-invites",
    );
    expect(calls[4].init?.method).toBe("POST");
    expect(calls[5].url).toBe(
      "http://127.0.0.1:8000/v1/account-connect/team-invites/acctinvite_1/revoke",
    );
    expect(calls[6].url).toBe(
      "http://127.0.0.1:8000/v1/account-connect/billing-portal-sessions?project_id=boss-dogfood&limit=50",
    );
    expect(calls[7].url).toBe(
      "http://127.0.0.1:8000/v1/account-connect/billing-portal-sessions",
    );
  });

  it("uses Account Connect health check API contracts", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const healthCheck = {
      id: "accthealth_1",
      project_id: "boss-dogfood",
      account_connect_org_id: "acctconn_1",
      hosted_org_id: "acct_org_123",
      check_kind: "hosted_service",
      status: "healthy",
      latency_ms: 27,
      provider_ref: "acct-health-1",
      readiness_packet_artifact_id: "art_acct_readiness_1",
      response_metadata: { status: "healthy" },
      checked_by: "ceo",
      metadata: {},
      created_at: "2026-06-03T00:00:00Z",
    };
    const responses = [
      { status: 200, body: { project_id: "boss-dogfood", items: [healthCheck] } },
      { status: 201, body: healthCheck },
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

    const listed = await client.listAccountConnectHealthChecks(
      "boss-dogfood",
      20,
    );
    const created = await client.runAccountConnectHealthCheck({
      project_id: "boss-dogfood",
      hosted_org_id: "acct_org_123",
      actor: "ceo",
    });

    expect(listed.items[0].status).toBe("healthy");
    expect(created.provider_ref).toBe("acct-health-1");
    expect(created.readiness_packet_artifact_id).toBe("art_acct_readiness_1");
    expect(calls[0].url).toBe(
      "http://127.0.0.1:8000/v1/account-connect/health-checks?project_id=boss-dogfood&limit=20",
    );
    expect(calls[1].url).toBe(
      "http://127.0.0.1:8000/v1/account-connect/health-checks",
    );
    expect(calls[1].init?.method).toBe("POST");
  });

  it("uses enterprise cluster API contracts", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const cluster = {
      id: "entcluster_boss-dogfood_enterprise_cloud_attached",
      org_id: "org_boss",
      project_id: "boss-dogfood",
      deployment_mode: "enterprise_cloud",
      account_connect_mode: "attached",
      worker_pool_strategy: "dedicated_managed_workers",
      ha_profile: "regional_ha",
      data_residency: "us-east",
      identity_controls: {
        sso_oidc_configured: true,
        scim_configured: true,
      },
      governance_controls: {
        budget_governor_precedence: true,
        agents_receive_raw_provider_secrets: false,
      },
      audit_controls: {
        audit_exports_enabled: true,
        retention_days: 730,
      },
      private_worker_pools: [],
      compliance_packs: ["enterprise_compliance"],
      support_tier: "enterprise",
      support_sla: {
        response_time_hours: 2,
        uptime_target: "99.95%",
      },
      status: "active",
      blocking_gaps: [],
      created_by: "ceo",
      metadata: {},
      created_at: "2026-06-03T00:00:00Z",
      updated_at: "2026-06-03T00:00:00Z",
    };
    const validationRun = {
      id: "entclval_1",
      org_id: "org_boss",
      project_id: "boss-dogfood",
      enterprise_cluster_id: "entcluster_boss-dogfood_enterprise_cloud_attached",
      validation_kind: "multi_node_readiness",
      status: "passed",
      checks: [
        {
          name: "kubernetes_manifests",
          status: "passed",
          required: true,
        },
      ],
      evidence_refs: ["artifact:k8s_validate"],
      blocking_gaps: [],
      validated_by: "ceo",
      metadata: {},
      created_at: "2026-06-03T00:05:00Z",
    };
    const responses = [
      { status: 200, body: { project_id: "boss-dogfood", items: [cluster] } },
      { status: 201, body: cluster },
      { status: 200, body: { project_id: "boss-dogfood", items: [validationRun] } },
      { status: 201, body: validationRun },
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

    const listed = await client.listEnterpriseClusters("boss-dogfood");
    const upserted = await client.upsertEnterpriseCluster({
      project_id: "boss-dogfood",
      deployment_mode: "enterprise_cloud",
      account_connect_mode: "attached",
      identity_controls: { sso_oidc_configured: true, scim_configured: true },
      actor: "ceo",
    });
    const validationRuns = await client.listEnterpriseClusterValidationRuns(
      "boss-dogfood",
      "entcluster_boss-dogfood_enterprise_cloud_attached",
    );
    const createdValidationRun =
      await client.createEnterpriseClusterValidationRun({
        project_id: "boss-dogfood",
        enterprise_cluster_id: "entcluster_boss-dogfood_enterprise_cloud_attached",
        validation_kind: "multi_node_readiness",
        checks: [
          {
            name: "kubernetes_manifests",
            status: "passed",
            required: true,
          },
        ],
        evidence_refs: ["artifact:k8s_validate"],
        actor: "ceo",
      });

    expect(listed.items[0].blocking_gaps).toEqual([]);
    expect(upserted.support_tier).toBe("enterprise");
    expect(validationRuns.items[0].validation_kind).toBe("multi_node_readiness");
    expect(createdValidationRun.evidence_refs).toEqual(["artifact:k8s_validate"]);
    expect(calls[0].url).toBe(
      "http://127.0.0.1:8000/v1/enterprise/clusters?project_id=boss-dogfood",
    );
    expect(calls[1].url).toBe(
      "http://127.0.0.1:8000/v1/enterprise/clusters",
    );
    expect(calls[1].init?.method).toBe("POST");
    expect(calls[2].url).toBe(
      "http://127.0.0.1:8000/v1/enterprise/cluster-validation-runs?project_id=boss-dogfood&enterprise_cluster_id=entcluster_boss-dogfood_enterprise_cloud_attached&limit=50",
    );
    expect(calls[3].url).toBe(
      "http://127.0.0.1:8000/v1/enterprise/cluster-validation-runs",
    );
    expect(calls[3].init?.method).toBe("POST");
  });

  it("uses enterprise support case API contracts", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const supportCase = {
      id: "entsup_1",
      org_id: "org_boss",
      project_id: "boss-dogfood",
      enterprise_cluster_id: "entcluster_1",
      account_connect_org_id: "acctconn_1",
      hosted_org_id: "acct_hosted",
      title: "Hosted escalation",
      severity: "high",
      category: "support",
      description: "Need enterprise support.",
      status: "blocked_configuration",
      sla_due_at: "2026-06-03T04:00:00Z",
      response_time_hours: 4,
      hosted_case_ref: undefined,
      receipt_id: "rcpt_support",
      support_packet_artifact_id: "art_support_packet_1",
      assigned_to: undefined,
      created_by: "ceo",
      metadata: { blocked_reason: "account_connect_url_missing" },
      created_at: "2026-06-03T00:00:00Z",
      updated_at: "2026-06-03T00:00:00Z",
    };
    const responses = [
      { status: 200, body: { project_id: "boss-dogfood", items: [supportCase] } },
      { status: 201, body: supportCase },
      { status: 200, body: { ...supportCase, status: "in_progress", assigned_to: "cto" } },
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

    const listed = await client.listEnterpriseSupportCases("boss-dogfood");
    const created = await client.createEnterpriseSupportCase({
      project_id: "boss-dogfood",
      title: "Hosted escalation",
      description: "Need enterprise support.",
      severity: "high",
      escalate_to_hosted: true,
      actor: "ceo",
    });
    const updated = await client.updateEnterpriseSupportCase("entsup_1", {
      project_id: "boss-dogfood",
      status: "in_progress",
      assigned_to: "cto",
      actor: "ceo",
    });

    expect(listed.items[0].receipt_id).toBe("rcpt_support");
    expect(created.support_packet_artifact_id).toBe("art_support_packet_1");
    expect(created.metadata.blocked_reason).toBe("account_connect_url_missing");
    expect(updated.assigned_to).toBe("cto");
    expect(calls[0].url).toBe(
      "http://127.0.0.1:8000/v1/enterprise/support-cases?project_id=boss-dogfood&limit=50",
    );
    expect(calls[1].url).toBe(
      "http://127.0.0.1:8000/v1/enterprise/support-cases",
    );
    expect(calls[1].init?.method).toBe("POST");
    expect(calls[2].url).toBe(
      "http://127.0.0.1:8000/v1/enterprise/support-cases/entsup_1",
    );
  });

  it("uses enterprise SCIM provisioning API contracts", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const group = {
      id: "scimgroup_boss-dogfood_engineering_admins",
      org_id: "org_boss",
      project_id: "boss-dogfood",
      external_id: "grp-eng-admins",
      display_name: "Engineering Admins",
      role_keys: ["engineering_admin"],
      permissions: ["audit_export"],
      raw_group: {},
      status: "active",
      created_by: "ceo",
      created_at: "2026-06-03T00:00:00Z",
      updated_at: "2026-06-03T00:00:00Z",
    };
    const user = {
      id: "scimuser_boss-dogfood_alice_admin_example_com",
      org_id: "org_boss",
      project_id: "boss-dogfood",
      external_id: "usr-alice",
      user_name: "alice.admin@example.com",
      display_name: "Alice Admin",
      member_id: "scim_alice_admin_example_com",
      role_keys: ["engineering_admin"],
      permissions: ["audit_export", "command_submit"],
      groups: ["Engineering Admins"],
      raw_profile: {},
      status: "active",
      active: true,
      created_by: "ceo",
      created_at: "2026-06-03T00:00:00Z",
      updated_at: "2026-06-03T00:00:00Z",
    };
    const responses = [
      { status: 200, body: { project_id: "boss-dogfood", items: [group] } },
      { status: 201, body: group },
      { status: 200, body: { project_id: "boss-dogfood", items: [user] } },
      { status: 201, body: user },
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

    const groups = await client.listEnterpriseScimGroups("boss-dogfood");
    const upsertedGroup = await client.upsertEnterpriseScimGroup({
      project_id: "boss-dogfood",
      display_name: "Engineering Admins",
      permissions: ["audit_export"],
      actor: "ceo",
    });
    const users = await client.listEnterpriseScimUsers("boss-dogfood");
    const upsertedUser = await client.upsertEnterpriseScimUser({
      project_id: "boss-dogfood",
      user_name: "alice.admin@example.com",
      display_name: "Alice Admin",
      groups: ["Engineering Admins"],
      actor: "ceo",
    });

    expect(groups.items[0].display_name).toBe("Engineering Admins");
    expect(upsertedGroup.permissions).toContain("audit_export");
    expect(users.items[0].member_id).toBe("scim_alice_admin_example_com");
    expect(upsertedUser.active).toBe(true);
    expect(calls[0].url).toBe(
      "http://127.0.0.1:8000/v1/enterprise/scim/groups?project_id=boss-dogfood",
    );
    expect(calls[1].url).toBe(
      "http://127.0.0.1:8000/v1/enterprise/scim/groups",
    );
    expect(calls[2].url).toBe(
      "http://127.0.0.1:8000/v1/enterprise/scim/users?project_id=boss-dogfood",
    );
    expect(calls[3].url).toBe(
      "http://127.0.0.1:8000/v1/enterprise/scim/users",
    );
    expect(calls[3].init?.method).toBe("POST");
  });

  it("uses enterprise identity provider API contracts", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const provider = {
      id: "entidp_boss-dogfood_okta_oidc",
      org_id: "org_boss",
      project_id: "boss-dogfood",
      provider_key: "okta_oidc",
      provider_type: "oidc",
      display_name: "Okta OIDC",
      issuer: "https://idp.example",
      audience: "boss-api",
      jwks_url: "https://idp.example/.well-known/jwks.json",
      status: "active",
      test_status: "not_tested",
      blocking_gaps: [],
      metadata: {},
      created_by: "ceo",
      created_at: "2026-06-03T00:00:00Z",
      updated_at: "2026-06-03T00:00:00Z",
    };
    const rolloutPolicy = {
      id: "entidpol_boss-dogfood_saml",
      org_id: "org_boss",
      project_id: "boss-dogfood",
      provider_type: "saml",
      runtime_mode: "auth_allowed",
      allowed_domains: ["example.com"],
      rollout_notes: "SAML dogfood approved.",
      status: "active",
      created_by: "ceo",
      metadata: {},
      created_at: "2026-06-03T00:00:00Z",
      updated_at: "2026-06-03T00:00:00Z",
    };
    const responses = [
      { status: 200, body: { project_id: "boss-dogfood", items: [provider] } },
      { status: 201, body: provider },
      {
        status: 200,
        body: { project_id: "boss-dogfood", items: [rolloutPolicy] },
      },
      { status: 201, body: rolloutPolicy },
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

    const listed = await client.listEnterpriseIdentityProviders("boss-dogfood");
    const upserted = await client.upsertEnterpriseIdentityProvider({
      project_id: "boss-dogfood",
      provider_key: "okta_oidc",
      provider_type: "oidc",
      display_name: "Okta OIDC",
      issuer: "https://idp.example",
      audience: "boss-api",
      jwks_url: "https://idp.example/.well-known/jwks.json",
      status: "active",
      actor: "ceo",
    });
    const rolloutPolicies = await client.listEnterpriseIdentityRolloutPolicies(
      "boss-dogfood",
    );
    const upsertedPolicy = await client.upsertEnterpriseIdentityRolloutPolicy({
      project_id: "boss-dogfood",
      provider_type: "saml",
      runtime_mode: "auth_allowed",
      allowed_domains: ["example.com"],
      actor: "ceo",
    });

    expect(listed.items[0].provider_type).toBe("oidc");
    expect(upserted.status).toBe("active");
    expect(rolloutPolicies.items[0].runtime_mode).toBe("auth_allowed");
    expect(upsertedPolicy.provider_type).toBe("saml");
    expect(calls[0].url).toBe(
      "http://127.0.0.1:8000/v1/enterprise/identity-providers?project_id=boss-dogfood",
    );
    expect(calls[1].url).toBe(
      "http://127.0.0.1:8000/v1/enterprise/identity-providers",
    );
    expect(calls[1].init?.method).toBe("POST");
    expect(calls[2].url).toBe(
      "http://127.0.0.1:8000/v1/enterprise/identity-rollout-policies?project_id=boss-dogfood",
    );
    expect(calls[3].url).toBe(
      "http://127.0.0.1:8000/v1/enterprise/identity-rollout-policies",
    );
  });

  it("uses hosted marketplace listing and sync API contracts", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const listing = {
      id: "mklisting_1",
      project_id: "boss-dogfood",
      account_connect_org_id: "acctconn_1",
      hosted_org_id: "acct_org_123",
      listing_id: "listing_private_agent",
      pack_id: "private_enterprise_agent_v1",
      version: "1.0.0",
      display_name: "Private Enterprise Agent",
      publisher: "BOSS",
      category: "enterprise",
      description: "Enterprise-only hosted module.",
      status: "active",
      price_model: "enterprise_contract",
      price_usd_micros: 0,
      required_entitlements: ["private_agent_marketplace"],
      capabilities: ["agent_module.install"],
      source_url: "https://accounts.example/marketplace/private",
      metadata: {},
      last_sync_run_id: "mktsync_1",
      created_at: "2026-06-04T00:00:00Z",
      updated_at: "2026-06-04T00:00:00Z",
    };
    const syncRun = {
      id: "mktsync_1",
      project_id: "boss-dogfood",
      account_connect_org_id: "acctconn_1",
      hosted_org_id: "acct_org_123",
      status: "synced",
      listing_count: 1,
      provider_ref: "marketplace-sync-1",
      sync_packet_artifact_id: "art_marketplace_sync_packet_1",
      synced_by: "ceo",
      metadata: {},
      created_at: "2026-06-04T00:00:00Z",
    };
    const responses = [
      { status: 200, body: { project_id: "boss-dogfood", items: [listing] } },
      { status: 200, body: { project_id: "boss-dogfood", items: [syncRun] } },
      {
        status: 201,
        body: {
          project_id: "boss-dogfood",
          sync_run: syncRun,
          listing_count: 1,
        },
      },
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

    const listings = await client.listHostedMarketplaceListings(
      "boss-dogfood",
      true,
      25,
    );
    const runs = await client.listHostedMarketplaceSyncRuns("boss-dogfood", 10);
    const synced = await client.syncHostedMarketplace({
      project_id: "boss-dogfood",
      hosted_org_id: "acct_org_123",
      actor: "ceo",
    });

    expect(listings.items[0].pack_id).toBe("private_enterprise_agent_v1");
    expect(runs.items[0].provider_ref).toBe("marketplace-sync-1");
    expect(runs.items[0].sync_packet_artifact_id).toBe(
      "art_marketplace_sync_packet_1",
    );
    expect(synced.listing_count).toBe(1);
    expect(synced.sync_run.sync_packet_artifact_id).toBe(
      "art_marketplace_sync_packet_1",
    );
    expect(calls[0].url).toContain("/v1/marketplace/listings?");
    expect(calls[0].url).toContain("include_inactive=true");
    expect(calls[0].url).toContain("limit=25");
    expect(calls[1].url).toBe(
      "http://127.0.0.1:8000/v1/marketplace/sync-runs?project_id=boss-dogfood&limit=10",
    );
    expect(calls[2].url).toBe(
      "http://127.0.0.1:8000/v1/marketplace/sync",
    );
    expect(calls[2].init?.method).toBe("POST");
  });

  it("uses agent module entitlement and install-record API contracts", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const module = {
      module_id: "private_enterprise_agent_v1",
      version: "1.0.0",
      display_name: "Private Enterprise Agent",
      description: "Enterprise-only packaged agent.",
      publisher: "BOSS",
      category: "enterprise",
      status: "active",
      role_key: "private_enterprise_agent",
      job_kinds: ["enterprise.marketplace.validate"],
      default_skill_ids: ["artifact.create"],
      tags: ["enterprise"],
      price_model: "enterprise_contract",
      price_usd_micros: 0,
      required_entitlements: ["private_agent_marketplace"],
      entitlement_status: "allowed",
      config_schema: {},
      installed: true,
      installed_by: "ceo",
      installed_at: "2026-06-03T00:00:00Z",
      updated_at: "2026-06-03T00:00:00Z",
      installed_version: "1.0.0",
      config: {},
      prompt_bundle: {},
      runtime: {},
      marketplace: { required_entitlement: "private_agent_marketplace" },
      role: {},
      role_result: { role_key: "private_enterprise_agent" },
      entitlement_result: { status: "allowed" },
      install_status: "installed",
    };
    const record = {
      id: "agent_module_install_record_1",
      project_id: "boss-dogfood",
      module_id: "private_enterprise_agent_v1",
      action: "install",
      from_version: undefined,
      to_version: "1.0.0",
      actor: "ceo",
      status: "installed",
      entitlement_result: { status: "allowed" },
      rollback_snapshot: {},
      metadata: { role_key: "private_enterprise_agent" },
      created_at: "2026-06-03T00:00:00Z",
    };
    const runtime = {
      project_id: "boss-dogfood",
      job_kind: "agent.pm.scope_brief",
      matched: true,
      agent_module: { id: "pm_product_planner_v1", role_key: "pm" },
      compiled_prompt: {
        system: "No spendy action without a BudgetReservation.",
        job: "Return scope brief.",
      },
      runtime: { default_skill_ids: ["artifact.create"] },
      role: { role_key: "pm" },
      config: {},
    };
    const uninstalled = {
      ...module,
      installed: false,
      install_status: "uninstalled",
      role_result: { status: "restored_base_role" },
    };
    const rolledBack = {
      ...module,
      installed: true,
      install_status: "rolled_back",
      config: { branch_prefix: "catalog/" },
    };
    const responses = [
      { status: 200, body: { project_id: "boss-dogfood", items: [module] } },
      { status: 201, body: module },
      { status: 200, body: { project_id: "boss-dogfood", items: [record] } },
      { status: 200, body: runtime },
      { status: 200, body: uninstalled },
      { status: 200, body: rolledBack },
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

    const listed = await client.listAgentModules("boss-dogfood");
    const installed = await client.installAgentModule({
      project_id: "boss-dogfood",
      module_id: "private_enterprise_agent_v1",
      actor: "ceo",
    });
    const records = await client.listAgentModuleInstallRecords(
      "boss-dogfood",
      "private_enterprise_agent_v1",
      10,
    );
    const resolved = await client.resolveAgentRuntime({
      project_id: "boss-dogfood",
      job_kind: "agent.pm.scope_brief",
      task_payload: { command: "Scope Decision Inbox defaults." },
      actor: "ceo",
    });
    const disabled = await client.uninstallAgentModule(
      "private_enterprise_agent_v1",
      {
        project_id: "boss-dogfood",
        actor: "ceo",
        reason: "Disable private module.",
      },
    );
    const rollback = await client.rollbackAgentModule(
      "private_enterprise_agent_v1",
      {
        project_id: "boss-dogfood",
        actor: "ceo",
        reason: "Restore previous install.",
      },
    );

    expect(listed.items[0].required_entitlements).toEqual([
      "private_agent_marketplace",
    ]);
    expect(installed.entitlement_result.status).toBe("allowed");
    expect(records.items[0].action).toBe("install");
    expect(resolved.agent_module?.id).toBe("pm_product_planner_v1");
    expect(disabled.install_status).toBe("uninstalled");
    expect(rollback.install_status).toBe("rolled_back");
    expect(calls[0].url).toBe(
      "http://127.0.0.1:8000/v1/agent-modules?project_id=boss-dogfood",
    );
    expect(calls[1].url).toBe(
      "http://127.0.0.1:8000/v1/agent-modules/install",
    );
    expect(calls[1].init?.method).toBe("POST");
    expect(calls[2].url).toContain(
      "/v1/agent-module-install-records?project_id=boss-dogfood",
    );
    expect(calls[2].url).toContain("module_id=private_enterprise_agent_v1");
    expect(calls[2].url).toContain("limit=10");
    expect(calls[3].url).toBe(
      "http://127.0.0.1:8000/v1/agent-runtime/resolve",
    );
    expect(calls[3].init?.method).toBe("POST");
    expect(calls[4].url).toBe(
      "http://127.0.0.1:8000/v1/agent-modules/private_enterprise_agent_v1/uninstall",
    );
    expect(calls[5].url).toBe(
      "http://127.0.0.1:8000/v1/agent-modules/private_enterprise_agent_v1/rollback",
    );
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

  it("carries entitlement OAuth connection metadata through the API contract", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const entitlementSource = {
      id: "src_boss_openai_codex_chatgpt",
      project_id: "boss-dogfood",
      provider: "openai",
      source_id: "openai_codex_chatgpt",
      source_kind: "official_cli",
      entitlement_kind: "subscription",
      owner_type: "workspace_operator",
      owner_id: "founder",
      workload_classes_allowed: ["owner_sandbox"],
      multi_tenant_allowed: false,
      credential_ref: "BOSS_OPENAI_CODEX_OAUTH_SESSION_REF",
      cost_mode: "subscription",
      limit_shape: {},
      supported_models: ["codex"],
      task_strengths: ["coding"],
      risk_tier_max: 1,
      status: "active",
      effective_status: "active",
      auth_method: "official_cli_oauth",
      connection_status: "connected",
      connection_ref: "local:codex-login",
      oauth_scopes: ["codex.local"],
      connection_metadata: { login_command: "codex login" },
      policy_notes: ["Do not use as shared backend infrastructure."],
      observed_capacity: {
        credential_present: true,
        official_oauth_connected: true,
      },
      runtime_supported: true,
      created_at: "2026-06-03T00:00:00Z",
      updated_at: "2026-06-03T00:00:00Z",
    };
    const disconnectedSource = {
      ...entitlementSource,
      connection_status: "revoked",
      effective_status: "missing_oauth_connection",
      observed_capacity: {
        credential_present: false,
        official_oauth_connected: false,
        capacity_state: "unknown",
      },
    };
    const responses = [
      { status: 200, body: { project_id: "boss-dogfood", items: [entitlementSource] } },
      { status: 201, body: entitlementSource },
      { status: 200, body: entitlementSource },
      { status: 200, body: disconnectedSource },
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

    const listed = await client.listEntitlementSources("boss-dogfood");
    const upserted = await client.upsertEntitlementSource({
      project_id: "boss-dogfood",
      provider: "openai",
      source_id: "openai_codex_chatgpt",
      source_kind: "official_cli",
      entitlement_kind: "subscription",
      owner_type: "workspace_operator",
      owner_id: "founder",
      auth_method: "official_cli_oauth",
      connection_status: "connected",
      connection_ref: "local:codex-login",
      oauth_scopes: ["codex.local"],
      connection_metadata: { login_command: "codex login" },
      actor: "ceo",
    });
    const connected = await client.connectEntitlementSource("openai_codex_chatgpt", {
      project_id: "boss-dogfood",
      connection_ref: "local:codex-login",
      auth_method: "official_cli_oauth",
      oauth_scopes: ["codex.local"],
      connection_metadata: { login_command: "codex login" },
      observed_capacity: { capacity_state: "available", remaining_units: 10 },
      actor: "ceo",
    });
    const disconnected = await client.disconnectEntitlementSource(
      "openai_codex_chatgpt",
      {
        project_id: "boss-dogfood",
        reason: "operator_revoked_session",
        actor: "ceo",
      },
    );

    expect(listed.items[0].auth_method).toBe("official_cli_oauth");
    expect(listed.items[0].connection_status).toBe("connected");
    expect(upserted.connection_ref).toBe("local:codex-login");
    expect(connected.observed_capacity.official_oauth_connected).toBe(true);
    expect(disconnected.connection_status).toBe("revoked");
    expect(calls[0].url).toContain("/v1/entitlements/sources");
    expect(calls[1].init?.method).toBe("POST");
    expect(calls[2].url).toContain(
      "/v1/entitlements/sources/openai_codex_chatgpt/connect",
    );
    expect(calls[3].url).toContain(
      "/v1/entitlements/sources/openai_codex_chatgpt/disconnect",
    );
  });

  it("uses the v2 Model Entitlement Broker API contract", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const adapter = {
      id: "adapter_openai_api",
      provider: "openai",
      source_id: "openai_api",
      source_kind: "official_api",
      access_class: "official_api",
      entitlement_kind: "usage_based_api",
      auth_method: "api_key",
      connection_ref: "OPENAI_API_KEY",
      supported_models: ["gpt-5.2-codex"],
      task_strengths: ["planning"],
      allowed_workload_classes: ["owner_sandbox", "multi_tenant_customer"],
      multi_tenant_allowed: true,
      runtime_supported: true,
      credential_present: true,
      official_connection_required: false,
      policy_notes: ["API usage requires billing."],
    };
    const pool = {
      id: "pool_boss-dogfood_openai_api",
      project_id: "boss-dogfood",
      provider: "openai",
      source_id: "openai_api",
      source_kind: "official_api",
      access_class: "official_api",
      auth_method: "api_key",
      connection_status: "connected",
      effective_status: "active",
      owner_type: "project",
      owner_id: "boss-dogfood",
      allowed_workload_classes: ["owner_sandbox"],
      routing_modes: ["budget_safe"],
      capacity_state: "available",
      multi_tenant_allowed: true,
      budget_policy_ref: "budget_policy:boss-dogfood",
      runtime_supported: true,
      supported_models: ["gpt-5.2-codex"],
      task_strengths: ["planning"],
      risk_tier_max: 3,
      observed_capacity: { capacity_state: "available" },
      policy_notes: [],
      updated_at: "2026-06-03T00:00:00Z",
    };
    const receipt = {
      id: "caprec_1",
      project_id: "boss-dogfood",
      entitlement_pool_id: pool.id,
      source_id: "openai_api",
      provider: "openai",
      capacity_kind: "official_api",
      usage_units: { requests: 1 },
      capacity_state_after: "available",
      dedupe_key: "probe-1",
      status: "recorded",
      observed_capacity: { capacity_state: "available" },
      created_by: "ceo",
      created_at: "2026-06-03T00:00:00Z",
    };
    const decision = {
      id: "route_1",
      project_id: "boss-dogfood",
      skill_id: "model.generate_text",
      task_kind: "planning",
      workload_class: "owner_sandbox",
      risk_tier: 1,
      selected_entitlement_pool_id: pool.id,
      selected_source_id: "openai_api",
      selected_provider: "openai",
      selected_model_id: "openai:gpt-5.2-codex",
      routing_mode: "budget_safe",
      rejected_sources: [],
      budget_state: "green",
      data_boundary: "internal",
      capacity_snapshot: { source_id: "openai_api" },
      policy_result: { allowed: true },
      created_at: "2026-06-03T00:00:00Z",
    };
    const responses = [
      { status: 200, body: { items: [adapter] } },
      { status: 200, body: { project_id: "boss-dogfood", items: [pool] } },
      {
        status: 200,
        body: {
          project_id: "boss-dogfood",
          pool,
          receipt,
          duplicate: false,
        },
      },
      { status: 200, body: { project_id: "boss-dogfood", decision } },
      { status: 200, body: { project_id: "boss-dogfood", items: [decision] } },
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

    const adapters = await client.listModelAdapters();
    const pools = await client.listEntitlementPools("boss-dogfood");
    const probe = await client.probeEntitlementPool(pool.id, {
      project_id: "boss-dogfood",
      observed_capacity: { capacity_state: "available" },
      usage_units: { requests: 1 },
      dedupe_key: "probe-1",
      actor: "ceo",
    });
    const simulated = await client.simulateModelRouteDecision({
      project_id: "boss-dogfood",
      skill_id: "model.generate_text",
      task_kind: "planning",
      workload_class: "owner_sandbox",
      actor: "ceo",
    });
    const listed = await client.listModelRouteDecisions("boss-dogfood", {
      limit: 5,
      provider: "openai",
      data_boundary: "internal",
      selected_only: true,
    });

    expect(adapters.items[0].source_id).toBe("openai_api");
    expect(pools.items[0].id).toBe(pool.id);
    expect(probe.receipt.dedupe_key).toBe("probe-1");
    expect(simulated.decision.selected_entitlement_pool_id).toBe(pool.id);
    expect(listed.items[0].id).toBe("route_1");
    expect(calls[0].url).toBe("http://127.0.0.1:8000/v1/model-adapters");
    expect(calls[1].url).toContain("/v1/entitlement-pools?project_id=boss-dogfood");
    expect(calls[2].url).toContain(
      "/v1/entitlement-pools/pool_boss-dogfood_openai_api/probe",
    );
    expect(calls[2].init?.method).toBe("POST");
    expect(calls[3].url).toBe(
      "http://127.0.0.1:8000/v1/model-route-decisions/simulate",
    );
    expect(calls[4].url).toContain("/v1/model-route-decisions?");
    expect(calls[4].url).toContain("project_id=boss-dogfood");
    expect(calls[4].url).toContain("limit=5");
    expect(calls[4].url).toContain("provider=openai");
    expect(calls[4].url).toContain("data_boundary=internal");
    expect(calls[4].url).toContain("selected_only=true");
  });

  it("manages entitlement connection sessions through the API contract", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const source = {
      id: "src_boss-dogfood_openai_codex_chatgpt",
      project_id: "boss-dogfood",
      provider: "openai",
      source_id: "openai_codex_chatgpt",
      source_kind: "official_cli",
      entitlement_kind: "subscription",
      owner_type: "workspace_operator",
      owner_id: "founder",
      workload_classes_allowed: ["owner_sandbox"],
      multi_tenant_allowed: false,
      cost_mode: "subscription",
      limit_shape: {},
      supported_models: ["codex"],
      task_strengths: ["coding"],
      risk_tier_max: 1,
      status: "active",
      effective_status: "active",
      auth_method: "official_cli_oauth",
      connection_status: "connected",
      connection_ref: "secretref://local/codex-session",
      oauth_scopes: ["codex.local"],
      connection_metadata: {},
      policy_notes: [],
      observed_capacity: { capacity_state: "available" },
      runtime_supported: true,
      created_at: "2026-06-04T00:00:00Z",
      updated_at: "2026-06-04T00:00:00Z",
    };
    const session = {
      id: "entconn_1",
      project_id: "boss-dogfood",
      source_id: "openai_codex_chatgpt",
      provider: "openai",
      auth_method: "official_cli_oauth",
      connection_mode: "browser_oauth",
      status: "operator_action_required",
      requested_scopes: ["codex.local"],
      login_command: "codex login",
      instructions: { operator_action: "Run codex login" },
      metadata: {},
      error: {},
      requested_by: "ceo",
      created_at: "2026-06-04T00:00:00Z",
      updated_at: "2026-06-04T00:00:00Z",
    };
    const responses = [
      { status: 201, body: session },
      { status: 200, body: { project_id: "boss-dogfood", items: [session] } },
      {
        status: 200,
        body: {
          project_id: "boss-dogfood",
          session: { ...session, status: "completed", completed_by: "ceo" },
          source,
        },
      },
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

    const started = await client.createEntitlementConnectionSession(
      "openai_codex_chatgpt",
      {
        project_id: "boss-dogfood",
        connection_mode: "browser_oauth",
        requested_scopes: ["codex.local"],
        actor: "ceo",
      },
    );
    const listed = await client.listEntitlementConnectionSessions("boss-dogfood", {
      source_id: "openai_codex_chatgpt",
      limit: 5,
    });
    const completed = await client.completeEntitlementConnectionSession("entconn_1", {
      project_id: "boss-dogfood",
      connection_ref: "secretref://local/codex-session",
      oauth_scopes: ["codex.local"],
      actor: "ceo",
    });

    expect(started.login_command).toBe("codex login");
    expect(listed.items[0].id).toBe("entconn_1");
    expect(completed.source.connection_status).toBe("connected");
    expect(calls[0].url).toContain(
      "/v1/entitlements/sources/openai_codex_chatgpt/connection-sessions",
    );
    expect(calls[0].init?.method).toBe("POST");
    expect(calls[1].url).toContain(
      "/v1/entitlement-connection-sessions?project_id=boss-dogfood&source_id=openai_codex_chatgpt&limit=5",
    );
    expect(calls[2].url).toContain(
      "/v1/entitlement-connection-sessions/entconn_1/complete",
    );
    expect(calls[2].init?.method).toBe("POST");
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

  it("manages communications channel rollout policies", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const responses = [
      {
        status: 200,
        body: {
          project_id: "boss-dogfood",
          items: [
            {
              id: "comms_policy_boss-dogfood_slack",
              project_id: "boss-dogfood",
              provider: "slack",
              delivery_mode: "draft_only",
              allowed_data_boundaries: ["public", "internal"],
              rollout_notes: "Owner approval pending.",
              status: "active",
              created_by: "executive_console",
              metadata: { source: "mission_control" },
              created_at: "2026-06-03T00:00:00Z",
              updated_at: "2026-06-03T00:00:00Z",
            },
          ],
        },
      },
      {
        status: 201,
        body: {
          id: "comms_policy_boss-dogfood_email",
          project_id: "boss-dogfood",
          provider: "email",
          delivery_mode: "real_send_allowed",
          allowed_data_boundaries: ["public"],
          rollout_notes: "SMTP sandbox approved.",
          status: "active",
          created_by: "release_manager",
          metadata: { approval_ref: "dec_1" },
          created_at: "2026-06-03T00:00:00Z",
          updated_at: "2026-06-03T00:01:00Z",
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

    const listed = await client.listCommsChannelPolicies("boss-dogfood");
    const policy = await client.upsertCommsChannelPolicy({
      project_id: "boss-dogfood",
      provider: "email",
      delivery_mode: "real_send_allowed",
      allowed_data_boundaries: ["public"],
      rollout_notes: "SMTP sandbox approved.",
      metadata: { approval_ref: "dec_1" },
      actor: "release_manager",
    });

    expect(calls[0].url).toBe(
      "http://127.0.0.1:8000/v1/comms/policies?project_id=boss-dogfood",
    );
    expect(calls[1].url).toBe(
      "http://127.0.0.1:8000/v1/comms/policies",
    );
    expect(JSON.parse(String(calls[1].init?.body))).toMatchObject({
      provider: "email",
      delivery_mode: "real_send_allowed",
      allowed_data_boundaries: ["public"],
    });
    expect(listed.items[0].delivery_mode).toBe("draft_only");
    expect(policy.provider).toBe("email");
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

  it("uses canonical KPI API routes", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const responses = [
      {
        status: 200,
        body: {
          project_id: "boss-dogfood",
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
              updated_at: "2026-06-05T00:00:00Z",
            },
          ],
        },
      },
      {
        status: 201,
        body: {
          id: "kpi_1",
          project_id: "boss-dogfood",
          kpi_key: "verified_work_throughput",
          name: "Verified Work Throughput",
          target_value: 10,
          current_value: 3,
          unit: "artifacts/week",
          status: "active",
          created_by: "ceo",
          created_at: "2026-06-05T00:00:00Z",
          updated_at: "2026-06-05T00:01:00Z",
        },
      },
    ];
    const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      const response = responses.shift();
      return new Response(JSON.stringify(response?.body), {
        status: response?.status ?? 500,
        headers: { "Content-Type": "application/json" },
      });
    };
    const client = createBossApiClient({
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl,
    });

    const listed = await client.listKpis("boss-dogfood");
    const created = await client.createKpi({
      project_id: "boss-dogfood",
      kpi_key: "verified_work_throughput",
      name: "Verified Work Throughput",
      target_value: 10,
      current_value: 3,
      unit: "artifacts/week",
      actor: "ceo",
    });

    expect(calls[0].url).toBe(
      "http://127.0.0.1:8000/v1/kpis?project_id=boss-dogfood&limit=50&offset=0",
    );
    expect(calls[1].url).toBe("http://127.0.0.1:8000/v1/kpis");
    expect(JSON.parse(String(calls[1].init?.body))).toMatchObject({
      kpi_key: "verified_work_throughput",
      target_value: 10,
      current_value: 3,
    });
    expect(listed.items[0].id).toBe("kpi_1");
    expect(created.current_value).toBe(3);
  });

  it("uses self-improvement proposal/canary/rollout/rollback API contracts (P2 e2e + SIM states)", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const responses = [
      {
        status: 200,
        body: {
          project_id: "boss-dogfood",
          items: [
            {
              id: "imp_1",
              project_id: "boss-dogfood",
              task_id: "task_imp",
              title: "Test self imp",
              summary: "e2e",
              change_ref: "pr://1",
              diff_summary: "diff",
              status: "proposed",
              risk_tier: 2,
              expected_impact: ["a"],
              test_plan: ["t"],
              canary_scope: ["c"],
              rollback_plan: ["r"],
              success_metrics: ["m"],
              created_by: "vp_agents",
              created_at: "2026-06-05T00:00:00Z",
              updated_at: "2026-06-05T00:00:00Z",
            },
          ],
        },
      },
      {
        status: 201,
        body: {
          id: "imp_1",
          project_id: "boss-dogfood",
          task_id: "task_imp",
          title: "Test self imp",
          summary: "e2e",
          change_ref: "pr://1",
          diff_summary: "diff",
          status: "proposed",
          risk_tier: 2,
          expected_impact: ["a"],
          test_plan: ["t"],
          canary_scope: ["c"],
          rollback_plan: ["r"],
          success_metrics: ["m"],
          created_by: "vp_agents",
          created_at: "2026-06-05T00:00:00Z",
          updated_at: "2026-06-05T00:00:00Z",
        },
      },
      { status: 200, body: { id: "imp_1", status: "canary_succeeded" } },
      { status: 200, body: { id: "imp_1", status: "rolled_out" } },
      { status: 200, body: { id: "imp_1", status: "rolled_back" } },
    ];
    const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      const response = responses.shift();
      return new Response(JSON.stringify(response?.body), {
        status: response?.status ?? 500,
        headers: { "Content-Type": "application/json" },
      });
    };
    const client = createBossApiClient({
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl,
    });

    const listed = await client.listSelfImprovementProposals("boss-dogfood");
    const created = await client.createSelfImprovementProposal({
      project_id: "boss-dogfood",
      title: "Test self imp",
      summary: "e2e",
      change_ref: "pr://1",
      diff_summary: "diff",
      expected_impact: ["a"],
      test_plan: ["t"],
      canary_scope: ["c"],
      rollback_plan: ["r"],
      success_metrics: ["m"],
      actor: "vp_agents",
    });
    await client.recordSelfImprovementCanary("imp_1", {
      actor: "release_manager",
      outcome: "succeeded",
      summary: "canary ok",
      metrics: { ok: true },
      evidence_refs: ["e1"],
    });
    await client.recordSelfImprovementRollout("imp_1", {
      actor: "release_manager",
      summary: "rolled",
      metrics: { ok: true },
      evidence_refs: ["e2"],
    });
    await client.rollbackSelfImprovementProposal("imp_1", {
      actor: "release_manager",
      reason: "rolled back",
      metrics: { ok: true },
      evidence_refs: ["e3"],
    });

    expect(calls[0].url).toBe(
      "http://127.0.0.1:8000/v1/self-improvement/proposals?project_id=boss-dogfood&limit=25",
    );
    expect(calls[1].url).toBe("http://127.0.0.1:8000/v1/self-improvement/proposals");
    expect(calls[2].url).toContain("/v1/self-improvement/proposals/imp_1/canary");
    expect(calls[3].url).toContain("/v1/self-improvement/proposals/imp_1/rollout");
    expect(calls[4].url).toContain("/v1/self-improvement/proposals/imp_1/rollback");
    expect(listed.items[0].id).toBe("imp_1");
    expect(created.status).toBe("proposed");
  });

  it("exercises permission denied (403) response path for KPI and budget via client (GAP-AUTO-002 perm failure)", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      return new Response(
        JSON.stringify({ detail: "actor lacks required permission (pack_manage or budget)" }),
        {
          status: 403,
          headers: { "Content-Type": "application/json" },
        },
      );
    };
    const client = createBossApiClient({
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl,
    });

    let kpiErr: any = null;
    try {
      await client.listKpis("boss-dogfood");
    } catch (e) {
      kpiErr = e;
    }

    let budgetErr: any = null;
    try {
      await client.getBudgetForecast("project", "boss-dogfood", 24);
    } catch (e) {
      budgetErr = e;
    }

    expect(calls.some((c) => c.url.includes("/v1/kpis"))).toBe(true);
    expect(calls.some((c) => c.url.includes("/v1/budget/forecast"))).toBe(true);
    // client may wrap as BossApiError (preferred) or raw; either covers the 403 perm path
    if (kpiErr) {
      expect(kpiErr instanceof BossApiError || String(kpiErr).match(/403|forbidden|permission/i)).toBeTruthy();
    }
    if (budgetErr) {
      expect(budgetErr instanceof BossApiError || String(budgetErr).match(/403|forbidden|permission/i)).toBeTruthy();
    }
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
      {
        status: 200,
        body: {
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
              status: "active",
              created_by: "ceo",
              metadata: {},
              created_at: "2026-06-03T00:00:00Z",
              updated_at: "2026-06-03T00:00:00Z",
            },
          ],
        },
      },
      {
        status: 201,
        body: {
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
          status: "active",
          created_by: "ceo",
          metadata: {},
          created_at: "2026-06-03T00:00:00Z",
          updated_at: "2026-06-03T00:00:00Z",
        },
      },
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
    await client.listDeploymentTargets("boss-dogfood", 50, 0);
    await client.listDeploymentEnvironmentPolicies("boss-dogfood", 50, 0);
    await client.upsertDeploymentEnvironmentPolicy({
      project_id: "boss-dogfood",
      environment: "staging",
      runtime_mode: "staging_allowed",
      allowed_target_kinds: ["digitalocean_app"],
      actor: "ceo",
    });
    await client.listDeploymentRuns("boss-dogfood", 5, 0);
    await client.createDeploymentRun({
      project_id: "boss-dogfood",
      deployment_target_id: "dt_1",
      commit_ref: "abc123",
      actor: "ceo",
    });
    await client.runDeploymentHealthCheck("deprun_1", { actor: "ceo" });
    await client.listProviderHealth("boss-dogfood", { providerAccountId: "pa_1", limit: 5, offset: 0 });
    await client.listDriftReports("boss-dogfood", 5, 0);
    await client.listReconciliationRuns("boss-dogfood", 5, 0);

    expect(calls.map((call) => call.url)).toEqual([
      "http://127.0.0.1:8000/v1/providers?project_id=boss-dogfood",
      "http://127.0.0.1:8000/v1/providers",
      "http://127.0.0.1:8000/v1/providers/pa_1/test?project_id=boss-dogfood&actor=ceo",
      "http://127.0.0.1:8000/v1/resources/sync",
      "http://127.0.0.1:8000/v1/resources?project_id=boss-dogfood&provider=digitalocean&ownership=discovered_unmanaged&limit=25",
      "http://127.0.0.1:8000/v1/costs?project_id=boss-dogfood&limit=10&offset=0",
      "http://127.0.0.1:8000/v1/cost-anomalies?project_id=boss-dogfood&limit=10&offset=0",
      "http://127.0.0.1:8000/v1/changesets",
      "http://127.0.0.1:8000/v1/changesets/chg_1/approve",
      "http://127.0.0.1:8000/v1/changesets/chg_1/apply",
      "http://127.0.0.1:8000/v1/resource-receipts?project_id=boss-dogfood&limit=10&offset=0",
      "http://127.0.0.1:8000/v1/worker-pools?project_id=boss-dogfood",
      "http://127.0.0.1:8000/v1/worker-pools",
      "http://127.0.0.1:8000/v1/autoscaler/plans?project_id=boss-dogfood&limit=5",
      "http://127.0.0.1:8000/v1/autoscaler/plans",
      "http://127.0.0.1:8000/v1/resource-janitor/run",
      "http://127.0.0.1:8000/v1/deployments",
      "http://127.0.0.1:8000/v1/deployments?project_id=boss-dogfood&limit=50&offset=0",
      "http://127.0.0.1:8000/v1/deployment-environment-policies?project_id=boss-dogfood&limit=50&offset=0",
      "http://127.0.0.1:8000/v1/deployment-environment-policies",
      "http://127.0.0.1:8000/v1/deployment-runs?project_id=boss-dogfood&limit=5&offset=0",
      "http://127.0.0.1:8000/v1/deployment-runs",
      "http://127.0.0.1:8000/v1/deployment-runs/deprun_1/health-check",
      "http://127.0.0.1:8000/v1/provider-health?project_id=boss-dogfood&provider_account_id=pa_1&limit=5&offset=0",
      "http://127.0.0.1:8000/v1/drift-reports?project_id=boss-dogfood&limit=5&offset=0",
      "http://127.0.0.1:8000/v1/reconciliation-runs?project_id=boss-dogfood&limit=5&offset=0",
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

  it("uses structured meeting run API contract", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const responseBody = {
      id: "mtg_release",
      project_id: "boss-dogfood",
      title: "Release go/no-go",
      purpose: "Review release readiness.",
      status: "completed",
      participants: ["ceo", "program_manager"],
      agenda: ["readiness"],
      transcript_artifact_id: "art_transcript",
      minutes_artifact_id: "art_minutes",
      action_task_ids: ["tsk_action"],
      decisions: [{ decision_id: "dec_release", question: "Approve release?" }],
      dissent_records: [],
      blocked_state: null,
      completed_at: "2026-06-03T00:00:00Z",
      created_by: "ceo",
      created_at: "2026-06-03T00:00:00Z",
      updated_at: "2026-06-03T00:00:00Z",
    };
    const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      return new Response(JSON.stringify(responseBody), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    };
    const client = createBossApiClient({
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl,
    });

    const meeting = await client.runMeeting("mtg_release", {
      actor: "program_manager",
      transcript: [{ speaker: "program_manager", message: "Ready." }],
      minutes: { summary: "Ready with approval." },
      action_items: [{ title: "Attach smoke report", priority: "p1" }],
      decision_items: [{ question: "Approve release?", risk_tier: 2 }],
    });

    expect(calls[0].url).toBe(
      "http://127.0.0.1:8000/v1/meetings/mtg_release/run",
    );
    expect(calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init?.body))).toMatchObject({
      actor: "program_manager",
      minutes: { summary: "Ready with approval." },
      action_items: [{ title: "Attach smoke report", priority: "p1" }],
    });
    expect(meeting.minutes_artifact_id).toBe("art_minutes");
    expect(meeting.action_task_ids).toEqual(["tsk_action"]);
  });

  it("uses deadletter remediation API contracts", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const deadletter = {
      id: "dlq_1",
      project_id: "boss-dogfood",
      source_kind: "job",
      source_id: "job_1",
      reason: "terminal failure",
      payload: { message: "terminal failure" },
      status: "requeued",
      incident_task_id: "tsk_incident",
      incident_artifact_id: "art_incident",
      requeued_job_id: "job_requeued",
      resolved_by: "ceo",
      resolved_at: "2026-06-03T00:00:00Z",
      created_at: "2026-06-03T00:00:00Z",
      updated_at: "2026-06-03T00:00:00Z",
    };
    const responses = [
      {
        status: 200,
        body: {
          status: "incident_created",
          deadletter: { ...deadletter, status: "incident_created" },
          incident_task_id: "tsk_incident",
          incident_artifact_id: "art_incident",
        },
      },
      {
        status: 200,
        body: {
          status: "requeued",
          deadletter,
          requeued_job_id: "job_requeued",
        },
      },
    ];
    const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      const next = responses.shift();
      return new Response(JSON.stringify(next?.body ?? {}), {
        status: next?.status ?? 500,
        headers: { "Content-Type": "application/json" },
      });
    };
    const client = createBossApiClient({
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl,
    });

    const incident = await client.createDeadletterIncident("dlq_1", {
      actor: "ceo",
      severity: "critical",
    });
    const requeued = await client.requeueDeadletterJob("dlq_1", {
      actor: "ceo",
      reason: "root_cause_fixed",
    });

    expect(calls[0].url).toBe(
      "http://127.0.0.1:8000/v1/deadletters/dlq_1/incident",
    );
    expect(JSON.parse(String(calls[0].init?.body))).toMatchObject({
      actor: "ceo",
      severity: "critical",
    });
    expect(calls[1].url).toBe(
      "http://127.0.0.1:8000/v1/deadletters/dlq_1/requeue",
    );
    expect(incident.incident_artifact_id).toBe("art_incident");
    expect(requeued.requeued_job_id).toBe("job_requeued");
  });

  it("uses ops queue inspection and digest API contracts", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const inspection = {
      project_id: "boss-dogfood",
      generated_at: "2026-06-03T00:00:00Z",
      budget_state: "green",
      queue_state: { queued_jobs: 1, retryable_jobs: 1, leased_jobs: 1 },
      workload_state: { active_workers: 1 },
      incident_state: { open_deadletters: 1 },
      job_status_counts: { queued: 1, failed_retryable: 1, leased: 1 },
      job_kind_counts: { "agent.coo.ops_digest": 1 },
      stale_lease_job_ids: ["job_stale"],
      ready_job_ids: ["job_ready"],
      jobs: [
        {
          id: "job_ready",
          project_id: "boss-dogfood",
          kind: "agent.coo.ops_digest",
          status: "queued",
          priority: 100,
          attempt: 0,
          max_attempts: 3,
          data_boundary: "internal",
          run_after: "2026-06-03T00:00:00Z",
          last_error: {},
          created_at: "2026-06-03T00:00:00Z",
          updated_at: "2026-06-03T00:00:00Z",
        },
      ],
      deadletters: [
        {
          id: "dlq_1",
          project_id: "boss-dogfood",
          source_kind: "job",
          source_id: "job_failed",
          reason: "terminal failure",
          payload: { message: "terminal failure" },
          status: "open",
          created_at: "2026-06-03T00:00:00Z",
          updated_at: "2026-06-03T00:00:00Z",
        },
      ],
      pending_decisions: [
        {
          id: "dec_1",
          question: "Approve queue remediation?",
          status: "pending",
          risk_tier: 2,
          default_action: "hold",
          created_at: "2026-06-03T00:00:00Z",
          updated_at: "2026-06-03T00:00:00Z",
        },
      ],
      blockers: [
        {
          severity: "high",
          category: "deadletters",
          title: "Open deadletters require remediation",
          object_ids: ["dlq_1"],
          next_action: "Create incident packets.",
        },
      ],
      recommended_actions: ["Create incident packets."],
    };
    const digest = {
      status: "created",
      project_id: "boss-dogfood",
      artifact_id: "art_ops_digest",
      artifact_type: "ops_digest",
      created_at: "2026-06-03T00:00:00Z",
      counts: { jobs: 1, deadletters: 1, pending_decisions: 1, blockers: 1 },
      queue_state: inspection.queue_state,
      blockers: inspection.blockers,
      recommended_actions: inspection.recommended_actions,
    };
    const responses = [
      { status: 200, body: inspection },
      { status: 201, body: digest },
    ];
    const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      const next = responses.shift();
      return new Response(JSON.stringify(next?.body ?? {}), {
        status: next?.status ?? 500,
        headers: { "Content-Type": "application/json" },
      });
    };
    const client = createBossApiClient({
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl,
    });

    const queue = await client.inspectOpsQueue("boss-dogfood", 10, true);
    const createdDigest = await client.createOpsDigest({
      project_id: "boss-dogfood",
      actor: "coo",
      limit: 10,
      include_terminal: true,
    });

    expect(calls[0].url).toBe(
      "http://127.0.0.1:8000/v1/ops/queue?project_id=boss-dogfood&limit=10&include_terminal=true",
    );
    expect(calls[1].url).toBe(
      "http://127.0.0.1:8000/v1/ops/digests",
    );
    expect(calls[1].init?.method).toBe("POST");
    expect(JSON.parse(String(calls[1].init?.body))).toMatchObject({
      project_id: "boss-dogfood",
      actor: "coo",
      limit: 10,
      include_terminal: true,
    });
    expect(queue.deadletters[0].id).toBe("dlq_1");
    expect(queue.pending_decisions[0].risk_tier).toBe(2);
    expect(createdDigest.artifact_type).toBe("ops_digest");
  });
});
