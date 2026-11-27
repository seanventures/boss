// @vitest-environment happy-dom
/**
 * AGENT PLATFORM CONSOLE (AP-P3, BOSSAPP-1054; wireframes A1 A3 A4 A5 A6 A7):
 *  - A1 Deployments overview + MCP tools + filter chips
 *  - A3 Passport Deployments (dev/staging deploy, staging->prod promote -> decision, rollback)
 *  - A4 Passport Triggers (5 kinds, schedule preview, match preview, pause/resume, rotate one-time secret, delete)
 *  - A5 Health view (alerts, deployment pause/resume, D7 system services, worker scaling, bot tick)
 *  - A6 Decision Inbox (promote_agent_deployment card + approval effect + checks + in-flight note)
 *  - A7 Passport Endpoints (REST/MCP/A2A cards, A2A toggle, Try-It SSE stream, thread + run + fork)
 *  - GatedButton pre-greying + LockedPanel fail-closed reads
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const denied = new Set<string>();
vi.mock("../components/identity/identity-provider", async (orig) => ({
  ...(await orig<typeof import("../components/identity/identity-provider")>()),
  useCan: (p: string) => (denied.has(p)
    ? { allowed: false, reason: `Needs “${p}”, ask an Admin`, needsStepUp: false }
    : { allowed: true, reason: "", needsStepUp: false }),
}));

import { DeploymentsOverview } from "../app/agents/deployments-overview";
import { HealthView } from "../app/agents/health-view";
import { PassportDeployments } from "../app/agents/passport-deployments";
import { PassportEndpoints } from "../app/agents/passport-endpoints";
import { PassportTriggers } from "../app/agents/passport-triggers";
import { DecisionCard } from "../app/decision-inbox-ui";
import { $, click, mount, settle, unmount } from "./_helpers/live-render";

const PID = "p1";
const ACTOR = "usr_ceo";
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

type Call = { url: string; method: string; body: Record<string, unknown> };

function mockApi(over: (url: string, method: string, body: Record<string, unknown>) => Response | undefined = () => undefined) {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (u: string, init?: RequestInit) => {
    const url = String(u);
    const method = String(init?.method ?? "GET");
    let body: Record<string, unknown> = {};
    if (typeof init?.body === "string" && init.body.trim().startsWith("{")) {
      try { body = JSON.parse(init.body); } catch { body = {}; }
    }
    calls.push({ url, method, body });
    const custom = over(url, method, body);
    if (custom) return custom;

    if (url.startsWith("/v1/agent-platform/agents?")) {
      return json({
        summary: { live_agents: 2, bots_running: 1, need_attention: 1, spend_7d_usd: 14.5 },
        agents: [
          {
            key: "triage", name: "Triage Agent", initials: "TA", kind: "assistant", kind_detail: "Assistant",
            live: [{ environment: "staging", version: 2, deployment_id: "dep_s" }, { environment: "prod", version: 1, deployment_id: "dep_p" }],
            health: { state: "healthy", label: "Healthy" }, cost_7d_usd: 12.0, last_run_at: "2026-11-25T10:00:00Z", runner: "local",
          },
          {
            key: "monitor_bot", name: "Latency Bot", initials: "LB", kind: "bot", kind_detail: "Cron bot",
            live: [{ environment: "prod", version: 1, deployment_id: "dep_b" }],
            health: { state: "degraded", label: "2 failed" }, cost_7d_usd: 2.5, last_run_at: "2026-11-25T09:55:00Z", runner: "local",
          },
        ],
      });
    }
    if (url.startsWith("/v1/agent-platform/mcp-tools?")) {
      return json({ tools: [{ tool: "agent_triage", agent_key: "triage", name: "Triage Agent", version: 1 }] });
    }
    if (url.startsWith("/v1/agents/triage/deployments?")) {
      return json({
        agent_key: "triage",
        environments: [
          { environment: "dev", deployed: false },
          { environment: "staging", deployed: true, deployment_id: "dep_s", version: 2, state: "active", runs_7d: 18, ok_rate: 1.0, cost_7d_usd: 3.0, in_flight: 0, next: { action: "promote", to: "prod" } },
          { environment: "prod", deployed: true, deployment_id: "dep_p", version: 1, state: "active", previous_version: 0, runs_7d: 120, ok_rate: 0.99, cost_7d_usd: 12.0, in_flight: 1 },
        ],
        draft: { version: 3, state: "draft", note: "New prompt" },
        versions: [{ version: 3, active: false, environments: [] }, { version: 2, active: true, environments: ["staging"] }, { version: 1, active: true, environments: ["prod"] }],
        history: [{ id: "dh_1", environment: "prod", action: "deploy", status: "applied", from_version: null, to_version: 1, actor: "usr_ceo", created_at: "2026-11-25T09:00:00Z" }],
      });
    }
    if (url.startsWith("/v1/agents/triage/promotion-checks?")) {
      return json({
        agent_key: "triage", from_environment: "staging", to_environment: "prod", version: 2, live_version: 1,
        checks: [{ key: "eval", ok: true, label: "Staging runs passed", detail: "18/18" }],
        can_promote: true, blocked_by: [], approval: { required: true, who: "CEO" },
        in_flight: { runs: 1, version: 1, note: "1 in-flight run keeps v1" },
      });
    }
    if (url === "/v1/agents/triage/promote" && method === "POST") {
      return json({ status: "awaiting_approval", decision_id: "dec_prom_1" });
    }
    if (url.startsWith("/v1/agents/triage/triggers?")) {
      return json({
        agent_key: "triage",
        kinds: ["cron", "event", "webhook", "monitor", "intake"],
        concurrency: ["skip_if_active", "always_enqueue"],
        triggers: [{
          id: "atrg_w1", agent_key: "triage", environment: "prod", kind: "webhook", kind_label: "Webhook",
          name: "PagerDuty Hook", status: "active", on: true, url_path: "/v1/triggers/wh_1",
          fires: 5, skips: 0, config: {},
        }],
      });
    }
    if (url.startsWith("/v1/agent-triggers/preview?")) {
      return json({ kind: "cron", config: {}, next: [{ at: "2026-11-25T11:00:00Z", label: "In 1 hour" }] });
    }
    if (url === "/v1/agent-triggers/atrg_w1/rotate-secret" && method === "POST") {
      return json({ id: "atrg_w1", name: "PagerDuty Hook", secret: "whsec_ROTATED_ONCE" });
    }
    if (url.startsWith("/v1/intake/connections?")) return json({ connections: [] });
    if (url.startsWith("/v1/agent-platform/health?")) {
      return json({
        hours: 24,
        alerts: [{ deployment_id: "dep_b", agent_key: "monitor_bot", title: "Failure streak", detail: "2 timeouts", since: "2026-11-25T09:50:00Z", trace_run_id: "arun_fail_1" }],
        deployments: [{
          deployment_id: "dep_b", agent_key: "monitor_bot", name: "Latency Bot", environment: "prod", version: 1,
          runner: "local", kind: "bot", state: "degraded", label: "Degraded", runs: 10, ok: 8, failed: 2,
          failure_streak: 2, ok_rate: 0.8, p95_ms: 920, cost_usd: 2.5, last_run_at: "2026-11-25T09:55:00Z",
          interrupt_backlog: 0, stuck_runs: 0, paused: false, triggers: [],
        }],
        system_services: [{ key: "intake_processor", name: "Intake Processor", kind: "service", runs_in: "worker", state: "ok", ok: true, interval_seconds: 15, last_beat_at: "2026-11-25T10:00:00Z", last_ok_at: "2026-11-25T10:00:00Z" }],
        workers: { live_workers: 1, slots: 4, busy_slots: 1, free_slots: 3, queued_runs: 0, waiting_runs: 0, desired_slots: 4, signal: "steady", words: "Steady" },
      });
    }
    if (url.startsWith("/v1/workers/scaling?")) {
      return json({ project_id: PID, live_workers: 1, slots: 4, busy_slots: 1, free_slots: 3, queued_runs: 0, waiting_runs: 0, desired_slots: 4, signal: "steady", words: "Steady", workers: [{ id: "w1", worker_id: "wrk_1", status: "active", liveness: "live", slots: 4, busy_slots: 1 }] });
    }
    if (url.startsWith("/v1/agents/triage/endpoints?")) {
      return json({
        agent_key: "triage", environment: "prod", version: 1,
        rest: { enabled: true, stream: "/v1/agents/triage/runs/stream", wait: "/v1/agents/triage/runs/wait", permission: "agent:invoke" },
        mcp: { enabled: true, tool: "agent_triage", note: "Requires agent:invoke" },
        a2a: { enabled: false, card_path: "/.well-known/agents/triage/agent.json", may_enable: false, note: "Admin grant needed" },
        callers: [{ principal_id: "usr_1", name: "Ops Bot", kind: "service", via: "rest", may: "agent:invoke", last_used_at: "2026-11-25T09:00:00Z" }],
        try_it: "stream",
      });
    }
    if (url === "/v1/agents/triage/runs/stream" && method === "POST") {
      const sse = "event: metadata\ndata: {\"run_id\":\"run_sse_1\",\"thread_id\":\"thr_sse_1\"}\n\nevent: end\ndata: {\"status\":\"completed\"}\n\n";
      return new Response(sse, { status: 200, headers: { "Content-Type": "text/event-stream" } });
    }
    if (url.startsWith("/v1/threads/thr_sse_1/history")) {
      return json({ thread_id: "thr_sse_1", runs: [{ run_id: "run_sse_1", thread_id: "thr_sse_1", status: "completed", agent_key: "triage" }], checkpoints: [] });
    }
    return json({});
  }));
  return calls;
}

beforeEach(() => { denied.clear(); });
afterEach(() => { unmount(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("AP-P3 Agent Platform Console (A1 A3 A4 A5 A6 A7)", () => {
  it("A1 DeploymentsOverview renders KPIs, agents, and MCP tools", async () => {
    mockApi();
    await mount(<DeploymentsOverview projectId={PID} actor={ACTOR} />);
    expect(document.body.textContent).toContain("Triage Agent");
    expect(document.body.textContent).toContain("Latency Bot");
    expect(document.body.textContent).toContain("agent_triage");
  });

  it("A3 PassportDeployments checks promotion and submits promote with signed-in actor", async () => {
    const calls = mockApi();
    await mount(<PassportDeployments projectId={PID} actor={ACTOR} roleKey="triage" />);
    expect(document.body.textContent).toContain("staging");
    expect(document.body.textContent).toContain("prod");
    await click($('[data-testid="ap-check-promote-staging"]')!);
    expect(document.body.textContent).toContain("Staging runs passed");
    await click($('[data-testid="ap-promote-confirm"]')!);
    const post = calls.find((c) => c.url === "/v1/agents/triage/promote" && c.method === "POST")!;
    expect(post.body).toMatchObject({ project_id: PID, actor: ACTOR, from_environment: "staging", to_environment: "prod" });
    expect(document.body.textContent).toContain("dec_prom_1");
  });

  it("A4 PassportTriggers lists webhook trigger, rotates one-time secret, and previews cron schedule", async () => {
    const calls = mockApi();
    await mount(<PassportTriggers projectId={PID} actor={ACTOR} roleKey="triage" />);
    expect($('[data-testid="ap-trig-row-atrg_w1"]')?.textContent).toContain("PagerDuty Hook");
    await click($('[data-testid="ap-trig-rotate-atrg_w1"]')!);
    expect($('[data-testid="ap-trig-secret-once"]')?.textContent).toContain("whsec_ROTATED_ONCE");
    await click($('[data-testid="ap-trig-new-btn"]')!);
    await settle();
    expect($('[data-testid="ap-trig-sched-preview"]')?.textContent).toContain("In 1 hour");
    expect(calls.some((c) => c.url.startsWith("/v1/agent-triggers/preview?"))).toBe(true);
  });

  it("A5 HealthView renders D7 system services, worker scaling, and fails closed on 403", async () => {
    mockApi();
    await mount(<HealthView projectId={PID} actor={ACTOR} />);
    expect(document.body.textContent).toContain("Intake Processor");
    expect(document.body.textContent).toContain("Failure streak");
    unmount();

    mockApi((url) => (url.startsWith("/v1/agent-platform/health?")
      ? json({ detail: "Missing permission agent:read" }, 403)
      : undefined));
    await mount(<HealthView projectId={PID} actor={ACTOR} />);
    expect(document.body.textContent).toContain("Locked");
  });

  it("A6 DecisionCard renders promote_agent_deployment effect, checks, and in-flight count", async () => {
    await mount(
      <DecisionCard
        projectId={PID}
        actorId={ACTOR}
        decision={{
          id: "dec_prom_1",
          project_id: PID,
          task_id: null,
          decision_type: "deployment_promotion",
          question: "Promote triage v2 to prod?",
          reason: "staging_promoted",
          status: "pending",
          created_at: "2026-11-25T10:00:00Z",
          costSummary: {},
          recommendation: {
            action: "promote_agent_deployment",
            agent_key: "triage",
            from_environment: "staging",
            to_environment: "prod",
            version: 2,
            checks: [{ ok: true, label: "Staging eval passed" }],
            in_flight: { runs: 3, note: "3 in-flight runs keep v1" },
          },
        } as never}
      />,
    );
    expect(document.body.textContent).toContain("Promote triage v2 (staging → prod)");
    expect(document.body.textContent).toContain("Promotion checks passed");
    expect(document.body.textContent).toContain("3 in-flight run(s) keep current version");
    expect(document.body.textContent).toContain("Approving promotes this agent version to prod");
  });

  it("A7 PassportEndpoints streams Try-It SSE frames and pre-greys actions for viewers", async () => {
    const calls = mockApi();
    await mount(<PassportEndpoints projectId={PID} actor={ACTOR} roleKey="triage" />);
    expect($('[data-testid="ap-ep-a2a-note"]')?.textContent).toContain("Admin grant needed");
    await click($('[data-testid="ap-ep-run"]')!);
    expect(calls.some((c) => c.url === "/v1/agents/triage/runs/stream" && c.body.actor === ACTOR)).toBe(true);
    expect($('[data-testid="ap-ep-stream-frames"]')?.textContent).toContain("run_sse_1");
    unmount();

    denied.add("agent:invoke");
    mockApi();
    await mount(<PassportEndpoints projectId={PID} actor={ACTOR} roleKey="triage" />);
    expect($('[data-testid="ap-ep-run"]')?.getAttribute("aria-disabled")).toBe("true");
  });
});
