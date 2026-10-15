// @vitest-environment happy-dom
/**
 * A workflow can be run as a TEST with pinned data, and a finished run can be run again from a
 * step (#512). The engine decides (services/workflow_test_runs.py); the designer says first what
 * a test will do with each step, sends exactly the pins and live steps the person chose, and the
 * run view labels a test and offers "Run again from here" on a finished run.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Capability, SpecStep } from "../lib/workflow-editor-types";
import {
  isTestRun, parsePin, pinnedOutput, rerunNote, testRunRequest, testRunRows, wouldRefuse,
} from "../lib/workflow-test-run";
import { $, cleanup, click, render, settle, type } from "./_helpers/kit-render";
import { codeOnly } from "./_helpers/source";

const cap = (key: string, risk_tier: number, extra: Partial<Capability> = {}): Capability => ({
  key, label: key, summary: "", risk_tier, required_inputs: [], optional_inputs: [],
  produces: [], skills: [], ...extra,
});
const CAPS = [cap("report", 0), cap("build", 1), cap("spec_review", 2, { is_gate: true })];
const STEPS: SpecStep[] = [
  { id: "build", kind: "capability", binding: { capability: "build" }, next: ["check"] },
  { id: "check", kind: "condition", next: [] },
  { id: "report", kind: "capability", binding: { capability: "report" } },
  { id: "approve", kind: "capability", binding: { capability: "spec_review" } },
  { id: "call", kind: "task" },
];

describe("a pin", () => {
  it("is empty, or a JSON object", () => {
    expect(parsePin("")).toEqual({ ok: true, value: null });
    expect(parsePin(' {"answer": 42} ')).toEqual({ ok: true, value: { answer: 42 } });
    expect(parsePin("[1]").ok).toBe(false);
    expect(parsePin("42").ok).toBe(false);
    expect(parsePin("{nope").ok).toBe(false);
    expect(parsePin(JSON.stringify({ x: "y".repeat(70_000) })).ok).toBe(false);
  });
});

describe("what a test does with each step", () => {
  it("refuses what could spend, runs what only reads, never pins a control step", () => {
    const rows = Object.fromEntries(testRunRows(STEPS, CAPS).map((r) => [r.id, r]));
    expect(rows.build.refusedBecause).toMatch(/spends/);
    expect(rows.call.refusedBecause).toMatch(/task/);
    expect(rows.report.refusedBecause).toBeNull();
    expect(rows.approve.refusedBecause).toBeNull();
    expect(rows.check.pinnable).toBe(false);
  });

  it("sends exactly the pins and live steps chosen, and says what it would still refuse", () => {
    const rows = testRunRows(STEPS, CAPS);
    const built = testRunRequest(rows, {
      build: { text: '{"pr": 1}', live: false },
      call: { text: "", live: true },
      check: { text: '{"ignored": true}', live: false },
    });
    expect(built).toEqual({ ok: true, request: { pins: { build: { pr: 1 } }, allow_live: ["call"] } });
    if (built.ok) expect(wouldRefuse(rows, built.request)).toEqual([]);
    const bare = testRunRequest(rows, {});
    if (bare.ok) expect(wouldRefuse(rows, bare.request).map((r) => r.id)).toEqual(["build", "call"]);
    expect(testRunRequest(rows, { build: { text: "[", live: false } }).ok).toBe(false);
  });

  it("reads a run's mode, provenance and pinned outputs", () => {
    expect(isTestRun({ mode: "test" })).toBe(true);
    expect(isTestRun({ mode: "normal" })).toBe(false);
    expect(rerunNote({ rerun_of: { run_id: "wfr_1", from_step: "build" } }))
      .toBe("Run again from build of wfr_1");
    expect(rerunNote({})).toBeNull();
    expect(pinnedOutput({ pinned: true, x: 1 })).toBe(true);
    expect(pinnedOutput({ x: 1 })).toBe(false);
  });
});

describe("the designer's Test run panel", () => {
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it("posts the test with its pins and says what it refuses", async () => {
    const calls: Array<{ url: string; body: Record<string, unknown> }> = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url: String(url), body: JSON.parse(String(init?.body ?? "{}")) });
      return new Response(JSON.stringify({ id: "wfr_t", status: "running" }), { status: 201 });
    }));
    const started = vi.fn();
    const { TestRunPanel } = await import("../app/workflows/editor/test-run-panel");
    await render(<TestRunPanel projectId="p1" workflowKey="wf" version={3} steps={STEPS}
                               capabilities={CAPS} dirty={false} onClose={() => {}}
                               onStarted={started} />);
    expect($('[data-testid="wf-test-run-step-build"]')?.dataset.outcome).toBe("refused");
    expect($('[data-testid="wf-test-run-refused"]')?.textContent).toMatch(/build/);
    expect($('[data-testid="wf-test-run-pin-check"]')).toBeNull();
    await type('[data-testid="wf-test-run-pin-build"]', '{"pr": 7}');
    expect($('[data-testid="wf-test-run-step-build"]')?.dataset.outcome).toBe("pinned");
    await click('[data-testid="wf-test-run-live-call"]');
    expect($('[data-testid="wf-test-run-refused"]')).toBeNull();
    await click('[data-testid="wf-test-run-start"]');
    await settle();
    const sent = calls.find((c) => c.url.includes("/api/delivery-workflows"));
    expect(sent?.body).toMatchObject({
      action: "test_run", project_id: "p1", workflow_key: "wf", version: 3,
      pins: { build: { pr: 7 } }, allow_live: ["call"],
    });
    expect(started).toHaveBeenCalledWith("wfr_t");
  });
});

describe("the run view and the proxy", () => {
  const UI = codeOnly("app/delivery-workflows-ui.tsx");
  const ROUTE = codeOnly("app/api/delivery-workflows/route.ts");

  it("labels a test run and offers Run again from here on a finished run", () => {
    expect(UI).toContain('data-testid="dw-run-test"');
    expect(UI).toMatch(/terminal && onRerunFrom/);
    expect(UI).toContain('"rerun_from"');
    expect(UI).toContain('showTests ? "runs_with_tests" : "runs"');
  });

  it("proxies the two new doors to their own endpoints", () => {
    expect(ROUTE).toContain('test_run: "/v1/delivery-workflows/test-runs"');
    expect(ROUTE).toMatch(/rerun_from: \(id\) => `\/v1\/delivery-workflows\/runs\/\$\{encodeURIComponent\(id\)\}\/rerun`/);
    expect(ROUTE).toContain("include_test=true");
  });
});
