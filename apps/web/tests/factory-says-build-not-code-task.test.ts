import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * The console calls them Projects.
 *
 * "Code task" is what the ENGINE calls the row — `code_task_runs`, `/v1/code-tasks`, `crun_…`
 * — and those names stay exactly as they are. Renaming an identifier or a route because a
 * label changed is how a rename turns into an outage.
 *
 * So this asserts the split: no visible copy says "code task", and every identifier still
 * does.
 *
 * THE AREA IS "Code Tasks" (BOSSAPP-914, operator 2026-11-06: "Factory → Code Tasks"). That is the
 * place's proper name, capitalised; a row in it is still a build, never "a code task".
 */
const FILES = [
  "app/ConsoleShell.tsx", "app/code-tasks-ui.tsx", "app/page.tsx",
  "app/decision-inbox-ui.tsx", "app/agent-ui.tsx", "app/sandboxes-ui.tsx",
  "app/pipeline-ui.tsx", "app/onboarding-help-card.tsx", "app/ask-boss-hero.tsx",
];

/** Source with comments stripped — comments record history and keep the old wording. */
const visible = (f: string) => codeOnly(f);

describe("the visible copy", () => {
  it.each(FILES)("%s never says code task", (file) => {
    const src = visible(file)
      // Identifiers, routes and area keys are not copy.
      .replace(/data-testid="[^"]*"/g, "")
      .replace(/className="[^"]*"/g, "")
      .replace(/area="code_tasks"/g, "")
      .replace(/data-area="code_tasks"/g, "")
      .replace(/id: "code_tasks"/g, "")
      .replace(/\/v1\/code-tasks[^"'`]*/g, "")
      .replace(/code_task[a-z_]*/g, "")
      .replace(/CodeTask[A-Za-z]*/g, "")
      // The area's proper name (BOSSAPP-914). Only the capitalised name; "a code task" still fails.
      .replace(/Code Tasks/g, "");
    expect(src).not.toMatch(/code tasks?/i);
  });

  it("names the primary action New build", () => {
    expect(visible("app/code-tasks-ui.tsx")).toContain("+ New build");
  });

  it("labels the nav area Code Tasks (was Factory, BOSSAPP-914)", () => {
    expect(visible("app/ConsoleShell.tsx")).toContain('label: "Code Tasks"');
  });
});

describe("the machinery keeps its own names", () => {
  it("still routes on the code_tasks area id", () => {
    // The area key is a URL and a router key, not a label. Renaming it would break every
    // deep link the console has ever produced.
    expect(visible("app/ConsoleShell.tsx")).toContain('id: "code_tasks"');
    expect(visible("app/page.tsx")).toContain('data-area="code_tasks"');
  });

  it("still calls the API by its real client method", () => {
    // The generated client keeps the engine's vocabulary; only the label changed.
    expect(codeOnly("lib/code-tasks.ts")).toContain("getCodeTasks");
  });

  it("keeps the CodeTaskRun type", () => {
    expect(visible("app/code-tasks-ui.tsx")).toContain("CodeTaskRun");
  });
});
