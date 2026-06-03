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
