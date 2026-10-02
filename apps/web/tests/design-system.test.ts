import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join, resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { groupRunsIntoApps } from "../app/app-grouping";

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
    // Still one renderer, still used by the benchmark catalogue and the task DETAIL view.
    // One renderer, still used by the benchmark catalogue. The card faces no longer carry it:
    // both the single and grouped cards are now compact grid tiles, and acceptance criteria on
    // a 320px tile is exactly the overload that made the card unreadable.
    expect(codeTasks.split("<ContractDetails").length - 1).toBeGreaterThanOrEqual(1);

    // It is no longer on the list card's face, and that is the point of the redesign rather
    // than a regression. The card carried a title, a status badge, an outcome line, a contract
    // disclosure, two action blocks and a four-item footer — everything competing, so the
    // journey spine added to it changed nothing anyone could see. The face now carries what
    // you need to decide whether to OPEN a task; the criteria and verification live one click
    // away in the task view, which is where you go to judge the work itself.
    const detail = codeTasks.slice(codeTasks.indexOf("function CodeTaskDetail"));
    expect(detail).toContain("acceptance_criteria");
    // A <details> inside a <button> is invalid markup whose clicks fight each other.
    expect(codeTasks).not.toContain('<button key={c.id} className="ct-card"');
  });

  it("lets you launch an existing task, not only a benchmark", () => {
    // The benchmark cards could start work; the task list could not, so re-running something
    // meant retyping its instructions and acceptance criteria into the New Code Task form —
    // copying data BOSS already held.
    const codeTasks = readFileSync(resolve(__dirname, "../app/code-tasks-ui.tsx"), "utf8");
    expect(codeTasks).toContain("function LaunchTask");
    expect(codeTasks).toContain("<LaunchTask run={c}");            // every ATTEMPT, inside the app
    // Deliberately NOT on the app card. Re-running the identical brief only makes sense when
    // the spec is being held constant on purpose -- comparing benchmark runs, or re-running
    // after a platform failure that taught nothing about the product. Offered from the card it
    // reads as the remedy for a disappointing app, which is the one thing it cannot be: the
    // same brief is what produced it.
    expect(codeTasks).not.toContain("<LaunchTask run={members[0]}");
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
    // The caption that carried this wording is gone. It read "…same as the original ·
    // budgeted, review-gated" on EVERY card, in the same words, taking a line to state
    // something that never varied and that nothing could be done about. What it was guarding
    // still holds and is asserted above: the relaunch sends the original's profile and tier
    // rather than silently adopting the project default. The profile is now shown once, as a
    // pill in the meta row, instead of narrated on every row.
    expect(relaunch).toMatch(/data\.set\("profile", profile\)/);
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
    // Asserted on BEHAVIOUR, not on source text. This used to grep code-tasks-ui.tsx for
    // "qa:${bench}" and "(fc(c).root_run_id as string)" -- the two clauses of a first-match
    // chain. That chain was the reason ApproveIt showed three times (each run carried a
    // different one of those fields, and the chain stops at the first), so the test was
    // pinning the defect in place: replacing it with a rule that actually groups the runs
    // broke the test while fixing the behaviour it was meant to protect.
    const attempts = [
      { id: "crun_1", spec: { base_branch: "main" },
        finalize_ctx: { qa_benchmark: { id: "approveit" } } },
      { id: "crun_2", spec: { base_branch: "main" },
        finalize_ctx: { qa_benchmark: { id: "approveit" } } },
      { id: "crun_3", spec: { base_branch: "boss/code/crun_1" }, finalize_ctx: {} },
    ];
    expect(groupRunsIntoApps(attempts)).toHaveLength(1);

    // A decomposed build stays its own bucket rather than being regrouped by benchmark.
    expect(groupRunsIntoApps([
      { id: "crun_a", spec: { base_branch: "main" }, finalize_ctx: { root_run_id: "crun_a" } },
      { id: "crun_b", spec: { base_branch: "main" }, finalize_ctx: { root_run_id: "crun_a" } },
    ])).toHaveLength(1);

    // "N tasks" would describe three attempts as a three-part build.
    const codeTasks = readFileSync(resolve(__dirname, "../app/code-tasks-ui.tsx"), "utf8");
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
    // The Command Center composer (`cc-composer`) was replaced by the Ask BOSS hero, which
    // merged asking and assigning an outcome into one entry point at the top of the page.
    expect(page).toContain("<AskBossHero");
    const hero = readFileSync(resolve(__dirname, "../app/ask-boss-hero.tsx"), "utf8");
    expect(hero).toContain('className="askboss"');
    expect(operations).toContain('className="mission-task-board"');
    expect(operations).toContain('className="ar-table"');
    const inbox = readFileSync(resolve(__dirname, "../app/decision-inbox-ui.tsx"), "utf8");
    expect(page).toContain("<DecisionInbox");
    expect(inbox).toContain('className="dq"');
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

  it("keeps the Command Center bound to live workflow runs, not a static showcase", () => {
    // BOSS-146: the surface must remain a control-plane projection, not a static showcase.
    // REWRITTEN: this asserted the task-era sources (status.openTasks, an orb roster over
    // tasks.owner_role_id, <CoordinationPanel>). A run started by Build creates no task, so
    // those never showed it; the Command Center now reads workflow runs. What is protected is
    // the same: live data on first paint, and a screen that keeps itself current.
    expect(page).toContain("fetchCommandCenter(activeProjectId");
    expect(page).toContain("<CommandCenter");
    const cc = readFileSync(resolve(__dirname, "../app/command-center-ui.tsx"), "utf8");
    expect(cc).toContain("/v1/console/command-center?project_id=");
    expect(cc).toContain("POLL_MS");
    expect(cc).toContain("onChanged(");
  });

  it("keeps approvals decision-backed and exposes their complete evidence", () => {
    // BOSS-147: a pending decision needs a clearly attributable proposer,
    // impact, rationale/evidence, and the governed resolve action. A read-only
    // card or a blind approve button would both violate the queue contract.
    const inbox = readFileSync(resolve(__dirname, "../app/decision-inbox-ui.tsx"), "utf8");
    const forms = readFileSync(resolve(__dirname, "../app/client-forms.tsx"), "utf8");
    expect(page).toContain("<DecisionInbox");
    expect(inbox).toContain('className="dq-row"');
    expect(inbox).toContain('className="dq-who"');
    expect(inbox).toContain('className={`dq-impact ${impact.tone}`}');
    expect(inbox).toContain('className="dq-evidence"');
    expect(inbox).toContain("DecisionResolveForm");
    expect(inbox).toContain('className={`dq-verdict ${');
    expect(forms).toContain("resolveDecisionAction");
    expect(css).toContain(".dq-row + .dq-row");
    expect(css).toContain(".dq-actions");
  });

  it("renders receipt evidence with governed completion proof rather than a status claim", () => {
    // BOSS-150: a receipt view must communicate effect lineage plus measured
    // checks. The completed-code-task and fallback-receipt branches both need
    // the same visible evidence structure.
    expect(page).toContain('className="mission-evidence-receipt"');
    expect(page).toContain('className="mission-receipt-metrics mission-receipt-metrics--four"');
    expect(page).toContain('className="mission-receipt-verification"');
    expect(page).toContain('className="mission-receipt-files"');
    expect(page).toContain("Independent reviewer signed off");
    expect(page).toContain("Unique dedupe key prevents duplicate effects");
    expect(css).toContain(".mission-receipt-metrics--four");
    expect(css).toContain(".mission-receipt-verification");
  });

  it("keeps global controls accessible, motion-safe, and visibly stateful", () => {
    // BOSS-151: the persistent shell, keyboard palette/dialog, completion
    // feedback, and motion preference are system-level behavior, not isolated
    // page decoration.
    const forms = readFileSync(resolve(__dirname, "../app/client-forms.tsx"), "utf8");
    // The rail marks the current selection. It keys off `selected` rather than `active`:
    // `active` is the area currently PAINTED, and during a navigation the two differ on
    // purpose so the outgoing screen stays visible while the incoming one loads. The
    // highlight must follow the press, so it is the one that reads `selected`.
    expect(shell).toContain('className={`bx-nav-item${a.id === selected ? " active" : ""}`}');
    expect(shell).toContain('role="dialog"');
    expect(shell).toContain("aria-modal=\"true\"");
    expect(forms).toContain("role=\"alert\"");
    expect(forms).toContain("boss:toast");
    expect(css).toContain(".bx-nav-item.active");
    expect(css).toContain(".toast-success");
    expect(css).toContain("@media (prefers-reduced-motion: reduce)");
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

describe("the task list is about the operator's work", () => {
  it("hides BOSS's own housekeeping runs", () => {
    // Forty-odd "Captain: QUALITY IMPROVE PASS. Measured scorecard gaps…" runs sat in the
    // Factory list, drowning the tasks somebody actually started. The identical rule was
    // already written for the Apps page and never applied here: the same defect fixed in one
    // place and left in the one people look at.
    const src = readFileSync(resolve(__dirname, "../app/code-tasks-ui.tsx"), "utf8");
    expect(src).toContain("function isInternalRun");
    // The filter moved into the grouping call when the identity chain was replaced; what
    // matters is that housekeeping runs never reach the grid, not the line that removes them.
    expect(src).toMatch(/isInternalRun\(c\)/);
    for (const marker of ["captain:", "quality improve pass"]) {
      expect(src.toLowerCase()).toContain(marker);
    }
    // A follow-up is the operator's OWN work — an earlier filter caught it and took the list
    // from 48 cards to 2.
    expect(src).not.toMatch(/INTERNAL_RUN =.*follow-up/i);
  });

  it("hides them rather than deleting them", () => {
    // They are real runs. They still open by id and still count in Reports; they simply do not
    // lead the list an operator uses to see their own work.
    const src = readFileSync(resolve(__dirname, "../app/code-tasks-ui.tsx"), "utf8");
    expect(src).not.toMatch(/DELETE FROM code_task_runs/i);
  });
});
