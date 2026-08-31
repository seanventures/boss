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
