import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/client", () => ({
  PROJECT_ID: "boss-dogfood",
  resolveProjectId: (id?: string) => id || "boss-dogfood",
  client: {
    getQaBenchmarkApps: vi.fn(),
    getQaBenchmarkReport: vi.fn(),
  },
}));

describe("official QA scorecard island", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("is mounted on the reports area", () => {
    const page = readFileSync(resolve(__dirname, "../app/page.tsx"), "utf8");
    expect(page).toContain("<QaScorecardIsland");
    expect(page).toContain("fetchQaScorecardFeed");
  });

  it("never invents scores when evidence is missing", async () => {
    const { client } = await import("../lib/client");
    vi.mocked(client.getQaBenchmarkApps).mockResolvedValueOnce({
      benchmark_apps: [
        {
          id: "bugbox",
          name: "BugBox",
          tagline: "",
          summary: "",
          focus: [],
          acceptance_scenarios: [],
          completion_contract: [],
          verification_commands: [],
          scoring_dimensions: [],
          score_baselines: {},
          built: { run_id: "crun_1", reportable: true, run_status: "running" },
        },
      ],
    });
    vi.mocked(client.getQaBenchmarkReport).mockResolvedValueOnce({
      run_id: "crun_1",
      run_status: "running",
      completion_contract: [],
      required_verification_commands: [],
      results: [],
      passed: false,
      honesty: {
        honest: true,
        scored: ["speed"],
        missing_evidence: [{ id: "ui", reason: "not attached" }],
        invented_scores: [],
      },
      scorecard: {
        version: "1.1",
        baselines: {},
        dimensions: [],
        observed_weight: 0,
        evidence_coverage_pct: 0,
        overall_score: null,
        overall_status: "provisional",
        tuning_note: "",
      },
      message: "",
    });
    const { fetchQaScorecardFeed } = await import("../lib/qa-scorecard");
    const feed = await fetchQaScorecardFeed("boss-dogfood");
    expect(feed.apiReachable).toBe(true);
    expect(feed.apps[0]?.honest).toBe(true);
    expect(feed.apps[0]?.passed).toBe(false);
    expect(feed.apps[0]?.scored).toEqual(["speed"]);
    expect(feed.apps[0]?.invented).toEqual([]);
    expect(feed.apps[0]?.ui).toBeNull();
  });
});
