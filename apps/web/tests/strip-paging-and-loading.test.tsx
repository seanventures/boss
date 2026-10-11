import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { ProjectStrip } from "../app/project-strip";
import { codeOnly } from "./_helpers/source";

/**
 * Forty projects, four visible, and no sign there are more.
 *
 * The strip scrolled, but the only clue was a fade at its edge — which reads as a style, not a
 * control. An operator scanning for a project concluded the rest were not there. Alongside it,
 * the console streams its ~55 feeds, so a count reads "12 code tasks" for a moment before it
 * reads 200; watched with no indicator, that also reads as missing data.
 */
(globalThis as Record<string, unknown>).React = React;

const STRIP = codeOnly("app/project-strip.tsx");
const SHELL = codeOnly("app/ConsoleShell.tsx");
const CSS = codeOnly("app/globals.css");

describe("paging the strip", () => {
  it("offers arrows only when there is somewhere to go", () => {
    // Arrows that are always present and dead half the time teach an operator to ignore them.
    expect(STRIP).toContain("{(edges.left || edges.right) ?");
  });

  it("disables rather than hides the arrow at each end", () => {
    // A disappearing arrow makes the row reflow and moves the other one under the cursor.
    expect(STRIP).toContain("disabled={!edges.left}");
    expect(STRIP).toContain("disabled={!edges.right}");
    const rule = CSS.slice(CSS.indexOf(".bx-strip-page:disabled"));
    expect(rule.slice(0, 80)).toContain("opacity: .3");
  });

  it("pages by less than a full screen", () => {
    // A project sitting on the boundary must not be skipped past — losing your place is worse
    // than a shorter jump.
    expect(STRIP).toContain("el.clientWidth * 0.8");
  });

  it("clamps to the ends instead of scrolling into empty space", () => {
    expect(STRIP).toContain("Math.max(0, Math.min(el.scrollWidth - el.clientWidth, from + distance))");
  });

  it("does not rely on scroll-behavior: smooth", () => {
    // Measured in this console's own browser: scrollBy with behavior "auto" moved it 300 -> 700,
    // and the identical call with "smooth" moved it nothing at all. The control looked wired
    // and silently did nothing, which every markup-level test would have passed.
    expect(STRIP).not.toContain('behavior: "smooth"');
    expect(STRIP).toContain("window.requestAnimationFrame(step)");
  });

  it("cancels an animation already running", () => {
    // Two clicks in quick succession left two loops writing scrollLeft from different starting
    // points, and the strip measurably jumped BACKWARDS.
    expect(STRIP).toContain("window.cancelAnimationFrame(animation.current)");
  });

  it("jumps instead of animating when nobody can see it", () => {
    // Chrome stops servicing requestAnimationFrame in a hidden tab, so an animated scroll
    // started there simply never happens.
    expect(STRIP).toContain("document.hidden");
  });

  it("re-decides the arrows because it moved, not because an event fired", () => {
    // Derived purely from the scroll listener, the arrows were exactly backwards at the far
    // right — a hidden tab defers those events.
    const page = STRIP.slice(STRIP.indexOf("const page = useCallback"));
    expect(page.slice(0, page.indexOf("}, [measure]);"))).toContain("measure();");
  });

  it("does not re-render on every frame of a scroll", () => {
    // A fresh object per scroll event re-rendered a component inside a page of 74 app cards at
    // frame rate, and froze the renderer outright.
    expect(STRIP).toContain("current.left === left && current.right === right ? current");
  });
});

describe("the console says when it is still filling up", () => {
  it("renders the indicator on the SERVER", () => {
    // The window it exists for is before any client JS runs. A flag that only became true on
    // the client would miss all of it.
    const html = renderToStaticMarkup(<ProjectStrip projectId="boss-dogfood" />);
    expect(html).toContain("Loading");           // the strip's own three-state loading
    expect(SHELL).toContain("useState(true)");   // settling starts true, cleared after mount
  });

  it("clears shortly after hydration", () => {
    expect(SHELL).toContain("setSettling(false)");
  });

  it("does NOT stay up for the background refresh", () => {
    // `refreshPending` is true for the whole server re-render — 20 seconds and more here —
    // against an interval computed from that same cost, so the bar never went off. An
    // indicator that cannot be dismissed and explains nothing is furniture.
    expect(SHELL).toContain("{settling ? (");
    expect(SHELL).not.toContain("{settling || refreshPending ?");
    expect(SHELL).not.toContain("{settling || refreshing ?");
  });

  it("puts refresh feedback in the Live chip instead", () => {
    // Same chip, no layout shift, gone when it lands. Enough to answer "is it updating?"
    // without occupying the page.
    expect(SHELL).toContain('refreshing ? "Syncing" : "Live"');
  });

  it("is a bar, not a blocking overlay", () => {
    // The panels that HAVE arrived stay usable; covering them makes a fast console feel slower
    // than a slow one.
    expect(SHELL).toContain('className="bx-loading"');
    expect(SHELL).toContain('role="status"');
  });

  it("never invents a percentage", () => {
    // The console does not know how many feeds are outstanding, and a progress bar that makes
    // a number up is worse than one that only says "still working".
    const rule = CSS.slice(CSS.indexOf(".bx-loading-bar {"), CSS.indexOf("@media (max-width: 900px) { .bx-loading"));
    expect(rule).toContain("animation: bx-loading-sweep");
    expect(rule).not.toContain("width: var(--progress");
  });

  it("still reads as busy with motion turned off", () => {
    // Anchored on the RULE, not on the comment above it — `codeOnly` strips comments, so a
    // slice bounded by one lands nowhere and asserts against an empty string.
    const at = CSS.indexOf(".bx-loading-bar::after { animation: none");
    expect(at).toBeGreaterThan(-1);
    expect(CSS.slice(at, at + 120)).toContain("width: 100%");
  });
});

/**
 * Three caps stacked between the database and the header, and none of them said so.
 *
 * Measured on reference host: 474 code-task runs, of which 244 are real work and 59 group into
 * apps. The strip asked for the endpoint's default of 40 and drew 40 as though that were all
 * of them; the overview only ever read the newest 400 runs, so apps beyond that window could
 * not be returned at any limit; and the Factory heading read "200 code tasks" for a project
 * that had 244. Each one silently converted "the first page" into "everything".
 */
describe("the strip does not pretend a page is the whole list", () => {
  const ROUTE = codeOnly("app/api/projects-strip/route.ts");

  it("asks for more than the endpoint's default of 40", () => {
    expect(ROUTE).toContain("limit=100");
  });

  it("forwards how many actually exist", () => {
    expect(ROUTE).toContain("total: d.total_projects");
  });

  it("says how much of a truncated project list is loaded", () => {
    expect(STRIP).toContain("{total > projects.length ?");
    expect(STRIP).toContain("Showing {projects.length} of {total}");
  });
});

describe("the strip keeps pulling", () => {
  it("refreshes on a timer, not once on mount", () => {
    // It fetched once and never again, so a project that started, gated or shipped after the
    // page opened never appeared — the header answered "what was happening when you arrived".
    expect(STRIP).toContain("setInterval");
    expect(STRIP).toContain("30_000");
  });

  it("refreshes when the tab is looked at again", () => {
    expect(STRIP).toContain("visibilitychange");
  });

  it("shows that it is syncing WITHOUT a bar across the top", () => {
    // The full-width refreshing bar was the thing that got called "very annoying", correctly:
    // it was on screen for the whole 20s+ re-render. The state belongs on the label.
    expect(STRIP).toContain('data-testid="bx-strip-syncing"');
    expect(CSS).toContain(".bx-strip-syncing");
  });

  it("does not blank a good strip when a later refresh fails", () => {
    // Losing the list you were reading, for a reason you cannot see, is worse than stale rows.
    expect(STRIP).toContain('if (mode === "first") {');
  });

  it("does not show the syncing chip during the very first load", () => {
    // The first load has its own state; showing both makes the header flicker twice.
    expect(STRIP).toContain('if (mode === "refresh") setRefreshing(true);');
  });
});

describe("the build feed reports a total and stays capped", () => {
  const FEED = codeOnly("lib/code-tasks.ts");
  const ROUTE = codeOnly("app/api/code-tasks/route.ts");

  it("falls back to what arrived when the API sends no total", () => {
    expect(FEED).toContain('typeof res.total === "number" ? res.total : items.length');
  });

  it("goes through a route handler with a cap", () => {
    expect(ROUTE).toContain("exclude_internal=true");
    expect(ROUTE).toContain("export const dynamic = \"force-dynamic\"");
    expect(ROUTE).toContain("Math.min(");
  });
});

describe("the loading bar stays up until the page has actually arrived", () => {
  const SHELL2 = codeOnly("app/ConsoleShell.tsx");

  it("waits for the document, not a fixed 1.2 seconds", () => {
    // Measured on reference host: the Factory page is 9.2 MB and streams for 10.4s. The bar cleared
    // 1200ms after hydration — early in that stream — so for the remaining ~9 seconds cards
    // kept appearing with nothing saying so. A list still arriving looks exactly like a list
    // that is short.
    expect(SHELL2).toContain('document.readyState === "complete"');
    expect(SHELL2).toContain('window.addEventListener("load"');
  });

  it("keeps a floor so a fast page does not blink it", () => {
    expect(SHELL2).toContain("window.setTimeout(r, 1200)");
  });

  it("keeps a ceiling, because an indicator that cannot go away is the last bug", () => {
    // The previous bar became permanent furniture that explained nothing.
    expect(SHELL2).toContain("window.setTimeout(finish, 60_000)");
  });

  it("shows elapsed seconds, so it reads as working rather than stuck", () => {
    expect(SHELL2).toContain('data-testid="console-loading-elapsed"');
    expect(SHELL2).toContain("settledFor >= 3 ?");
  });

  it("does not jitter the sentence as the number ticks", () => {
    expect(CSS).toContain("font-variant-numeric: tabular-nums");
  });
});
