import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join, resolve } from "node:path";

import { describe, expect, it } from "vitest";

describe("BOSS mission-control design system", () => {
  const css = readFileSync(resolve(__dirname, "../app/design-system.css"), "utf8");
  const shell = readFileSync(resolve(__dirname, "../app/ConsoleShell.tsx"), "utf8");
  const page = readFileSync(resolve(__dirname, "../app/page.tsx"), "utf8");
  const operations = readFileSync(
    resolve(__dirname, "../app/mission-operations-ui.tsx"),
    "utf8",
  );
  const providerConfiguration = readFileSync(
    resolve(__dirname, "../app/provider-configuration-ui.tsx"),
    "utf8",
  );

  it("defines the approved foundations", () => {
    expect(css).toContain("--void: #090d17");
    expect(css).toContain("--teal: #2ec5c5");
    expect(css).toContain("--green: #34d399");
    expect(css).toContain("--amber: #f5b544");
    expect(css).toContain("--red: #f2647a");
    expect(css).toContain('"Sora"');
    expect(css).toContain('"Manrope"');
    expect(css).toContain('"JetBrains Mono"');
  });

  it("uses the supplied BOSS mark and an icon library", () => {
    expect(shell).toContain('src="/boss-mark.png"');
    expect(shell).not.toContain('src="/BOSS-logo.png"');
    expect(shell).toContain('from "@phosphor-icons/react"');
    expect(shell).not.toContain("<svg");
  });

  it("shows the current mark EVERYWHERE, not just in the shell", () => {
    // This assertion used to cover the shell alone, so the org gate — the very first screen
    // anyone sees, and the one the reconnect banner renders on — kept the retired "Business
    // Operating System Sentinel" icon for weeks after the rebrand. Checking one file proves
    // one file. The rule is about the product, so the test reads the whole app.
    const appDir = resolve(__dirname, "../app");
    const offenders = readdirSync(appDir, { recursive: true } as never)
      .filter((f) => typeof f === "string" && /\.(tsx|ts|css)$/.test(f as string))
      .filter((f) => readFileSync(join(appDir, f as string), "utf8").includes("BOSS-logo.png"));
    expect(offenders).toEqual([]);
  });

  it("shows what the work must do and how it is proven, in ONE component", () => {
    // A benchmark card and a governed task answer the same two questions — acceptance
    // criteria, and the commands that verify them. They were rendered two different ways, so
    // the newer screen looked like a different product and the task list hid the only content
    // that says what a task was for. `spec.acceptance_criteria` and `spec.test_commands` were
    // already on every task; nothing had to be invented, only shown.
    const codeTasks = readFileSync(resolve(__dirname, "../app/code-tasks-ui.tsx"), "utf8");
    expect(codeTasks).toContain("function ContractDetails");
    // Used by the benchmark catalogue AND by the task cards — three call sites, one renderer.
    expect(codeTasks.split("<ContractDetails").length - 1).toBeGreaterThanOrEqual(3);
    expect(codeTasks).toContain("criteria={c.spec?.acceptance_criteria}");
    expect(codeTasks).toContain("verification={c.spec?.test_commands}");
    // A <details> inside a <button> is invalid markup whose clicks fight each other.
    expect(codeTasks).not.toContain('<button key={c.id} className="ct-card"');
  });

  it("lets you launch an existing task, not only a benchmark", () => {
    // The benchmark cards could start work; the task list could not, so re-running something
    // meant retyping its instructions and acceptance criteria into the New Code Task form —
    // copying data BOSS already held.
    const codeTasks = readFileSync(resolve(__dirname, "../app/code-tasks-ui.tsx"), "utf8");
    expect(codeTasks).toContain("function LaunchTask");
    expect(codeTasks).toContain("<LaunchTask run={c}");            // every task card
    expect(codeTasks).toContain("<LaunchTask run={members[0]}");   // and grouped builds
    expect(codeTasks).toContain("relaunchCodeTaskAction");
  });

  it("cannot launch the same work twice by impatience", () => {
    // Four identical HookWatch runs and two BugBox runs are queued on the production box,
    // because a plain form submit changes nothing on screen and an inert-looking button gets
    // pressed again. Each press is a budgeted governed pipeline.
    const codeTasks = readFileSync(resolve(__dirname, "../app/code-tasks-ui.tsx"), "utf8");
    for (const marker of ["function BenchmarkLaunch", "disabled={sent}", "Starting…"]) {
      expect(codeTasks).toContain(marker);
    }
    // Assert the PROPERTY, not the markup. The first version required every launch <form> to
    // carry `onSubmit={() => setSent(true)}` — and went red the moment the forms became
    // buttons that await the action and refresh, which is strictly better behaviour. A test
    // that pins HOW a guarantee is met turns an improvement into a failure.
    //
    // What must hold: every launch control disables itself while a launch is in flight.
    const launchButtons = codeTasks.split("<button").slice(1)
      .filter((b) => b.slice(0, 400).includes("Starting…"));
    expect(launchButtons.length).toBeGreaterThanOrEqual(2);   // benchmark + relaunch
    for (const button of launchButtons) {
      expect(button.slice(0, 400)).toContain("disabled={sent}");
    }
    // And the busy state must be able to CLEAR, or the button lies in the other direction:
    // it sat on "Starting…" forever while the run had in fact been created.
    expect(codeTasks).toContain("setSent(false)");
  });

  it("gives the browser tab a real icon", () => {
    // Next's App Router serves app/icon.png as the favicon. Without it the tab shows a
    // generic globe, which is what "the icon is still the old one" looks like from a bookmark.
    expect(existsSync(resolve(__dirname, "../app/icon.png"))).toBe(true);
  });

  it("implements the operational design-system views", () => {
    expect(page).toContain('className="cc-composer"');
    expect(operations).toContain('className="mission-task-board"');
    expect(operations).toContain('className="ar-table"');
    expect(page).toContain('className="dq"');
    expect(page).toContain('className="mission-evidence-receipt"');
    expect(page).toContain("<MissionAgentRoster");
    expect(page).toContain("<MissionTaskBoard");
    expect(css).toContain(".cc-composer");
    expect(css).toContain(".ar-head");
    expect(css).toContain(".dq-row");
    expect(css).toContain(".mission-task-footer");
    expect(providerConfiguration).toContain('className="provider-subnav"');
    expect(providerConfiguration).toContain('className="provider-table"');
    expect(providerConfiguration).toContain('className="provider-drawer"');
    expect(providerConfiguration).toContain('className="provider-cost-chart"');
    expect(css).toContain(".provider-suite-summary");
    expect(css).toContain(".provider-drawer");
  });

  it("honors reduced-motion preferences", () => {
    expect(css).toContain("@media (prefers-reduced-motion: reduce)");
  });

  it("keeps provider tables and fixed drawers inside the mobile viewport", () => {
    expect(css).toMatch(
      /\.provider-table-wrap\s*\{[\s\S]*position:\s*relative;[\s\S]*max-width:\s*100%;[\s\S]*overflow-x:\s*auto;/,
    );
    expect(css).toMatch(
      /\.bx-content\[data-active="infra"\][\s\S]*animation:\s*none\s*!important;/,
    );
    expect(css).toContain("width: 100vw;");
  });
});
