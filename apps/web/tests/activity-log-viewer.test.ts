import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * The Activity Log, as a log viewer rather than a table of rows that happen to be logs.
 *
 * The thing it exists to answer: a build runs for forty to ninety minutes and its longest phase
 * used to be one row — "solo, claude-sonnet-5, 45 turns". An operator watching a build had
 * nothing to read. These pin the parts of the viewer that make that phase legible, and the
 * parts that stop it from burying everything else.
 */
const UI = codeOnly("app/activity-log-ui.tsx");
const LIB = codeOnly("lib/activity-log.ts");
const ROUTE = codeOnly("app/api/activity/route.ts");
const CSS = codeOnly("app/globals.css");
const SHELL = codeOnly("app/ConsoleShell.tsx");
const EDITION = codeOnly("lib/edition.ts");

describe("it reaches the new streams", () => {
  it("knows the conversation and the model calls are sources", () => {
    expect(UI).toContain('"llm_call"');
    expect(UI).toContain('"agent_turn"');
  });

  it("offers BOSS's own infrastructure as one filter", () => {
    // "Is BOSS healthy" and "is my project healthy" are different questions.
    expect(UI).toContain('value: "infra"');
  });

  it("forwards every filter through the route handler", () => {
    // A param missing here is dropped silently, so the filter narrows nothing and looks broken.
    for (const param of ["level", "source", "run_id", "agent"]) {
      expect(ROUTE).toContain(`"${param}"`);
      expect(LIB).toContain(`"${param}"`);
    }
  });
});

describe("levels make the default view readable", () => {
  it("has the four levels, loudest first in the UI", () => {
    expect(UI).toContain('const LEVELS = ["error", "warn", "info", "verbose"] as const');
  });

  it("hides verbose by default", () => {
    // Per-turn capture writes hundreds of rows per build, most of them a model thinking out
    // loud. Opening on all of them buries what people came to find.
    expect(UI).toContain("useState(true)");
    expect(UI).toContain("hideVerbose");
  });

  it("says how many rows it is hiding", () => {
    // A viewer that quietly drops most of what it loaded reads as one that found nothing.
    expect(UI).toContain("hidden)`");
  });

  it("puts a count on every level chip, including zero", () => {
    expect(UI).toContain("levelCounts[value] ?? 0");
  });

  it("shows the level as a WORD, not only a colour", () => {
    // Colour does not survive a screenshot, a printout, or a colour-blind reader — and this is
    // the column people scan.
    expect(UI).toContain("(item.level || \"info\").toUpperCase()");
    expect(CSS).toContain('.alog-tag[data-level="error"]');
  });

  it("defaults a level-less row to info rather than dropping it", () => {
    // An API that predates levels must not have its rows vanish from every filter.
    expect(LIB).toContain('asText(value.level) || "info"');
  });
});

describe("a turn can be read", () => {
  it("shows the first line of a turn inline", () => {
    // Without it every transcript row reads "Turn Completion" and the log is a list of shapes.
    expect(UI).toContain("firstLine(item.body)");
  });

  it("expands in place instead of opening a dialog", () => {
    // Reading a build means reading turns in order; a modal covering the list breaks the
    // sequence you came for.
    expect(UI).toContain("alog-body-row");
    expect(UI).toContain('data-testid="activity-turn-body"');
  });

  it("says when a turn was clipped, and how big it really was", () => {
    // A clipped turn must never read as a short one.
    expect(UI).toContain("content_bytes");
    expect(UI).toContain("Clipped for storage");
  });

  it("renders the body as output, not prose", () => {
    const rule = CSS.slice(CSS.indexOf(".alog-body {"), CSS.indexOf(".alog-truncated"));
    expect(rule).toContain("var(--mono, monospace)");
    expect(rule).toContain("white-space: pre-wrap");
  });
});

describe("scoping to one work task", () => {
  it("reads the run from the URL on the client", () => {
    // The Factory links here with pushState, which never re-runs the server component that
    // renders this one — a prop threaded from there would work on reload and do nothing on the
    // click that matters.
    expect(UI).toContain('new URLSearchParams(window.location.search).get("run")');
    expect(UI).toContain('window.addEventListener("popstate", read)');
  });

  it("announces a filter the operator did not set here", () => {
    // Arriving from a run page, the log is already narrowed. Unsaid, it reads as though the
    // whole project only ever did this.
    expect(UI).toContain('data-testid="activity-scope"');
    expect(UI).toContain("Show the whole project");
  });

  it("the run page links across with the run id", () => {
    const factory = codeOnly("app/code-tasks-ui.tsx");
    expect(factory).toContain('url.searchParams.set("area", "activity")');
    expect(factory).toContain('data-testid="ct-detail-logs"');
  });
});

describe("where it lives", () => {
  it("sits INSIDE Observability, not beside it", () => {
    // Observability is not one screen. It is a place you go with a question — is it up, and
    // what happened — and those are two screens. Listed as unrelated siblings, the log read as
    // a separate product rather than the thing you open when a dashboard says something is
    // wrong.
    const monitoring = SHELL.slice(SHELL.indexOf('id: "monitoring"'));
    expect(monitoring.slice(0, monitoring.indexOf("},"))).toContain('parent: "observability"');
    const activity = SHELL.slice(SHELL.indexOf('id: "activity"'));
    expect(activity.slice(0, activity.indexOf("},"))).toContain('parent: "observability"');
    // The old top-level "Observability" label moved to the section; the area is Monitoring.
    expect(SHELL).toContain('observability: { label: "Observability"');
    expect(SHELL).toContain('id: "monitoring", label: "Monitoring"');
  });

  it("the section is a disclosure, not a destination", () => {
    // It has no screen. Rendering it as a nav target would send an operator somewhere that
    // does not exist.
    const parent = SHELL.slice(SHELL.indexOf("bx-nav-parent"));
    expect(parent).toContain("aria-expanded={open}");
    expect(parent.slice(0, parent.indexOf("</button>"))).not.toContain("aria-current");
  });

  it("never hides the page you are on", () => {
    // Collapsing a section that holds the active area is the "where am I" bug: the nav stops
    // answering the one question it exists for.
    expect(SHELL).toContain("open || holdsActive ?");
    expect(SHELL).toContain("siblings.filter((x) => x.id === active)");
  });

  it("starts open", () => {
    // A section that starts closed hides a newly-added area from anyone who does not already
    // know it is there — indistinguishable from not having shipped it.
    expect(SHELL).toContain('new Set<NavSectionId>(["observability"])');
  });

  it("is visible to self-hosted operators", () => {
    // They have no access to our infrastructure, so this is the only way they can debug their
    // own builds.
    const set = EDITION.slice(EDITION.indexOf("V1_COMMUNITY_PRIMARY_NAV = new Set("));
    expect(set.slice(0, set.indexOf("]);"))).toContain('"activity"');
  });
});
