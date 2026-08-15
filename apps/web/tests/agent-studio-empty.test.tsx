import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AgentStudioFeed } from "../lib/agent-studio";

/**
 * BOSS-347 — Agent Studio empty/error honesty.
 *
 * An unseeded library must be a setup page with an install CTA, not a dead
 * "live system" workbench. Schema/API failures must surface the real reason.
 */

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
}));

vi.mock("../lib/client", () => ({
  API_URL: "https://boss-api.test",
  API_KEY: undefined,
  PROJECT_ID: "boss-dogfood",
  resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
  authHeaders: () => ({}) as Record<string, string>,
}));

const { AgentStudio } = await import("../app/agent-studio-ui");
const { fetchAgentStudioFeed } = await import("../lib/agent-studio");
const actionsSource = readFileSync(
  resolve(__dirname, "../app/agent-studio-actions.ts"),
  "utf8",
);

function emptyLibrary(overrides: Partial<AgentStudioFeed> = {}): AgentStudioFeed {
  return {
    projectId: "boss-dogfood",
    apiReachable: true,
    agents: [],
    panels: [],
    workflows: [],
    agentRuns: [],
    panelRuns: [],
    workflowRuns: [],
    tests: [],
    a2a: {
      channels: [],
      messages: [],
      health: { status: "unknown" },
    },
    ...overrides,
  };
}

function html(feed: AgentStudioFeed): string {
  return renderToStaticMarkup(
    <AgentStudio feed={feed} roles={[]} skills={[]} />,
  );
}

describe("Agent Studio empty library is a setup page, not a live workbench", () => {
  it("names the missing starter catalog and offers the no-spend install CTA", () => {
    const markup = html(emptyLibrary());

    expect(markup).toContain("Starter catalog not installed");
    expect(markup).toContain("Install BOSS starter catalog");
    expect(markup).toContain("POST /v1/agent-studio/catalog/seed");
    expect(markup).toContain("as-first-run");
    expect(markup).toContain("never dispatches work or spends budget");
    expect(markup).not.toContain("Agent Studio / live system");
    expect(markup).not.toContain("Compose intelligence. Watch the work.");
    expect(markup).not.toContain("as-workbench");
    expect(markup).not.toContain("as-metrics");
    expect(markup).not.toContain("Live coordination");
    expect(markup).not.toContain("Smoke test all");
  });

  it("keeps the workbench only after specialists exist", () => {
    const markup = html(
      emptyLibrary({
        agents: [
          {
            id: "agt_1",
            agent_key: "ux_engineer",
            name: "UX Engineer",
            owner: "vp_agents",
            status: "active",
            active_run_count: 0,
            active_version: {
              id: "agv_1",
              version: 1,
              role_key: "ux_engineer",
              model_policy: { execution_mode: "deterministic_brief" },
              requested_skills: [],
              scopes: { data_boundary: "internal" },
              validation: { valid: true, errors: [], warnings: [] },
            },
          },
        ],
      }),
    );

    expect(markup).toContain("Agent Studio / live system");
    expect(markup).toContain("as-workbench");
    expect(markup).toContain("UX Engineer");
    expect(markup).toContain("Smoke test all 1 agents");
    expect(markup).not.toContain("Starter catalog not installed");
  });

  it("treats unconfigured A2A as quiet honesty, not a red outage", () => {
    const markup = html(
      emptyLibrary({
        agents: [
          {
            id: "agt_1",
            agent_key: "ux_engineer",
            name: "UX Engineer",
            owner: "vp_agents",
            status: "active",
            active_run_count: 0,
          },
        ],
        a2a: {
          channels: [],
          messages: [],
          health: {
            status: "not_configured",
            transport: { configured: false, status: "not_configured" },
          },
        },
      }),
    );
    expect(markup).toContain("not_configured");
    expect(markup).toContain("as-beacon quiet");
    expect(markup).not.toContain("as-beacon bad");
  });

  it("shows an honest unavailable state instead of a seed CTA that cannot work", () => {
    const markup = html(
      emptyLibrary({
        apiReachable: false,
        blockedReason:
          "Agent Studio schema is not applied: agent_definitions",
      }),
    );

    expect(markup).toContain("Agent Studio control plane is unavailable");
    expect(markup).toContain("Agent Studio schema is not applied: agent_definitions");
    expect(markup).not.toContain("Install BOSS starter catalog");
    expect(markup).not.toContain("as-workbench");
  });
});

describe("Agent Studio catalog feed error honesty", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("surfaces schema-missing 503 detail instead of a generic status code", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: false,
        status: 503,
        json: async () => ({
          detail: "Agent Studio schema is not applied: agent_definitions",
        }),
      })),
    );

    const feed = await fetchAgentStudioFeed("boss-dogfood");

    expect(feed.apiReachable).toBe(false);
    expect(feed.agents).toEqual([]);
    expect(feed.blockedReason).toBe(
      "Agent Studio schema is not applied: agent_definitions",
    );
    expect(feed.blockedReason).not.toBe("Agent Studio API returned 503.");
  });

  it("falls back to the status code when the error body has no detail", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: false,
        status: 500,
        json: async () => ({}),
      })),
    );

    const feed = await fetchAgentStudioFeed("boss-dogfood");

    expect(feed.apiReachable).toBe(false);
    expect(feed.blockedReason).toBe("Agent Studio API returned 500.");
  });
});

describe("starter catalog seed action maps schema failures", () => {
  it("treats HTTP 503 as a schema-not-applied operator message", () => {
    expect(actionsSource).toContain("response.status === 503");
    expect(actionsSource).toContain("schema is not applied");
  });
});
