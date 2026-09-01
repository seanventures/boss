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
    // "decline" was rejected by the server, which only accepts approve|reject|revise|
    // delegate|execute — so Deny threw "Unsupported decision action" and did nothing.
    expect(DW).toContain('value="reject"');
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
    expect(DW2).toContain("Nothing to advance — every run is finished or waiting on a person.");
    expect(DW2).toContain("Advanced ${moved.length} step");
  });

  it("can put a failed step back in the queue", () => {
    expect(DW2).toContain('data-testid="dw-step-retry"');
    expect(DW2).toContain('"retry_step"');
  });

  it("can start the whole workflow again", () => {
    expect(DW2).toContain('data-testid="dw-rerun"');
    expect(DW2).toContain("Run this workflow again");
  });

  it("lets the release decision say WHERE it ships", () => {
    // A gate that only says yes/no cannot express "yes, to staging".
    expect(DW2).toContain('data-testid="dw-deploy-target"');
    expect(DW2).toContain('"set_input"');
  });

  it("links to the panel that ran, not the list of every panel", () => {
    expect(DW2).toContain('link({ area: "meetings", meeting })');
  });

  it("shows the preview url the tool actually returns", () => {
    // start_preview nests it under `preview.url`; this looked only for a flat `preview_url`,
    // so the one link an operator most wants never appeared.
    expect(DW2).toContain("output.preview as { url?: string }");
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

  it("shows the press happening", () => {
    // Approve and Deny were plain buttons on a server action that takes seconds: the page
    // looked identical before and after, which is how people end up pressing Deny twice.
    expect(DW3).toContain('data-testid="dw-deny"');
    expect(DW3).toContain('data-testid="dw-approve"');
    expect(DW3).toContain("SubmitButton");
  });

  it("reads a denial as a decision rather than a fault", () => {
    // "step 'deploy' was rejected" in crash-red, with no hint of what happens next.
    expect(DW3).toContain("const DENIED =");
    expect(DW3).toContain("You denied ${");
    expect(CSS3).toContain('.dw-blocked[data-denied="true"]');
  });

  it("links the preview that is running, not the one that has expired", () => {
    // The step recorded the preview live WHEN IT RAN; previews expire and are replaced, so the
    // recorded one answers "preview not running (status=expired)".
    expect(DW3).toContain('str("live_preview_url")');
    expect(DW3).toContain("live_preview_url?: string | null;");
  });
});

describe("Build starts the workflow, not a panel", () => {
  const BUILDS = codeOnly("app/api/builds/route.ts");
  const HERO2 = codeOnly("app/ask-boss-hero.tsx");

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

  it("tells the operator a run started, not a panel", () => {
    expect(HERO2).toContain("A workflow run is under way");
    expect(HERO2).toContain("open **Workflow** to watch it");
  });
});
