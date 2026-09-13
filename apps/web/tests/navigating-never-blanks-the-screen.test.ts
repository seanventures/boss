import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * Clicking a nav item must never leave the operator looking at nothing.
 *
 * `main.bx-content[data-active=X]` reveals `section[data-area=X]` and hides every other
 * section. While all forty areas were in the payload, flipping `data-active` on the click was
 * instant — the next section was already in the DOM. Rendering one area per request changed
 * that: the incoming section does not exist until a server round trip completes, so setting
 * `active` on the click hid the outgoing area while the incoming one did not yet exist, and
 * the page was empty for the whole trip.
 *
 * Reported as: "Every option I choose on left pane it takes several seconds to render until
 * then it shows blank."
 *
 * The fix is not to make the trip faster — it is already ~0.3s server-side — but to stop
 * showing nothing during it. `setActive` moves inside the navigation transition, so React
 * keeps the outgoing tree painted until the incoming one is ready and commits both together.
 */
const SHELL = codeOnly("app/ConsoleShell.tsx");
const CSS = codeOnly("app/globals.css");

describe("switching areas keeps the screen populated", () => {
  it("the area commits on the press, because it is already on the page", () => {
    // THE TRANSITION IS GONE, AND THAT IS THE FIX, NOT A REGRESSION.
    //
    // Deferring `active` into a transition kept the outgoing area painted while the incoming
    // one was fetched. It was the right answer to a blank screen caused by rendering one area
    // per request — the section being opened did not exist yet, so there was genuinely
    // something to wait for.
    //
    // Every area is in the DOM again. There is nothing to fetch and nothing to wait for, so
    // `active` commits on the press and the reveal is a CSS toggle. A transition here would
    // add latency to a navigation that has none.
    const go = SHELL.slice(
      SHELL.indexOf("const go = useCallback("),
      SHELL.indexOf("}, []);", SHELL.indexOf("const go = useCallback(")),
    );
    expect(go.length).toBeGreaterThan(0);
    expect(go).toContain("setActive(id)");
    expect(go).not.toContain("startNav(");
    expect(go).not.toContain("router.push");
  });

  it("and the screen cannot blank, because nothing is unmounted", () => {
    // The blank had one cause: `main[data-active=X]` hid the outgoing section while the
    // incoming one did not exist. Both exist now, so the class of bug is gone rather than
    // worked around — which is why the pending hairline and the dim went with it.
    expect(SHELL).not.toContain("bx-nav-progress");
    expect(SHELL).not.toContain("data-nav-pending");
  });

  it("the rail still answers the press immediately", () => {
    expect(SHELL).toContain("const selected = navTarget ?? active;");
    expect(SHELL).toContain("setNavTarget(id);");
    const rail = SHELL.slice(SHELL.indexOf("bx-nav-item"));
    expect(rail).toMatch(/\ba\.id === selected\b/);
    expect(rail).toMatch(/\bchild\.id === selected\b/);
    expect(SHELL).toContain("areasForEdition.find((a) => a.id === active)");
  });

  it("hovering the rail does not fetch anything", () => {
    // PREFETCHING THE RAIL MAKES ONE CLICK INTO EIGHT RENDERS.
    //
    // The pointer must travel ACROSS every item above its target to reach it, and each one it
    // passes fired a full server-side area render. Reaching "Infra and Models" crosses seven
    // items. Measured live: infra alone 0.46s, infra during one pointer traverse 2.84s.
    //
    // Moot now that a click fetches nothing at all — and kept for exactly that reason, so the
    // idea cannot come back the next time navigation is made to reach the server again.
    expect(SHELL).not.toContain("router.prefetch");
    expect(SHELL).not.toMatch(/onMouseEnter=\{\(\) => warm\(/);
    expect(SHELL).not.toMatch(/prefetch=\{true\}/);
  });
});


/**
 * Going back to an area you just looked at must not cost a server round trip.
 *
 * Next 15 defaults the client router cache for dynamic pages to ZERO seconds. The console is
 * one dynamic page and the area is a search param, so every nav — including returning to the
 * area you were on ten seconds ago — threw away the payload it already had and fetched it
 * again. That is why EVERY menu click cost a round trip and not just the first, and why the
 * console felt slower than when all forty areas shipped at once and switching was a CSS
 * toggle over content already in the browser.
 *
 * Reported, repeatedly: "when I click on the side menu option it is taking really really
 * horrible time to load."
 */
describe("a visited area is not fetched again", () => {
  const CONFIG = readFileSync(join(process.cwd(), "next.config.ts"), "utf8");

  it("the client router cache is configured at all", () => {
    expect(CONFIG).toContain("staleTimes");
    expect(CONFIG).toMatch(/dynamic:\s*\d+/);
  });

  it("the dynamic stale time is long enough to make revisits instant", () => {
    // Zero is the Next default and the bug. Anything under a few seconds leaves a normal
    // back-and-forth between two areas refetching both every time.
    const dynamic = Number(/dynamic:\s*(\d+)/.exec(CONFIG)?.[1]);
    expect(dynamic).toBeGreaterThanOrEqual(30);
    // And not so long that an operator watching a run reads a stale screen as a live one.
    expect(dynamic).toBeLessThanOrEqual(300);
  });
});
