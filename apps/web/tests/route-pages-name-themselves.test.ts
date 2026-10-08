import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";
import { shellHeading, shownArea } from "../lib/shell-heading";

/**
 * A ROUTE PAGE SAYS WHAT IT IS. Identity › Groups opened with "Command Center" in the top bar and
 * two lit rail rows: the area remembered from the area page (localStorage "boss.area") won over
 * the route. The route decides on /identity and /account; ?area= and the remembered area decide
 * only on the area page.
 */
const AREAS = [
  { id: "overview", label: "Command Center", sub: "What needs you" },
  { id: "code_tasks", label: "Factory", sub: "Builds" },
  { id: "enterprise", label: "Identity", sub: "Who can do what, and for whom" },
];
const known = AREAS.map((a) => a.id);

describe("which area a page shows", () => {
  it("a route page shows its own area whatever was remembered or asked for", () => {
    for (const stored of ["overview", "code_tasks", null]) {
      for (const requested of ["overview", "code_tasks", null]) {
        expect(shownArea({ routeMode: true, routeArea: "enterprise", requested, stored, known, fallback: "overview" })).toBe("enterprise");
        // My account has no rail row: nothing is lit.
        expect(shownArea({ routeMode: true, routeArea: null, requested, stored, known, fallback: "overview" })).toBeNull();
      }
    }
  });

  it("the area page follows ?area=, then the remembered area", () => {
    expect(shownArea({ routeMode: false, routeArea: null, requested: "code_tasks", stored: "enterprise", known, fallback: "overview" })).toBe("code_tasks");
    expect(shownArea({ routeMode: false, routeArea: null, requested: null, stored: "enterprise", known, fallback: "overview" })).toBe("enterprise");
    expect(shownArea({ routeMode: false, routeArea: null, requested: "nope", stored: null, known, fallback: "overview" })).toBe("overview");
  });
});

describe("the top bar's title", () => {
  it("names the route's area on /identity, even when the area page last showed Command Center", () => {
    expect(shellHeading(AREAS, { routeMode: true, routeArea: "enterprise", active: "overview" }).label).toBe("Identity");
  });
  it("uses the route's own title on /account", () => {
    const title = { label: "My account", sub: "Your sign-in" };
    expect(shellHeading(AREAS, { routeMode: true, routeArea: null, active: "overview", title })).toEqual(title);
  });
  it("names the shown area on the area page", () => {
    expect(shellHeading(AREAS, { routeMode: false, routeArea: null, active: "code_tasks" }).label).toBe("Factory");
  });
  it("is what the shell renders, and the shell never lets a remembered area repaint a route page", () => {
    const shell = codeOnly("app/ConsoleShell.tsx");
    expect(shell).toMatch(/\{heading\.label\}/);
    expect(shell).not.toMatch(/title\?\.label \?\? activeDef\.label/);
    expect(shell).toMatch(/shownArea\(\{\s*routeMode/);
    expect(shell).toMatch(/const onPop = \(\) => \{\s*if \(routeMode\) return;/);
  });
});
