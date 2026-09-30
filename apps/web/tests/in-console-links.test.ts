import { describe, expect, it } from "vitest";

import { isInConsoleLink } from "../app/console-href";

/**
 * Opening a PRD took ten seconds because an in-page link did a full navigation, which
 * re-renders and re-hydrates all fifty console areas. The panels already restore their
 * selection from the URL, so the round trip bought nothing.
 *
 * These pin both halves: the links we must intercept, and the ones we must NOT -- hijacking
 * a preview or a pull request would break the only way to see a finished app.
 */
const AREAS = ["meetings", "code_tasks", "discovery"] as const;
const here = { pathname: "/", currentPathname: "/", knownAreas: AREAS };

describe("in-console links are handled without a page load", () => {
  it("intercepts an area link", () => {
    expect(isInConsoleLink("?area=meetings", here)).toBe(true);
  });

  it("intercepts a deep link to one meeting — the 'open PRD' case", () => {
    expect(isInConsoleLink("?org=org_boss&area=meetings&meeting=mrun_1", here)).toBe(true);
  });

  it("leaves a preview URL alone: it is another origin entirely", () => {
    expect(isInConsoleLink("http://127.0.0.1:8000/v1/previews/prev_1/web/", here)).toBe(false);
  });

  it("leaves a link that opens in a new tab alone", () => {
    expect(isInConsoleLink("?area=meetings", { ...here, target: "_blank" })).toBe(false);
  });

  it("leaves links to a different page alone", () => {
    expect(isInConsoleLink("/?area=meetings", { ...here, pathname: "/build" })).toBe(false);
  });

  it("does not swallow a query link with no area", () => {
    expect(isInConsoleLink("?task=tsk_1", here)).toBe(false);
  });

  it("does not swallow an area this console does not have", () => {
    expect(isInConsoleLink("?area=nonsense", here)).toBe(false);
  });

  it("ignores an empty href", () => {
    expect(isInConsoleLink(null, here)).toBe(false);
  });
});
