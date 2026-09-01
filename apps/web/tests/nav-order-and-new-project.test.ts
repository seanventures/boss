import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * The Run group opens with the place work is DONE, and a project can be started from anywhere.
 *
 * The nav led with Discovery and Meetings because that is the chronological order of a piece of
 * work — which is not the same as what an operator opens the console to look at. Factory sat
 * third, and starting a project meant navigating INTO the area that lists projects first,
 * because the only "New" button lived on that page.
 */
const SHELL = codeOnly("app/ConsoleShell.tsx");
const CT = codeOnly("app/code-tasks-ui.tsx");
const CSS = codeOnly("app/globals.css");

/** Area ids in the order the Run group declares them. */
const runOrder = (): string[] =>
  [...SHELL.matchAll(/\{ id: "([a-z_-]+)",[^}]*group: "Run"/g)].map((m) => m[1]);

describe("the Run group order", () => {
  it("opens with the place work is done, then the path it follows", () => {
    expect(runOrder().slice(0, 7)).toEqual([
      "code_tasks",          // Factory / Projects
      "delivery_workflows",  // Workflow
      "workforce",           // Agents
      "org_chart",
      "decisions",           // Approvals
      "delivery",            // Infra and Models
      "monitoring",          // Observability
    ]);
  });

  it("keeps Discovery and Meetings — they moved, they did not go", () => {
    // They are the two ways IN besides starting a project directly. Demoting them is a
    // judgement about emphasis; removing them would be a loss of function.
    expect(runOrder()).toContain("discovery");
    expect(runOrder()).toContain("meetings");
  });

  it("lists each area exactly once", () => {
    // Reordering by copy-and-delete is how an entry ends up in two places at once.
    const ids = runOrder();
    expect(new Set(ids).size).toBe(ids.length);
  });
});

describe("the renamed areas", () => {
  it.each([
    ["workforce", "Agents"],
    ["delivery", "Infra and Models"],
    ["delivery_workflows", "Workflow"],
    ["code_tasks", "Projects"],
  ])("%s is labelled %s", (id, label) => {
    expect(SHELL).toMatch(new RegExp(`id: "${id}", label: "${label}"`));
  });

  it("keeps the area IDS untouched", () => {
    // Labels are copy; ids are routes, localStorage keys and deep links.
    for (const id of ["workforce", "delivery", "delivery_workflows", "code_tasks"]) {
      expect(SHELL).toContain(`id: "${id}"`);
    }
  });
});

describe("New project, from anywhere", () => {
  it("is in the top bar", () => {
    expect(SHELL).toContain('data-testid="topbar-new-project"');
    expect(SHELL).toContain("New project");
    expect(CSS).toContain(".bx-new-project");
  });

  it("opens the Factory area and asks it for the form", () => {
    const btn = SHELL.slice(SHELL.indexOf('data-testid="topbar-new-project"'),
                            SHELL.indexOf('className="bx-topbar-spacer"'));
    expect(btn).toContain('go("code_tasks")');
    expect(btn).toContain("searchForNewProject(window.location.search)");
  });

  it("is answered by the area that owns the form", () => {
    // The shell must not reach into the area's state, and the area must not know about the
    // shell. `?new=1` is the handoff.
    expect(CT).toContain("readNewProjectIntent(window.location.search)");
    expect(CT).toContain("setShowNew(true)");
  });

  it("CONSUMES the request rather than leaving it in the URL", () => {
    // This test replaces one that asserted the reader must never close the form — which
    // encoded the bug. `?new=1` is a one-shot command, and navigation copies the query string,
    // so leaving it there re-delivered it forever: clicking a project set `?run=` on a URL that
    // still said `new=1`, popstate fired, and the form reopened over the project you clicked.
    // The sequence itself is tested in url-intent.test.ts.
    const reader = CT.slice(CT.indexOf("readNewProjectIntent(window.location.search)"));
    expect(reader.slice(0, 700)).toContain("history.replaceState");
    expect(reader.slice(0, 700)).toContain("intent.nextSearch");
  });
});

describe("the top bar does not repeat the nav", () => {
  const CSS_ALL = codeOnly("app/globals.css");
  /** Just the one rule — a fixed-length window runs into whatever follows it. */
  const titleRule = () => {
    const at = CSS_ALL.indexOf(".bx-topbar-title {");
    return CSS_ALL.slice(at, CSS_ALL.indexOf("}", at) + 1);
  };

  it("hides the area title and description from sight", () => {
    // The left nav already shows the active area, selected and highlighted, at all times.
    // Repeating it here alongside its description spent two lines of a bar holding eight
    // other controls, to say what the eye had already been told.
    expect(titleRule()).toContain("clip-path: inset(50%)");
  });

  it("keeps it in the accessibility tree", () => {
    // It is the console's title element and a screen reader still announces which area you
    // are in. `display: none` would have removed it from that tree entirely — invisible and
    // unannounced are not the same thing.
    expect(titleRule()).not.toContain("display: none");
    expect(SHELL).toContain('id="boss-console-title"');
  });
});

describe("choosing the workflow", () => {
  const HERO = codeOnly("app/ask-boss-hero.tsx");

  it("Command Center sets the project DEFAULT, not a one-off", () => {
    // "Build →" does not create a project — it sends the directive down panel → PRD → then
    // code. Threading a per-press choice through a meeting and an approval before it reaches
    // the eventual code task is real plumbing for a choice nobody makes twice.
    expect(HERO).toContain('data-testid="askboss-default-workflow"');
    // The label is short — the full sentence is the tooltip — so assert the tooltip, which is
    // what actually says the scope of the setting.
    expect(HERO).toContain("every new project in this project will follow");
  });

  it("writes through the mapping action that already exists", () => {
    // One way a mapping gets created, not two that can disagree — the Workflows area uses the
    // same endpoint.
    expect(HERO).toContain('action: "apply"');
    expect(HERO).toContain('work_class: "project.new"');
  });

  it("says whether it saved", () => {
    // A silent select is indistinguishable from one that did nothing, and this control changes
    // what every future project does.
    expect(HERO).toContain('data-testid="askboss-wf-note"');
    expect(HERO).toContain("could not save");
  });

  it("the per-project picker still exists on the form", () => {
    // Two different questions: "what should THIS project follow" and "what should new projects
    // follow from now on".
    expect(CT).toContain('data-testid="ct-new-workflow"');
    expect(CT).toContain('name="workflow_version_id"');
  });
});

describe("watching the outer workflow, step by step", () => {
  const DW = codeOnly("app/delivery-workflows-ui.tsx");
  const CSS_DW = codeOnly("app/globals.css");

  it("shows how long each step took", () => {
    // The first question anyone asks of a pipeline, and the timestamps were in the payload all
    // along — the RunStep type simply never declared them.
    expect(DW).toContain("function stepDuration");
    expect(DW).toContain('data-testid="dw-step-took"');
    expect(DW).toContain("started_at: string | null;");
    expect(DW).toContain("completed_at: string | null;");
  });

  it("distinguishes a step still running from one that finished", () => {
    // `${text}…` on an unfinished step: "8m" and "8m…" are different facts.
    const fn = DW.slice(DW.indexOf("function stepDuration"), DW.indexOf("function stepLinks"));
    expect(fn).toContain("step.completed_at ? text");
  });

  it("links to what the step produced, for debugging", () => {
    // A run view that shows only a status makes an operator open five other screens to find
    // the PR it opened or the panel it convened.
    expect(DW).toContain("function stepLinks");
    expect(DW).toContain("open the project");
    expect(DW).toContain("activity log");
    expect(DW).toContain("Ask BOSS");
    expect(DW).toContain("pull request");
  });

  it("finds the code task whether the step produced it or adopted it", () => {
    // An adopted build has the id under `await`, not under `code_task_run_id` — the same step
    // in the two shapes it can legitimately take.
    const fn = DW.slice(DW.indexOf("function stepLinks"), DW.indexOf("function RunsView"));
    expect(fn).toContain('str("code_task_run_id")');
    expect(fn).toContain('awaited.kind === "code_task"');
  });

  it("answers a gate where it is seen", () => {
    // A gate answerable only in another area makes the operator leave the screen that told
    // them it was waiting, and find it again in a list of everything else.
    expect(DW).toContain('data-testid="dw-step-decide"');
    expect(DW).toContain("resolveDecisionAction");
    expect(DW).toContain('value="approve"');
    expect(DW).toContain('value="decline"');
  });

  it("only offers Approve/Deny on a step actually waiting on one", () => {
    // The condition moved into `needsYou`, which the path view also uses to mark the node —
    // so the button and the highlight can never disagree about who is being asked.
    expect(DW).toContain("function needsYou");
    expect(DW).toContain('Boolean(step.decision_id) && step.status === "waiting"');
    expect(DW).toContain("{needsYou(step) ? (");
  });

  it("drops the panel that showed the coding pipeline's own phase", () => {
    // That is the layer INSIDE the build step — one step of nine, and the one an operator
    // cares least about. Showing it on the workflow page answered a question nobody asked.
    expect(DW).not.toContain("GovernedProjects");
    expect(CSS_DW).not.toContain(".dw-governed");
  });
});

describe("which run the workflow page shows first", () => {
  const DW = codeOnly("app/delivery-workflows-ui.tsx");

  it("puts live runs above finished and blocked ones", () => {
    // The page showed a blocked run from an earlier day while a project was building, because
    // the only ordering was `updated_at` — and anything that touches an old run floats it back
    // to the top. Liveness first, recency within it.
    expect(DW).toContain("function byLiveness");
    expect(DW).toContain("[...runs].sort(byLiveness)");
  });

  it("names a run by the project it is delivering", () => {
    // Five runs of "Standard Product Delivery" are five identical rows. The workflow name stays,
    // demoted to the second line, because which workflow is still worth knowing.
    expect(DW).toContain("function runSubject");
    expect(DW).toContain('data-testid="dw-run-workflow"');
  });

  it("still names the workflow when the run has no title", () => {
    // A run started by something other than a project has no subject, and an empty heading is
    // worse than a repeated one.
    expect(DW).toContain("runSubject(run) ?? run.workflow_name");
  });
});

describe("a phase that never happened", () => {
  const DW = codeOnly("app/delivery-workflows-ui.tsx");

  it("reads as not run, not as a success", () => {
    // A project started directly has no research phase and no panel. The runtime records those
    // steps as not applicable rather than commissioning research for something already built —
    // and showing them as plain successes would claim work nobody did.
    expect(DW).toContain("function notApplicable");
    expect(DW).toContain('"dw-step-na"');
    expect(DW).toContain('"not run"');
  });

  it("says why", () => {
    expect(DW).toContain("dw-step-note");
  });
});

describe("what review and test actually found", () => {
  const DW = codeOnly("app/delivery-workflows-ui.tsx");

  it("shows the quality gates the build recorded", () => {
    // `review` and `test` commission nothing — the governed pipeline already graded the build.
    // Before this they were two rows saying only "succeeded" about the checks an operator most
    // wants to read.
    expect(DW).toContain("function observedGates");
    expect(DW).toContain('"dw-step-gates"');
    expect(DW).toContain('out.status !== "observed"');
  });

  it("shows what the monitoring step is watching, not a gate list", () => {
    // "Set up monitoring" reports deployment targets under health probing — a different shape
    // from review and test, and the only thing that answers what that step is for.
    expect(DW).toContain("Array.isArray(out.targets)");
    expect(DW).toContain("`watching ${names.join(\", \")}`");
  });
});

describe("the workflow drawn as a path", () => {
  const DW = codeOnly("app/delivery-workflows-ui.tsx");
  const CSS_P = codeOnly("app/globals.css");

  it("draws a rail of steps, not a stack of rows", () => {
    // A pipeline is a shape. Nine rows of identical text made an operator read nine statuses
    // and assemble the shape in their head.
    expect(DW).toContain("function StepPath");
    expect(DW).toContain('data-testid="dw-node"');
    expect(CSS_P).toContain(".dw-rail {");
  });

  it("animates the segment the work is crossing right now", () => {
    expect(CSS_P).toContain("@keyframes dw-flow");
    expect(CSS_P).toContain('.dw-link[data-flow="moving"]');
    expect(DW).toContain('phase === "now" ? "moving" : "past"');
  });

  it("highlights the step in flight", () => {
    expect(CSS_P).toContain("@keyframes dw-pulse");
    expect(DW).toContain('aria-current={phase === "now" ? "step" : undefined}');
  });

  it("respects a request for less motion", () => {
    // Motion here is emphasis, not decoration; anyone who has asked for less must get less.
    const at = CSS_P.indexOf("@media (prefers-reduced-motion: reduce)");
    expect(at).toBeGreaterThan(-1);
    expect(CSS_P.slice(at, at + 260)).toContain("animation: none");
  });

  it("draws a phase that never happened as dashed, not green", () => {
    expect(CSS_P).toContain('.dw-node[data-phase="skipped"] .dw-node-btn { border-style: dashed;');
  });

  it("opens on the step that wants attention", () => {
    // Landing on "research succeeded" while a gate three steps along waits for you is the
    // wrong answer to "what is happening".
    expect(DW).toContain("steps.find(needsYou) ?? steps.find((s) => phaseOf(s) === \"now\")");
  });

  it("names a step by what it IS, from the engine's registry", () => {
    // `spec_review` is a step id; "Approve the spec" is what it does.
    expect(DW).toContain("capability?.label ?? step.capability ?? step.step_id");
  });

  it("shows the whole story of one step in one place", () => {
    // Duration, why it failed, what it produced, and the doors to debug it — the list the
    // operator asked for.
    expect(DW).toContain("function StepDetail");
    for (const marker of ['data-testid="dw-step-took"', 'data-testid="dw-step-error"',
                          'data-testid="dw-step-links"', 'data-testid="dw-step-out"',
                          'data-testid="dw-step-decide"']) {
      expect(DW).toContain(marker);
    }
  });
});

describe("a phase that was restarted says so", () => {
  const CT_SRC = codeOnly("app/code-tasks-ui.tsx");

  it("does not call a replayed phase 'BOSS's own machinery'", () => {
    // When a run is re-delivered the same phase appears twice, and the second copy finds no
    // unclaimed model call — so the timeline said "no model call — BOSS's own machinery" about
    // thirty minutes of work thrown away when a lease expired. That is how a 46-minute build
    // became a forensic exercise instead of a legible one.
    expect(CT_SRC).toContain('data-testid={g.replayed ? "ct-phase-replayed" : undefined}');
    expect(CT_SRC).toContain("restarted — the previous attempt was interrupted");
  });

  it("only marks the SECOND appearance of a phase", () => {
    // The first attempt was real work; only its repeat is the replay.
    expect(CT_SRC).toContain("phaseTimings.slice(0, i).some((p) => p.phase === t.phase)");
  });

  it("only when the run was actually re-delivered", () => {
    // A workflow that legitimately runs a phase twice is not a replay.
    expect(CT_SRC).toContain("redelivered && phaseTimings.slice(0, i)");
    expect(CT_SRC).toContain("_fc.redelivered_attempts");
  });
});
