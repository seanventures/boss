/**
 * The scorecard draws the first twelve agents. When the payload has more, it
 * says how many are on the page and how many are not. Twelve or fewer stays quiet.
 */
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { ReportFeed } from "../lib/reports";

(globalThis as Record<string, unknown>).React = React;

const { ReportsView } = await import("../app/reports-ui");

function agent(n: number) {
  const id = String(n).padStart(2, "0");
  return {
    role_key: `role-${id}`,
    display_name: `Agent ${id}`,
    trust_score: 50,
    autonomy_level: 1,
    successful_runs: n,
    policy_violations: 0,
    scope_violations: 0,
    last_reviewed_at: null,
    last_violation_at: null,
  };
}

function report(count: number): ReportFeed {
  const window = {
    tasks: 0,
    jobs: 0,
    jobs_succeeded: 0,
    jobs_failed: 0,
    decisions: 0,
    decisions_approved: 0,
    spend_usd_micros: 0,
    by_kind: [] as { kind: string; total: number; succeeded: number; failed: number; avg_seconds: number }[],
    by_model: [] as { model: string; count: number; spend_usd_micros: number }[],
    decision_latency: { count: 0, avg_seconds: 0 },
  };
  return {
    apiReachable: true,
    data: {
      project_id: "p",
      generated_at: "2026-01-01T00:00:00Z",
      windows: { today: window, week: window, month: window },
      daily: [],
      by_kind: [],
      by_model: [],
      agents: Array.from({ length: count }, (_, i) => agent(i + 1)),
      decision_latency: { count: 0, avg_seconds: 0 },
    },
  };
}

function html(count: number): string {
  return renderToStaticMarkup(<ReportsView report={report(count)} />);
}

function rowKeys(markup: string): string[] {
  return [...markup.matchAll(/class="mono"> (role-\d+)</g)].map((m) => m[1]!);
}

describe("the agent scorecard says how many are not on the page", () => {
  it("says twelve are on the page and how many more are not", () => {
    const markup = html(15);
    const note = markup.match(/<p[^>]*>[^<]*more are not\.[^<]*<\/p>/)?.[0] ?? "";
    expect(note).toContain("12 agents are on this page. 3 more are not.");
    expect(rowKeys(markup)).toEqual(Array.from({ length: 12 }, (_, i) => `role-${String(i + 1).padStart(2, "0")}`));
  });

  it("does not say more are hidden when every agent is on the page", () => {
    for (const count of [0, 1, 12]) {
      const markup = html(count);
      expect(markup, String(count)).not.toContain("more are not");
      expect(markup, String(count)).not.toContain("are on this page");
      expect(rowKeys(markup), String(count)).toEqual(
        Array.from({ length: count }, (_, i) => `role-${String(i + 1).padStart(2, "0")}`),
      );
    }
  });
});
