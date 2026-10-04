import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { ProjectStrip } from "../app/project-strip";
import { codeOnly } from "./_helpers/source";

/**
 * Every project across BOSS, in v1's header — v2's strip, brought over.
 *
 * v1 made you open Factory and scroll 74 cards to learn what was running. The strip answers
 * "what is happening right now" from inside whatever area you are already reading, and one
 * click opens the project.
 */
(globalThis as Record<string, unknown>).React = React;

const SRC = codeOnly("app/project-strip.tsx");
const ROUTE = codeOnly("app/api/projects-strip/route.ts");
const CSS = codeOnly("app/globals.css");

describe("the strip never claims an empty company", () => {
  it("says it is loading before it has asked", () => {
    // Rendering nothing while loading and nothing on failure tells the operator "no projects"
    // in both cases — the false claim of absence this console has made before.
    const html = renderToStaticMarkup(<ProjectStrip projectId="boss-dogfood" />);
    expect(html).toContain('data-state="loading"');
    expect(html).toContain("Builds");
    expect(html).toContain("Loading…");
    expect(html).not.toContain("No software projects yet");
  });

  it("has three distinct states, not two", () => {
    expect(SRC).toContain('useState<"loading" | "ready" | "failed">');
  });

  it("shows the REASON it failed, not a shrug", () => {
    // "Could not load" sends the operator to the API to discover what this line already knows.
    expect(SRC).toContain("Could not load software projects — {error}");
  });

  it("distinguishes 'no projects' from 'the filter is hiding them'", () => {
    // An empty strip under a filter reads as "there is nothing", and the operator forgets they
    // narrowed it two minutes ago.
    expect(SRC).toContain('"No software projects yet"');
    expect(SRC).toContain("in total`");
  });
});

describe("the filter", () => {
  it("offers the four states, ALL first", () => {
    expect(SRC).toContain('const FILTERS = ["ALL", "RUNNING", "GATED", "DONE"] as const');
  });

  it("counts STUCK as running", () => {
    // Work that started and has not finished. Filed under a tab nobody opens, a stalled project
    // stays invisible for a day.
    expect(SRC).toContain('RUNNING: (s) => s === "RUNNING" || s === "STUCK"');
  });

  it("shows a count on each tab, including zero", () => {
    // "GATED" alone does not say whether clicking it is worth doing.
    expect(SRC).toContain("bx-strip-filter-n");
    expect(SRC).toContain("{counts[f]}");
  });

  it("marks the active tab for assistive tech, not only in colour", () => {
    expect(SRC).toContain("aria-pressed={filter === f}");
  });
});

describe("clicking a project opens it", () => {
  it("switches to Factory and names the run", () => {
    expect(SRC).toContain("searchForProject(window.location.search, p.latest_run_id ?? null)");
  });

  it("does not carry a pending New-project request along with the click", () => {
    // It used to copy the whole query string, so a `?new=1` left by the top-bar button rode
    // along and the form reader re-fired on arrival — every project click landed on an empty
    // new-project dialog instead of the project. searchForProject drops one-shot commands;
    // the sequence is tested in url-intent.test.ts.
    expect(SRC).not.toContain("new URL(window.location.href)");
  });

  it("navigates client-side, like the rest of this console", () => {
    // A real navigation re-renders a tree that takes seconds to paint.
    expect(SRC).toContain("window.history.pushState");
    expect(SRC).toContain('new PopStateEvent("popstate")');
  });

  it("the Factory reads that param, or the click lands nowhere", () => {
    // The Factory reads `run=` as `build=` (and rewrites it), opening that build's cockpit.
    const factory = codeOnly("app/code-tasks-ui.tsx");
    expect(factory).toContain('q.get("build") || q.get("run")');
    expect(factory).toContain("window.addEventListener(\"popstate\", read)");
  });
});

describe("it does not slow the console down", () => {
  it("fetches through a route handler, not a Server Action", () => {
    // v1 already issues ~82 requests per render; a Server Action reply would re-render the
    // whole tree to draw a header strip.
    expect(SRC).toContain("/api/projects-strip?project_id=");
    expect(ROUTE).toContain('export const dynamic = "force-dynamic"');
  });

  it("sends only what the strip draws", () => {
    // The overview also carries burn, gates and autonomy — bytes on every refresh.
    expect(ROUTE).toContain("projects: d.projects ?? []");
  });

  it("holds the API key the browser does not have", () => {
    expect(ROUTE).toContain("authHeaders(");
  });
});

describe("presentation", () => {
  it("names projects the same way their cards do", () => {
    // A strip saying "PRD — Pull‐Request Review‐Time Tracker" beside a card saying
    // "Pull-Request Review-Time Tracker" is worse than either alone.
    expect(SRC).toContain("appIdentity({ title: p.title })");
  });

  it("scrolls the projects without pushing the filter off-screen", () => {
    const rule = CSS.slice(CSS.indexOf(".bx-strip-scroll {"), CSS.indexOf(".bx-strip-item {"));
    expect(rule).toContain("overflow-x: auto");
    expect(CSS).toContain(".bx-strip-filters { flex: 0 0 auto;");
  });

  it("sticks directly under the top bar, with no gap to show the page through", () => {
    const rule = CSS.slice(CSS.indexOf(".bx-strip {"), CSS.indexOf(".bx-strip-label {"));
    expect(rule).toContain("position: sticky");
    expect(rule).toContain("top: 62px");          // exactly .bx-topbar's height
  });

  it("never leaves colour as the only carrier of status", () => {
    // The stage and percentage sit beside the dot in the same chip.
    expect(SRC).toContain("bx-strip-stage");
    expect(SRC).toContain("bx-strip-pct");
  });
});
