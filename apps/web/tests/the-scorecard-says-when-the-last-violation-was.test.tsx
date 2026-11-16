/**
 * The scorecard says when an agent's last violation was.
 * A missing time adds no extra words.
 */
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { ReportFeed } from "../lib/reports";

(globalThis as Record<string, unknown>).React = React;

const { ReportsView } = await import("../app/reports-ui");

function agent(lastViolationAt: string | null) {
  return {
    role_key: "role-01",
    display_name: "Agent 01",
    trust_score: 50,
    autonomy_level: 1,
    successful_runs: 1,
    policy_violations: 1,
    scope_violations: 0,
    last_reviewed_at: null,
    last_violation_at: lastViolationAt,
  };
}

function report(lastViolationAt: string | null): ReportFeed {
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
      agents: [agent(lastViolationAt)],
      decision_latency: { count: 0, avg_seconds: 0 },
    },
  };
}

function html(lastViolationAt: string | null): string {
  return renderToStaticMarkup(<ReportsView report={report(lastViolationAt)} />);
}

describe("the scorecard says when the last violation was", () => {
  it("shows the formatted day when the agent has a last violation time", () => {
    expect(html("2026-04-02T15:04:00Z")).toContain("Last violation Apr 2, 2026");
  });

  it("does not say Last violation when the time is missing", () => {
    expect(html(null)).not.toContain("Last violation");
  });
});
