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
  it("the painted area commits with the navigation, not with the click", () => {
    const go = SHELL.slice(
      SHELL.indexOf("const go = useCallback("),
      SHELL.indexOf("}, [router, startNav]);"),
    );
    expect(go.length).toBeGreaterThan(0);
    // The press must not flip the thing the CSS keys off.
    const firstStatement = go.slice(0, go.indexOf("startNav("));
    expect(firstStatement).not.toContain("setActive(");
    // It must flip inside the transition, alongside the push.
    const transition = go.slice(go.indexOf("startNav("));
    expect(transition).toContain("router.push(");
    expect(transition).toContain("setActive(id)");
  });

  it("but the rail still answers the press immediately", () => {
    // Deferring `active` must not defer the highlight too, or the click feels ignored.
    expect(SHELL).toContain("const selected = navTarget ?? active;");
    expect(SHELL).toContain("setNavTarget(id);");
    // Every rail highlight reads `selected`, not `active`. Scoped to the rail: `activeDef`
    // labels the <main> element and must keep describing the area actually painted there.
    const rail = SHELL.slice(SHELL.indexOf("bx-nav-item"));
    expect(rail).not.toMatch(/\ba\.id === active\b/);
    expect(rail).not.toMatch(/\bchild\.id === active\b/);
    expect(rail).toMatch(/\ba\.id === selected\b/);
    expect(rail).toMatch(/\bchild\.id === selected\b/);
    expect(SHELL).toContain("areasForEdition.find((a) => a.id === active)");
  });

  it("a navigation that will never come back still commits", () => {
    // Re-clicking the current area produces no URL change and therefore no navigation. If the
    // transition were the only path that sets `active`, the rail would sit pending forever.
    const go = SHELL.slice(
      SHELL.indexOf("const go = useCallback("),
      SHELL.indexOf("}, [router, startNav]);"),
    );
    const afterTransition = go.slice(go.indexOf("} else {"));
    expect(afterTransition).toContain("setActive(id)");
    // And the non-browser path, where the URL could not even be read.
    expect(go.slice(go.indexOf("} catch {"))).toContain("setActive(id)");
  });

  it("the trip is visible while it happens", () => {
    expect(SHELL).toContain("const [navPending, startNav] = useTransition();");
    expect(SHELL).toContain("bx-nav-progress");
    expect(SHELL).toContain('data-nav-pending={navPending ? "true" : undefined}');
    expect(CSS).toContain(".bx-nav-progress");
    expect(CSS).toContain('.bx-content[data-nav-pending="true"]');
  });

  it("the outgoing area is dimmed, not hidden", () => {
    // Hiding it is the bug. Anything that removes it from view — display:none, visibility,
    // opacity 0 — puts the operator back in front of an empty page.
    const pending = CSS.slice(CSS.indexOf('.bx-content[data-nav-pending="true"]'));
    const block = pending.slice(0, pending.indexOf("}"));
    expect(block).not.toContain("display: none");
    expect(block).not.toContain("visibility: hidden");
    const opacity = /opacity:\s*([\d.]+)/.exec(block)?.[1];
    expect(opacity).toBeTruthy();
    expect(Number(opacity)).toBeGreaterThan(0.5);
  });
});
