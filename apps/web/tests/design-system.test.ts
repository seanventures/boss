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
    for (const marker of ["function BenchmarkLaunch", "Starting…"]) {
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
      // A pattern, not a literal. `disabled={sent}` became `disabled={sent || !!startedId}`
      // — strictly stronger — and the exact-string version went red on the improvement. That
      // is the third time one of these has pinned an implementation instead of a guarantee.
      // Fourth: `disabled={sent}` went red when a new control used `busy`, which is the same
      // guarantee under a different variable name. What must hold is that SOME in-flight flag
      // disables the button -- not what that flag is called.
      expect(button.slice(0, 400)).toMatch(/disabled=\{[^}]*\b(sent|busy|pending|inFlight)\b/);
    }
    // And the busy state must be able to CLEAR, or the button lies in the other direction:
    // it sat on "Starting…" forever while the run had in fact been created.
    expect(codeTasks).toMatch(/set(Sent|Busy)\(false\)/);
    // But it must NOT return to its original label afterwards. Resetting to "Launch again"
    // while the new row had not appeared yet is what made people press it a second time:
    // nothing on screen had changed, so nothing looked like it had happened.
    const relaunch = codeTasks.slice(codeTasks.indexOf("function LaunchTask"),
                                     codeTasks.indexOf("function ContractDetails"));
    // Once it has launched, the control must not offer to launch again. It used to do that
    // by staying disabled; it now becomes "View run →", which is stronger — it cannot start a
    // second run AND it leads somewhere. Assert the guarantee, not either implementation.
    expect(relaunch).toMatch(/startedId \? \(/);          // a different control once started
    expect(relaunch).toContain("View run →");
    expect(relaunch).toContain("onOpen?.(startedId)");
    // And it must NAME what it started rather than promising a place in a list it cannot see.
    expect(relaunch).toContain("startedId.slice");
    // That memory must live in the PARENT. Held inside the card it was destroyed by the very
    // reload the launch triggers — the card remounts, "Launched ✓" reverts to the original
    // label, nothing on screen has changed, and the operator presses it again. State that has
    // to outlive a re-render cannot live in the thing being re-rendered.
    expect(relaunch).not.toMatch(/useState[^\n]*startedId/);
    expect(codeTasks).toContain("const [launched, setLaunched]");
    expect(codeTasks).toContain("startedId={launched[c.id]?.run_id}");
  });

  it("never claims a profile the relaunch will not use", () => {
    // The Launch caption read "Strict profile" on every task card, because the markup was
    // copied wholesale from the benchmark card — where it is true, since that endpoint forces
    // strict. On a relaunch it was a claim about someone else's button: the currency build ran
    // `lean`, and the relaunch was silently adopting the project default, so the label, the
    // original and the rerun were three different things.
    const codeTasks = readFileSync(resolve(__dirname, "../app/code-tasks-ui.tsx"), "utf8");
    const relaunch = codeTasks.slice(codeTasks.indexOf("function LaunchTask"),
                                     codeTasks.indexOf("function ContractDetails"));
    expect(relaunch).not.toContain("Strict profile");
    // It must carry the original's profile, and say which one it is.
    expect(relaunch).toContain('data.set("profile", profile)');
    expect(relaunch).toContain('data.set("risk_tier"');
    expect(relaunch).toContain("same as the original");
    // Nor may the benchmark card. It said "Strict profile" long after the endpoint had been
    // changed to ask for balanced — a caption describing a decision made in another file,
    // which is exactly how it came to be wrong. It must name what the endpoint requests.
    const benchmark = codeTasks.slice(codeTasks.indexOf("function BenchmarkLaunch"),
                                      codeTasks.indexOf("function LaunchTask"));
    expect(benchmark).not.toMatch(/strict profile/i);
    // Names the profile the endpoint actually requests, in whatever wording the caption uses
    // -- pinning the exact sentence made this fail when the caption gained the PRD-panel step,
    // which is a change to the copy, not a return of the defect.
    expect(benchmark).toMatch(/balanced profile/i);
    const api = readFileSync(resolve(__dirname, "../../../apps/api/app/main.py"), "utf8");
    expect(api).toContain('(payload or {}).get("profile") or "balanced"');
  });

  it("both launch buttons behave the same way", () => {
    // The relaunch button was fixed and its sibling was not. So the QA benchmark card kept
    // the original defect — action fires, run is created, revalidatePath replaces the tree,
    // the callback lands on a remounted component, and the button reverts to "Launch
    // ApproveIt" with nothing on screen having changed. An operator who had waited hours to
    // press it saw a dead button. Fixing one path and not its twin is the recurring shape of
    // this whole area, so the test now covers both.
    const codeTasks = readFileSync(resolve(__dirname, "../app/code-tasks-ui.tsx"), "utf8");
    const benchmark = codeTasks.slice(codeTasks.indexOf("function BenchmarkLaunch"),
                                     codeTasks.indexOf("function LaunchTask"));
    expect(benchmark).toMatch(/startedId \? \(/);        // becomes a link once launched
    expect(benchmark).toContain("View run →");
    expect(benchmark).not.toContain("router.refresh()");  // would wipe the state it just set
    // The parent holds the memory for BOTH, keyed differently but the same mechanism.
    expect(codeTasks).toContain("startedId={launched?.[benchmark.id]?.run_id}");
  });

  it("keeps a benchmark's attempts together, and counts them as attempts", () => {
    // Pressing Launch ApproveIt three times over a week is three attempts at ONE thing,
    // scored against the same rubric. Scattered as loose rows among unrelated tasks, the
    // comparison the benchmark exists to make is lost. Lineage still wins where it applies,
    // so a decomposed build stays one bucket rather than being regrouped by benchmark.
    const codeTasks = readFileSync(resolve(__dirname, "../app/code-tasks-ui.tsx"), "utf8");
    expect(codeTasks).toContain("qa:${bench}");
    expect(codeTasks).toContain("(fc(c).root_run_id as string)");   // lineage first
    // "N tasks" would describe three attempts as a three-part build.
    expect(codeTasks).toMatch(/attempt/);
  });

  it("does not offer to re-run work that is still running", () => {
    // The API refuses it anyway — the duplicate guard returns the in-flight run — but a
    // button that looks available and then quietly does something else teaches an operator
    // that the controls here are decorative.
    const codeTasks = readFileSync(resolve(__dirname, "../app/code-tasks-ui.tsx"), "utf8");
    expect(codeTasks).toContain('run.status === "running" || run.status === "queued"');
    expect(codeTasks).toContain("disabled={sent || inFlight}");
    expect(codeTasks).toContain('inFlight ? "Running…"');
  });

  it("owns the list it claims a new run will appear in", () => {
    // "started — it is at the top of this list" was a promise about a server-rendered list
    // this component could not update: `router.refresh()` re-renders on the server while the
    // client keeps the old rows. The list did not move, so the launch read as a failure.
    const codeTasks = readFileSync(resolve(__dirname, "../app/code-tasks-ui.tsx"), "utf8");
    expect(codeTasks).toContain("listCodeTasksAction");
    expect(codeTasks).toContain("const [tasks, setTasks]");
    // The poller must read the panel's OWN state, or a run started a second ago cannot keep
    // it alive while the server prop catches up.
    expect(codeTasks).toContain("const anyLive = tasks.some(");
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
