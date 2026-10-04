import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";
import { laneStateOf, needsYou, type RunStep } from "../app/delivery-workflows-ui";
import { runPalette, runSpec } from "../app/workflows/flow/run-lane";
import { stepView } from "../lib/step-view";

/**
 * The Run group opens with the place work is DONE, and a project can be started from anywhere.
 *
 * The nav led with Discovery and Meetings because that is the chronological order of a piece of
 * work — which is not the same as what an operator opens the console to look at. Factory sat
 * third, and starting a project meant navigating INTO the area that lists projects first,
 * because the only "New" button lived on that page.
 */
const SHELL = codeOnly("app/ConsoleShell.tsx");

/** One run step, with only what a test sets. */
const step = (over: Partial<RunStep>): RunStep => ({
  step_id: "s", kind: "capability", status: "pending", capability: null, output: {},
  evidence: [], error: null, approval: {}, due_at: null, timeout_applied: null, attempt: 1,
  started_at: null, completed_at: null, decision_id: null, ...over,
} as RunStep);
const CT = codeOnly("app/code-tasks-ui.tsx");
const CSS = codeOnly("app/globals.css");

/** Area ids in the order the Run group declares them. */
const runOrder = (): string[] =>
  [...SHELL.matchAll(/\{ id: "([a-z_-]+)",[^}]*group: "Run"/g)].map((m) => m[1]);

describe("the Run group order", () => {
  it("opens with the place work is done, then the path it follows", () => {
    // Workflows and Agents are one row each now: Team structure and Agent Studio are views of
    // Agents, and their old links redirect (tests/legacy-areas.test.ts).
    expect(runOrder().slice(0, 5)).toEqual([
      "code_tasks",          // Factory / Projects
      "delivery_workflows",  // Workflows
      "workforce",           // Agents
      "decisions",           // Approvals
      "delivery",            // Infra
    ]);
    expect(runOrder()).not.toContain("org_chart");
    expect(runOrder()).not.toContain("studio");
  });

  it("keeps Discovery and Meetings as AREAS, off the rail", () => {
    // They are steps of a workflow, not destinations: nobody should start a research run or a
    // panel on its own, and a rail row advertises exactly that. A dossier started outside a
    // workflow was briefly answered by putting Discovery back here — the wrong end of the
    // problem; the chat agent starts a workflow now instead. Hidden from the rail, not
    // removed, or every link naming them would 404.
    expect(runOrder()).toContain("discovery");
    expect(runOrder()).toContain("meetings");
    expect(SHELL).toContain("const OFF_THE_NAV");
    expect(SHELL).toContain('new Set<AreaId>(["discovery", "meetings"])');
    expect(SHELL).toContain("!OFF_THE_NAV.has(a.id)");
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
    ["delivery", "Infra"],
    ["delivery_workflows", "Workflows"],
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

  it("opens the Workflow area and asks it to start something", () => {
    const btn = SHELL.slice(SHELL.indexOf('data-testid="topbar-new-project"'),
                            SHELL.indexOf('className="bx-topbar-spacer"'));
    expect(btn).toContain('go("delivery_workflows")');
    expect(btn).toContain("searchForNewProject(window.location.search)");
  });

  it("is answered by the area that watches the work", () => {
    // Starting work and watching it belong together now that the workflow drives: the press
    // creates a RUN, and the run is what the operator then needs to see.
    const DWN = codeOnly("app/delivery-workflows-ui.tsx")
      + codeOnly("app/workflows/new-project-composer.tsx");
    expect(DWN).toContain("readNewProjectIntent(window.location.search)");
    expect(DWN).toContain('data-testid="dw-new-project"');
    expect(DWN).toContain('data-testid="dw-new-start"');
  });

  it("Factory keeps its own entry for changing an app that already exists", () => {
    // A different act from starting something: it builds on a branch and a PR that exist, and
    // it lives on a project's own page rather than on the list.
    expect(CT).toContain('data-testid="ct-iterate-workflow"');
    expect(CT).toContain("Add to this project");
  });

  it("CONSUMES the request rather than leaving it in the URL", () => {
    // `?new=1` is a one-shot command, and navigation copies the query string, so leaving it
    // there re-delivered it forever: clicking a project set `?run=` on a URL that still said
    // `new=1`, popstate fired, and the form reopened over the project you clicked. The reader
    // lives on the Workflow page now, which is where the composer is.
    const DWC = codeOnly("app/delivery-workflows-ui.tsx");
    const reader = DWC.slice(DWC.indexOf("readNewProjectIntent(window.location.search)"));
    expect(reader.slice(0, 700)).toContain("history.replaceState");
    expect(reader.slice(0, 700)).toContain("intent.nextSearch");
  });

  it("Factory offers ONE way to start a project, and it is not a second form", () => {
    // Starting a project moved to Workflow and the Factory form stayed, so there were two
    // ways to do it on two pages — one of which showed the steps that follow and one of which
    // did not. Two entry points for one act is how somebody ends up unsure which is real.
    expect(CT).not.toContain("NewCodeTaskForm");
    expect(CT).toContain('data-testid="ct-start-in-workflow"');
    expect(CT).toContain("function startAProjectHref");
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

  it("a per-piece-of-work picker still exists", () => {
    // Two different questions: "what should THIS work follow" and "what should new projects
    // follow from now on". The first now lives in two places, each next to the work it
    // governs: the Workflow composer for a new project, the Iterate panel for a change.
    const DWP = codeOnly("app/workflows/new-project-composer.tsx");
    // It is a step in the New Project wizard now rather than a dropdown beside the box: one
    // card per path, each carrying the description and the gates, because those are the whole
    // reason one path is right and another is not — and a <select> hides them.
    expect(DWP).toContain('data-testid={`dw-new-workflow-${w.workflow_key}`}');
    expect(DWP).toContain('data-testid="dw-new-workflow-default"');
    expect(CT).toContain('data-testid="ct-iterate-workflow"');
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
    // The log is the step's own now: the panel's Activity section links to it.
    expect(DW).toContain("stepLogHref(");
    expect(DW).toContain("onAsk={ask}");
    expect(DW).toContain("pull request");
  });

  it("finds the code task whether the step produced it or adopted it", () => {
    // An adopted build has the id under `await`, not under `code_task_run_id` — the same step
    // in the two shapes it can legitimately take.
    const fn = DW.slice(DW.indexOf("function stepLinks"), DW.indexOf("function RunsView"));
    expect(fn).toContain('str("code_task_run_id")');
    expect(fn).toContain('awaited.kind === "code_task"');
  });

  it("sends a gate to where the decision can be READ", () => {
    // This asserted the opposite — two buttons answering a release from the step badge, with
    // no recommendation, no cost, no risk tier and no note in sight. Approving a deployment
    // from there is a decision made blind; the Approvals page is where that context lives.
    expect(DW).toContain('data-testid="dw-step-review"');
    expect(DW).toContain('area: "decisions"');
    expect(DW).toContain("decision: step.decision_id");
    expect(DW).not.toContain("resolveDecisionAction");
  });

  it("only offers the decision on a step actually waiting on one", () => {
    // The condition lives in `needsYou`, which the path view also uses to mark the node — so
    // the control and the highlight can never disagree about who is being asked.
    //
    // Asserted as BEHAVIOUR, not as the source of the expression. This pinned the exact text
    // `Boolean(step.decision_id) && step.status === "waiting"`, so teaching the gate to read
    // the DECISION's status — the fix for an approved gate that kept offering itself — broke a
    // test that has nothing to say about that. See an-approved-gate-stops-asking.test.ts.
    expect(DW).toContain("function needsYou");
    expect(DW).toContain("{needsYou(step) ? (");
    const gate = (over: Record<string, unknown>) => needsYou({
      step_id: "deploy", kind: "gate", status: "waiting", capability: null, output: {},
      evidence: [], error: null, approval: {}, due_at: null, timeout_applied: null,
      attempt: 1, started_at: null, completed_at: null, decision_id: "dec_1", ...over,
    } as never);
    expect(gate({ decision_status: "pending" })).toBe(true);
    expect(gate({ decision_id: null })).toBe(false);
    expect(gate({ status: "succeeded" })).toBe(false);
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
    expect(DW).toContain("byLiveness");
    expect(DW).toContain(".sort(byLiveness)");
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

  it("reads as skipped with its reason, not as a success", () => {
    // A project started directly has no research phase and no panel. The runtime records those
    // steps as not applicable — and showing them as plain successes would claim work nobody
    // did. On the lane they are `skipped`; an operator's skip says who skipped it.
    expect(DW).toContain("function notApplicable");
    expect(DW).toContain('"dw-step-na"');
    expect(DW).toContain("function skipReason");
    // On the run lane it is `skipped`, and the reason is the skipped node's tooltip.
    expect(DW).toContain('tip: skipReason(step) || "this step did not apply to this run"');
    expect(laneStateOf(step({ step_id: "research", status: "succeeded", output: { status: "not_applicable" } })).phase)
      .toBe("skipped");
    expect(laneStateOf(step({ step_id: "panel", status: "skipped" }))).toMatchObject({ phase: "skipped", meta: "skipped" });
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
  // REWRITTEN (Workflow Studio): the path is the shared lane (app/workflows/flow), in run mode.
  const LANE = codeOnly("app/workflows/flow/flow-lane.tsx");
  const NODE = codeOnly("app/workflows/flow/step-node.tsx");
  const FLOW_CSS = codeOnly("app/workflows/flow.css");

  it("draws the run on the same lane the editor draws, in run mode", () => {
    // A pipeline is a shape. The run's lane is the editor's FlowLane, so a workflow looks the
    // same being built and being run (its drawing is pinned in tests/flow-lane.test.tsx).
    expect(DW).toContain("function StepPath");
    expect(DW).toContain('<FlowLane spec={laneSpec} palette={lanePalette} mode="run"');
    expect(DW).toContain("runState={laneState}");
    expect(FLOW_CSS).toContain(".wf-lane-viewport {");
  });

  it("tells the lane which step is in flight, which waits on you, and which failed", () => {
    const lane = (s: RunStep) => laneStateOf(s);
    expect(lane(step({ step_id: "a", status: "succeeded", started_at: "2026-10-01T10:00:00Z",
                       completed_at: "2026-10-01T10:04:20Z" }))).toEqual({ phase: "done", meta: "4m 20s" });
    expect(lane(step({ step_id: "b", status: "running" })).phase).toBe("moving");
    expect(lane(step({ step_id: "c", status: "waiting", decision_id: "dec_1", decision_status: "pending" })))
      .toEqual({ phase: "needs_you", meta: "needs you" });
    expect(lane(step({ step_id: "d", status: "failed" })).phase).toBe("failed");
    expect(lane(step({ step_id: "e", status: "pending" }))).toEqual({ phase: "future", meta: "pending" });
  });

  it("animates the segment the work is crossing right now", () => {
    expect(FLOW_CSS).toContain("@keyframes wf-flow");
    expect(FLOW_CSS).toContain('.wf-conn[data-flow="moving"]');
    expect(LANE).toContain('return next === "moving" || next === "needs_you" ? "moving"');
  });

  it("highlights the step in flight", () => {
    expect(FLOW_CSS).toContain("@keyframes wf-pulse");
    expect(NODE).toContain('aria-current={mode === "run" && run?.phase === "moving" ? "step" : undefined}');
  });

  it("respects a request for less motion", () => {
    // Motion here is emphasis, not decoration; anyone who has asked for less must get less.
    const at = CSS_P.indexOf("@media (prefers-reduced-motion: reduce)");
    expect(at).toBeGreaterThan(-1);
    expect(CSS_P.slice(at, at + 260)).toContain("animation: none");
  });

  it("draws a phase that never happened as dashed, not green", () => {
    expect(FLOW_CSS).toMatch(/\.wf-node\[data-phase="skipped"\] \{[^}]*border-style: dashed;/);
  });

  it("opens on the step that wants attention", () => {
    // Landing on "research succeeded" while a gate three steps along waits for you is the
    // wrong answer to "what is happening".
    expect(DW).toContain(
      "steps.find(needsYou) ?? stoppedStep(run) ?? steps.find((s) => phaseOf(s) === \"now\")");
  });

  it("names a step by what it IS, from the engine's registry", () => {
    // `spec_review` is a step id; "Approve the spec" is what it does. The run's lane names a
    // step exactly as the editor does (stepView), from the run's own steps until the definition
    // it ran has loaded.
    const spec = runSpec([{ step_id: "spec_review", kind: "capability", capability: "approve_spec" },
                          { step_id: "odd", kind: "capability", capability: null }]);
    const palette = runPalette([{ key: "approve_spec", label: "Approve the spec" } as never], []);
    expect(stepView(spec.steps[0], palette).title).toBe("Approve the spec");
    expect(spec.steps[0].next).toEqual(["odd"]);
    expect(spec.entry_step).toBe("spec_review");
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

describe("following a link never logs you out", () => {
  const SHELL_SRC = codeOnly("app/ConsoleShell.tsx");
  const DW_SRC = codeOnly("app/delivery-workflows-ui.tsx");

  it("restores identity at the one place every in-console link passes through", () => {
    // The console renders "Select your organization" for any URL without `?org=`. Links
    // written as `?area=…` dropped it, so following one mid-session read as being logged out.
    // Fixed in the interceptor so a link written without it cannot break the session.
    expect(SHELL_SRC).toContain("for (const key of IDENTITY_PARAMS)");
    expect(SHELL_SRC).toContain("url.searchParams.set(key, mine)");
  });

  it("and the workflow's own step links build from the current URL", () => {
    expect(DW_SRC).toContain("consoleHref(here, params)");
    expect(DW_SRC).not.toContain("href: `?area=code_tasks&run=");
  });
});

describe("adding a feature to a project", () => {
  const CT_UI = codeOnly("app/code-tasks-ui.tsx");
  const CSS_IT = codeOnly("app/globals.css");

  it("offers starting points instead of a blank box", () => {
    // The first thing anyone does with an app they have just built is add to it, and the panel
    // was an empty textarea whose placeholder was the only guidance.
    expect(CT_UI).toContain("const FEATURE_TEMPLATES");
    expect(CT_UI).toContain('data-testid="ct-templates"');
    expect(CSS_IT).toContain(".ct-template {");
  });

  it("keeps templates to one entry each, so adding one touches nothing else", () => {
    // A template is data. If adding "an API endpoint" meant editing markup, nobody would.
    const list = CT_UI.slice(CT_UI.indexOf("const FEATURE_TEMPLATES"),
                             CT_UI.indexOf("// Feedback is conversational"));
    for (const key of ["page", "api", "data", "auth", "design", "bug"]) {
      expect(list).toContain(`key: "${key}"`);
    }
    expect(CT_UI).toContain("FEATURE_TEMPLATES.map((t) =>");
  });

  it("fills the fields in and leaves them editable", () => {
    // A template that cannot be edited is a form that argues with you.
    expect(CT_UI).toContain("value={message} onChange=");
    expect(CT_UI).toContain("onChange={(e) => setExpected(e.target.value)}");
  });

  it("lets the change follow a workflow, like a new project does", () => {
    // Adding a feature is a piece of work like any other and deserves the same choice.
    expect(CT_UI).toContain('data-testid="ct-iterate-workflow"');
    expect(CT_UI).toContain('name="workflow_version_id"');
  });

  it("is styled rather than assembled from inline rules", () => {
    expect(CT_UI).toContain('className="ct-iterate"');
    expect(CSS_IT).toContain(".ct-iterate {");
    expect(CSS_IT).toContain(".ct-proposal {");
  });
});

describe("managing runs, not just watching them", () => {
  const DW2 = codeOnly("app/delivery-workflows-ui.tsx");
  const CSS2 = codeOnly("app/globals.css");

  it("does not file waiting, stopped and finished runs under 'Running'", () => {
    // Every run sat under one heading, so a run stopped dead and a run waiting on a person
    // looked like a run making progress.
    expect(DW2).toContain("const BUCKETS =");
    for (const b of ["needs_you", "running", "blocked", "done"]) {
      expect(DW2).toContain(`"${b}"`);
    }
    expect(DW2).toContain('data-testid={`dw-filter-${b}`}');
  });

  it("puts a line between one run and the next", () => {
    expect(CSS2).toContain(".dw-runs .dw-run {");
    expect(CSS2).toContain('.dw-runs .dw-run[data-status="waiting"]');
  });

  it("says what Advance actually did", () => {
    // It reported success while advancing nothing — a run waiting on a person cannot be
    // ticked forward — and a button that always says "done" reads as one that does nothing.
    // REWRITTEN: the button no longer calls the worker-only pump (403 on authed installs); it
    // re-checks each unfinished run. The protected behaviour is the same — it says what it did.
    expect(DW2).toContain(
      "Nothing could move yet — each run is waiting on a person or on work in flight.");
    expect(DW2).toContain("Put ${requeued} step");
  });

  it("can put a failed step back in the queue", () => {
    // One component carries Try again (and Skip) for the step's detail and the run card; its
    // test ids are prefixed per place, "dw-step" by default.
    expect(DW2).toContain("data-testid={`${idPrefix}-retry`}");
    expect(DW2).toContain('idPrefix = "dw-step"');
    expect(DW2).toContain('"retry_step"');
  });

  it("can start the whole workflow again", () => {
    expect(DW2).toContain('testId: "dw-rerun"');
    // The label is the engine's, sent with the run as one of its `actions`.
    expect(readFileSync("../../engine/boss_engine/services/workflow_run_actions.py", "utf8"))
      .toContain('_verdict("rerun", "Run this workflow again", ENABLED)');
  });

  it("lets the release decision say WHERE it ships", () => {
    // A gate that only says yes/no cannot express "yes, to staging".
    expect(DW2).toContain('data-testid="dw-deploy-target"');
    expect(DW2).toContain('"set_input"');
  });

  it("links to the panel that ran, not the list of every panel", () => {
    expect(DW2).toContain('link({ area: "meetings", meeting })');
  });

  it("reads the preview the tool recorded as a fallback", () => {
    // start_preview nests its result under `preview`, and that is what the step carries when
    // the API has resolved no live one — it still has to become a link the same way.
    //
    // The nested shape used to be unpacked inline here, and this asserted on that source text.
    // Unpacking it by hand is what let `status: "running"` be hardcoded over a preview that had
    // FAILED, so the rule now lives in lib/previews as previewRecordFromStepOutput and is
    // tested on its behaviour in a-failed-preview-offers-no-link.test.ts. What this file can
    // still say is that the step uses it rather than growing a second copy.
    expect(DW2).toContain("previewRecordFromStepOutput(output)");
    expect(DW2).not.toContain('status: "running"');
    expect(DW2).toContain("open the preview");
  });

  it("names the pull request by number instead of a truncated url", () => {
    expect(DW2).toContain("pull request #${number}");
  });
});

describe("deciding a release, and where it ships", () => {
  const DW3 = codeOnly("app/delivery-workflows-ui.tsx");
  const CSS3 = codeOnly("app/globals.css");

  it("offers a HOST when the app has no target of its own", () => {
    // deploy_app refuses without an active target and never creates infrastructure, so a
    // project built for the first time had nowhere to go — and the gate could only offer
    // targets belonging to other apps, which would have shipped this one over somebody else's.
    expect(DW3).toContain("function DeployWhere");
    expect(DW3).toContain('data-testid="dw-deploy-host"');
    expect(DW3).toContain('"prepare_target"');
  });

  it("never offers another app's target", () => {
    expect(DW3).toContain("targets.filter((t) => t.app_name === appName)");
  });

  it("says so plainly when there is nowhere to ship", () => {
    // Better than an empty dropdown, which reads as a broken control.
    expect(DW3).toContain('data-testid="dw-deploy-nowhere"');
    expect(DW3).toContain("no verified host");
  });

  it("names who signs it off — once", () => {
    // The PROPERTY: before following the link, the operator can see whether the decision is
    // theirs. That used to be asserted at the button, which is where the name appeared for the
    // FOURTH time on one screen — run header, step badge, gate chip, button note. It is now on
    // the step header chip, which is above the button and cannot be scrolled away from it.
    expect(DW3).toContain('`needs ${approval.approver_role || "a person"}`');
    expect(DW3).not.toContain("signs this off");
  });

  it("reads a denial as a decision rather than a fault", () => {
    // "step 'deploy' was rejected" in crash-red, with no hint of what happens next.
    expect(DW3).toContain("const DENIED =");
    expect(DW3).toContain("You denied ${");
    expect(CSS3).toContain('.dw-blocked[data-denied="true"]');
  });

  it("builds the preview link the way Factory does, rather than a fourth way", () => {
    // Three times this was wrong, three different ways: no link at all; a link to a preview
    // that had expired; a link to 127.0.0.1:42543 — where the app really is listening, on the
    // host, which no browser elsewhere can reach. operatorPreviewOpenHref already encodes the
    // whole rule (refuse expired, refuse loopback, else the API's /v1/previews/<id>/web/
    // proxy). Using it is the fix; writing another version of it is how this kept recurring.
    expect(DW3).toContain("operatorPreviewOpenHref(record)");
    // ...and the gateway path stays on the console's own origin (lib/browser-links): prefixing it
    // with the server's API address sent boss-1's operator to their own laptop's port 8000.
    expect(DW3).toContain("browserHref(href)");
    expect(DW3).not.toContain("PREVIEW_API");
    expect(DW3).not.toContain('str("live_preview_url")');
  });
});

describe("Build starts the workflow, not a panel", () => {
  const BUILDS = codeOnly("app/api/builds/route.ts");
  const HERO2 = codeOnly("app/ask-boss-hero.tsx");
  const DW_WAIT = codeOnly("app/delivery-workflows-ui.tsx");

  it("starts a workflow run on the press", () => {
    // This posted straight to /v1/meetings/prd: a panel convened, a PRD was drafted, somebody
    // approved it, and only THEN did a workflow run appear — a record written after the fact,
    // adopting a meeting and an approval that had already happened. Which is why its first
    // three steps read 20s, 0s and "not run", and why nothing showed for several minutes.
    expect(BUILDS).toContain("/v1/delivery-workflows/start");
    expect(BUILDS).not.toContain("/v1/meetings/prd");
    expect(BUILDS).toContain('work_class: "project.new"');
  });

  it("gives the run the directive to work from", () => {
    expect(BUILDS).toContain("input: { title, directive }");
  });

  it("takes the operator to the workflow rather than telling them to navigate", () => {
    // "Open Workflow to watch it" asked somebody to do the navigation the press implied — and
    // gave them no way to know the run had appeared, since the page they were on shows none.
    expect(HERO2).toContain('url.searchParams.set("area", "delivery_workflows")');
    expect(HERO2).toContain('url.searchParams.set("wfr", runId)');
  });

  it("waits visibly for a run that is still being created", () => {
    // Arriving from Build, the run may not be in the list yet — an empty list reads as
    // "nothing happened".
    expect(DW_WAIT).toContain('data-testid="dw-awaiting"');
  });
});

describe("the panel argues from the research", () => {
  const CAT = codeOnly("../../engine/boss_engine/services/workflow_catalogue.py");

  it("binds the dossier into the meeting step", () => {
    // Research produced a cited dossier and the panel that followed debated the original
    // one-line directive as though none of it had happened.
    expect(CAT).toContain('"research": _from("research", "dossier")');
  });
});
