/**
 * THE FACTORY'S SERVER RENDER SHIPS SLIM ROWS (console latency, 2026-10-09).
 *
 * page.tsx handed CodeTasksHome up to 200 FULL code-task rows — state (stations, logs), usage and
 * the whole finalize_ctx — and React serialised every byte into the HTML: 590 KB per Factory load
 * on boss-1. CodeTasksHome reads a header count, the last spec's owner/repo, the benchmark ids
 * already built and the Apps tab's fallback grouping; the Floor, Apps and the Cockpit read their
 * own endpoints. The slim row keeps exactly what those read, and nothing a fallback needs is lost.
 */
import { readFileSync } from "node:fs";

import type { CodeTaskRun } from "@boss/api-client";
import { describe, expect, it } from "vitest";

import { appsFromRuns } from "../app/factory/apps-tab";
import { slimRunForFactory } from "../lib/code-tasks";

function fullRun(id: string, title: string, extra: Partial<CodeTaskRun> = {}): CodeTaskRun {
  return {
    id, project_id: "p", status: "completed", phase: "done", title,
    created_at: "2026-10-09T10:00:00Z", updated_at: "2026-10-09T10:05:00Z",
    pull_request_url: `https://github.com/acme/app/pull/${id.length}`, pull_request_number: id.length,
    spec: { owner: "acme", repo: "shop", base_branch: "main", instructions: "x".repeat(5000) },
    state: { stations: Array.from({ length: 40 }, (_, i) => ({ id: `s${i}`, log: "y".repeat(500) })) } as never,
    usage: { cost_usd_micros: 12, model: "m" },
    finalize_ctx: { attempt: 2, root_run_id: "crun_root", qa_benchmark: { id: "bench-1" },
                    origin: { title: "Shop" }, pending_diff: "z".repeat(20000), app_id: "app_1" } as never,
    ...extra,
  } as CodeTaskRun;
}

describe("the Factory's server render", () => {
  it("passes slim rows, not whole runs", () => {
    const page = readFileSync(new URL("../app/page.tsx", import.meta.url), "utf8");
    expect(page).toContain("codeTasks={codeTasksFeed.items.map(slimRunForFactory)}");
  });

  it("a slim row is a fraction of the run and keeps every field the Factory reads", () => {
    const run = fullRun("crun_a", "Build the shop");
    const slim = slimRunForFactory(run);
    expect(JSON.stringify(slim).length).toBeLessThan(JSON.stringify(run).length / 20);
    expect(slim.spec).toEqual({ owner: "acme", repo: "shop", base_branch: "main" });
    expect(slim.finalize_ctx).toEqual({ attempt: 2, root_run_id: "crun_root", qa_benchmark: { id: "bench-1" },
                                         origin: { title: "Shop" }, app_id: "app_1" });
    for (const k of ["id", "title", "status", "phase", "created_at", "updated_at", "pull_request_url",
                     "pull_request_number"] as const) {
      expect(slim[k]).toEqual(run[k]);
    }
  });

  it("the Apps tab's fallback groups and words slim rows exactly as it did whole runs", () => {
    const runs = [fullRun("crun_a", "Build the shop"),
                  fullRun("crun_b", "Build the shop", { status: "failed", finalize_ctx: { root_run_id: "crun_a" } as never }),
                  fullRun("crun_c", "QA benchmark: Todo", { status: "running" })];
    expect(appsFromRuns(runs.map(slimRunForFactory), [])).toEqual(appsFromRuns(runs, []));
  });
});
