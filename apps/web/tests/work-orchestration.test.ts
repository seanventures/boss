import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

describe("Work Orchestration operator surface", () => {
  const page = readFileSync(resolve(__dirname, "../app/page.tsx"), "utf8");
  const shell = readFileSync(resolve(__dirname, "../app/ConsoleShell.tsx"), "utf8");
  const ui = readFileSync(
    resolve(__dirname, "../app/work-orchestration-ui.tsx"),
    "utf8",
  );
  const actions = readFileSync(
    resolve(__dirname, "../app/work-orchestration-actions.ts"),
    "utf8",
  );
  const css = readFileSync(resolve(__dirname, "../app/globals.css"), "utf8");

  it("makes ranked allocation a first-class Mission Control area", () => {
    expect(shell).toContain('id: "portfolio"');
    expect(shell).toContain('label: "Work Allocation"');
    expect(page).toContain("<WorkOrchestration");
    expect(ui).toContain('aria-label="Ranked portfolio"');
    expect(ui).toContain('aria-label="Capacity and job queue"');
    expect(ui).toContain('aria-label="Tracker and delivery presence"');
  });

  it("lets operators score, allocate, track, and configure delivery presence", () => {
    expect(actions).toContain('post("/v1/work-orchestration/items"');
    expect(actions).toContain('post("/v1/work-orchestration/quotas"');
    expect(actions).toContain('post("/v1/work-orchestration/allocations/plan"');
    expect(actions).toContain('post("/v1/work-orchestration/tracker-bindings"');
    expect(actions).toContain('post("/v1/work-orchestration/bot-identities"');
    expect(actions).toContain('post("/v1/work-orchestration/status-channels"');
  });

  it("uses one restrained signal path and exposes score rationale", () => {
    expect(css).toContain(".wo-ledger");
    expect(css).toContain(".wo-flow-label");
    expect(css).toContain(".wo-inspector");
    // Visibility is generated from the AREAS list now (lib/area-visibility.ts), not hand-written
    // in globals.css; every-area-is-actually-visible.test.ts covers portfolio with the rest.
    expect(readFileSync(resolve(__dirname, "../app/ConsoleShell.tsx"), "utf8"))
      .toContain('{ id: "portfolio", label:');
    expect(ui).toContain("selected.rationale");
    expect(ui).toContain("effective_score.toFixed(1)");
    expect(ui).toContain("Tracker binding required");
  });
});
