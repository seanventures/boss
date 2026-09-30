import { renderToStaticMarkup } from "react-dom/server";
import React from "react";
import { describe, expect, it } from "vitest";

import { AssuranceCenter } from "../app/assurance-ui";

describe("Assurance Center", () => {
  it("renders measured truth, proof, ownership, verification, and spend", () => {
    const html = renderToStaticMarkup(
      <AssuranceCenter
        feed={{
          projectId: "project-1",
          apiReachable: true,
          overview: {
            project_id: "project-1",
            metrics: [{ id: "metric-1", health_status: "breached" }],
            cases: [{ id: "case-1", title: "Release proof", status: "accepted" }],
            surfaces: [{ id: "surface-1", status: "active" }],
            verification_runs: [{ id: "run-1", outcome: "refuted" }],
            settled_conclusions: [{ id: "conclusion-1", conclusion_key: "proxy-cause", conclusion: "The IP is not the root cause." }],
          },
          modelUsage: {
            project_id: "project-1",
            window_days: 30,
            totals: { calls: 2, total_tokens: 1200, cost_usd_micros: 250000 },
            items: [{
              engine: "openai_api", model: "model-a", calls: 2,
              successful_calls: 2, failed_calls: 0, prompt_tokens: 800,
              completion_tokens: 400, total_tokens: 1200,
              cost_usd_micros: 250000,
            }],
          },
        }}
      />,
    );
    expect(html).toContain("Assurance Center");
    expect(html).toContain("Release proof");
    expect(html).toContain("The IP is not the root cause.");
    expect(html).toContain("openai_api / model-a");
    expect(html).toContain("1 currently breached");
    expect(html).toContain("1 refuted or failed");
  });

  it("explains empty proof gates and offers a labeled demo seed", () => {
    const html = renderToStaticMarkup(
      <AssuranceCenter
        feed={{
          projectId: "project-1",
          apiReachable: true,
          overview: {
            project_id: "project-1",
            metrics: [],
            cases: [],
            surfaces: [],
            verification_runs: [],
            settled_conclusions: [],
          },
          modelUsage: {
            project_id: "project-1",
            window_days: 30,
            totals: {},
            items: [],
          },
        }}
      />,
    );
    expect(html).toContain("No cases recorded yet.");
    expect(html).toContain("measured contract");
    expect(html).toContain("POST /v1/assurance/metrics");
    expect(html).toContain("docs/api/ASSURANCE_GOVERNANCE.md");
    expect(html).toContain("Seed one demo metric + case");
    expect(html).toContain("seeded: true");
    expect(html).toContain("not production health");
    expect(html).toContain("not a healthy production claim");
  });
});
