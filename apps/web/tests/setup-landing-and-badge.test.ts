/**
 * First sign-in lands on Repo and models while setup is not ready, and the rail badge counts
 * what is unfinished or failing — both from the same GET /v1/setup the board renders.
 */
import { describe, expect, it } from "vitest";

import { landingArea, summarize } from "../lib/setup-server";
import { codeOnly } from "./_helpers/source";
import { board, everyRed, firstRun, repoDone } from "./_helpers/setup-fixtures";

describe("landing", () => {
  it("opens Repo and models when nothing is asked for and setup is not ready", () => {
    expect(landingArea(undefined, summarize(firstRun()))).toBe("setup");
    expect(landingArea("", summarize(repoDone()))).toBe("setup");
  });

  it("never overrides an area the operator asked for, and leaves a ready install alone", () => {
    expect(landingArea("workforce", summarize(firstRun()))).toBeNull();
    expect(landingArea(undefined, summarize(board()))).toBeNull();
    expect(landingArea(undefined, null)).toBeNull();          // the API did not answer
  });

  it("is applied before the shell renders, with the request's own parameters", () => {
    const PAGE = codeOnly("app/page.tsx");
    expect(PAGE).toContain("landingArea(requestedArea, setupSummary)");
    expect(PAGE).toContain('next.set("area", landing)');
  });
});

describe("the rail badge", () => {
  it("counts the unfinished rows while setup is not done", () => {
    expect(summarize(firstRun()).badge).toEqual({ count: 2, tone: "amber" });
    expect(summarize(repoDone()).badge).toEqual({ count: 1, tone: "amber" });
  });

  it("is red while a check fails, and gone when everything works", () => {
    expect(summarize(everyRed()).badge).toEqual({ count: 2, tone: "red" });
    expect(summarize(board()).badge).toBeNull();
  });

  it("is the shell's, on the Repo and models entry", () => {
    const SHELL = codeOnly("app/ConsoleShell.tsx");
    expect(SHELL).toContain('if (id === "setup" && status.setup && status.setup.count > 0)');
    expect(SHELL).toContain('sub: "Where BOSS writes code and what it thinks with"');
  });
});
