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
    expect(btn).toContain('searchParams.set("new", "1")');
  });

  it("is answered by the area that owns the form", () => {
    // The shell must not reach into the area's state, and the area must not know about the
    // shell. `?new=1` is the same handoff already used for `?run=` and `?agent=`.
    expect(CT).toContain('get("new") === "1"');
    expect(CT).toContain("setShowNew(true)");
  });

  it("only ever opens the form, never closes it", () => {
    // Clearing on an absent param would slam the form shut on someone part-way through filling
    // it in, for any navigation that happens to fire a popstate.
    const reader = CT.slice(CT.indexOf('get("new") === "1"') - 320,
                            CT.indexOf('get("new") === "1"') + 120);
    expect(reader).not.toContain("setShowNew(false)");
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
