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
  });

  it("puts refresh feedback in the Live chip instead", () => {
    // Same chip, no layout shift, gone when it lands. Enough to answer "is it updating?"
    // without occupying the page.
    expect(SHELL).toContain('refreshPending ? "Syncing" : "Live"');
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
