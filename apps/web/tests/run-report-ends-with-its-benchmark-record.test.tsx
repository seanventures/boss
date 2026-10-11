import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { BenchmarkRecord, benchmarkRows, type Benchmark } from "../app/run-report-ui";
import { codeOnly } from "./_helpers/source";

(globalThis as Record<string, unknown>).React = React;

/** Every run ends with one benchmark record (services/benchmark.py): measured values only, and
 *  a value BOSS did not measure says so — never a zero standing in for "unknown". */
const REC: Benchmark = {
  scenario: "HelpDesk AI", stack: "react-vite+fastapi+postgres", source_commit: null,
  models: ["claude-sonnet-5"], run_id: "wfr_1", result: "failure", pr_url: null,
  tests_passed: null, tests_total: null, human_approvals: 2, manual_interventions: 0,
  elapsed_minutes: 61.5, model_cost_usd: 26.04, retries: 3, deployment_smoke_test: "not-run",
};

describe("the run report's benchmark record", () => {
  it("shows every field, and says not measured rather than inventing a value", () => {
    const rows = Object.fromEntries(benchmarkRows(REC));
    expect(rows.Stack).toBe("react-vite+fastapi+postgres");
    expect(rows.Tests).toBe("not measured");
    expect(rows["Source commit"]).toBe("not measured");
    expect(rows["Manual interventions"]).toBe("0");
    expect(rows["Model cost"]).toBe("$26.04");
    expect(rows["Deployment smoke test"]).toBe("not-run");
    expect(Object.keys(rows)).toHaveLength(14);
  });

  it("is drawn at the end of the report", () => {
    expect(renderToStaticMarkup(<BenchmarkRecord benchmark={REC} />)).toContain("Benchmark record");
    const src = codeOnly("app/run-report-ui.tsx");
    expect(src.lastIndexOf("<BenchmarkRecord")).toBeGreaterThan(src.lastIndexOf("report.contributors"));
  });
});
