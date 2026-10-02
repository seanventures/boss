/**
 * Factory: Retry only where it can help, an answer when it is pressed, and panel cards named
 * for what was asked.
 *
 * "Retry build" showed on completed runs, with a tooltip saying it "resumes from where it
 * failed"; one click spent a new governed attempt ($4-15) re-doing finished work, and the
 * compact form rendered neither the started run nor the refusal. Panel cards showed "--- WHAT
 * THIS PANEL IS FOR --- You are the panel…" because the title was the scaffolding's opening.
 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";
import {
  directiveSubject,
  displayTitle,
  PANEL_ASK_MARK,
  PANEL_PURPOSE_MARK,
  PANEL_RESEARCH_MARK,
} from "../lib/directive-subject";
// Loaded once, at collection: importing this large module inside a test ran past the default
// timeout under a full parallel run.
import { failingGatesOf, isRetryable, leadLine } from "../app/code-tasks-ui";

describe("Retry build", () => {
  it("is offered on failed and gate-failed runs, never on a clean finished one", async () => {

    expect(isRetryable({ status: "failed" })).toBe(true);
    expect(isRetryable({ status: "blocked" })).toBe(true);
    expect(isRetryable({ status: "running" })).toBe(false);
    expect(isRetryable({ status: "queued" })).toBe(false);
    expect(isRetryable({ status: "completed" })).toBe(false);
    expect(isRetryable({ status: "completed",
                         finalize_ctx: { advisory_failing_gates: ["tests_strong_enough"] } }))
      .toBe(true);
  });

  it("agrees with the engine when the advisory list is empty but gates failed", () => {
    // refuse_finished_retry reads `advisory_failing_gates or failing_gates`; an empty list
    // falls through. The console used `??`, which does not, and hid a Retry the engine allows.
    const ctx = { advisory_failing_gates: [], failing_gates: ["written_tests_pass"] };
    expect(isRetryable({ status: "completed", finalize_ctx: ctx })).toBe(true);
    expect(failingGatesOf(ctx)).toEqual(["written_tests_pass"]);
    expect(isRetryable({ status: "completed",
                         finalize_ctx: { advisory_failing_gates: [], failing_gates: [] } }))
      .toBe(false);
    const engine = readFileSync("../../engine/boss_engine/services/code_task_service.py", "utf8");
    expect(engine).toContain(
      'fctx.get("advisory_failing_gates") or fctx.get("failing_gates")');
    expect(codeOnly("app/code-tasks-ui.tsx")).not.toMatch(/advisory_failing_gates\s*\?\?/);
  });

  it("the action bar uses that rule", () => {
    const ui = codeOnly("app/code-tasks-ui.tsx");
    expect(ui).toContain("{isRetryable(active) ? (");
  });

  it("the compact form says what happened", () => {
    const ui = codeOnly("app/code-tasks-ui.tsx");
    const compact = ui.slice(ui.indexOf("if (compact) {"), ui.indexOf("return (", ui.indexOf("if (compact) {") + 30) + 2000);
    expect(compact).toContain('data-testid="ct-retry-started"');
    expect(compact).toContain('data-testid="ct-retry-error"');
  });
});

describe("a panel is named for what was asked", () => {
  const wrapped = `PRD meeting: ${PANEL_PURPOSE_MARK} You are the panel inside BOSS's Standard `
    + `workflow. ${PANEL_ASK_MARK} Build an incident timeline builder. ${PANEL_RESEARCH_MARK} x`;

  it("strips the scaffolding and the PRD-meeting prefix", () => {
    expect(directiveSubject(wrapped)).toBe("Build an incident timeline builder.");
  });

  it("a title cut short inside the scaffolding falls back instead of showing it", () => {
    // boss-1: "PRD meeting: --- WHAT THIS PANEL IS FOR --- You are the pane…"
    expect(displayTitle(`PRD meeting: ${PANEL_PURPOSE_MARK} You are the pane…`))
      .toBe("Untitled panel");
  });

  it("uses the engine's own markers", () => {
    const engine = readFileSync("../../engine/boss_engine/services/meeting_service.py", "utf8");
    for (const mark of [PANEL_PURPOSE_MARK, PANEL_ASK_MARK, PANEL_RESEARCH_MARK]) {
      expect(engine).toContain(`"${mark}"`);
    }
  });

  it("the Factory cards and the Approvals evidence chip go through it", async () => {

    expect(leadLine(wrapped)).toBe("Build an incident timeline builder.");
    expect(codeOnly("app/decision-inbox-ui.tsx")).toContain("displayTitle(linkedTask.title");
  });
});

it("the Community Factory screen is called Factory, as the rail calls it", () => {
  // The private repo's page-community.tsx ships as this tree's page.tsx (page_export_rewrite).
  const page = readFileSync("app/page.tsx", "utf8");
  expect(page).toContain('data-area="code_tasks" aria-label="Factory"');
});
