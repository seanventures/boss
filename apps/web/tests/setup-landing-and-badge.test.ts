/**
 * First sign-in lands on Repo and models while setup is not ready, and the rail badge counts
 * what is unfinished or failing — both from the same GET /v1/setup the board renders.
 */
import { describe, expect, it } from "vitest";

import { landingArea, summarize } from "../lib/setup-server";
import { codeOnly } from "./_helpers/source";
import { board, everyRed, firstRun, repoDone } from "./_helpers/setup-fixtures";

describe("landing", () => {
  // BOSSAPP-808 (PARITY-808.md): a new install lands on Get started, its own area.
  it("opens Projects (its first run, which absorbed Get started) when nothing is asked for and setup is not ready", () => {
    expect(landingArea(undefined, summarize(firstRun()))).toBe("projects");
    expect(landingArea("", summarize(repoDone()))).toBe("projects");
  });

  it("an older summary without get_started still opens Repo and models", () => {
    const { get_started: _drop, ...older } = summarize(firstRun());
    expect(landingArea(undefined, older)).toBe("setup");
  });

  it("never overrides an area the operator asked for, and leaves a ready install alone", () => {
    expect(landingArea("workforce", summarize(firstRun()))).toBeNull();
    expect(landingArea(undefined, summarize(board()))).toBeNull();
    expect(landingArea(undefined, null)).toBeNull();          // the API did not answer
  });

  it("is the area this render shows and the shell opens — rendered, not redirected", () => {
    const PAGE = codeOnly("app/page.tsx");
    expect(PAGE).toContain("landingArea(requestedArea, setupSummary)");
    expect(PAGE).toContain("requested: landing ?? shownArea");
    expect(PAGE).toContain("initialArea={initialArea}");
    expect(PAGE).not.toMatch(/\bredirect\(/);
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
    expect(SHELL).toContain('if (id === "setup" && setupBadge && setupBadge.count > 0)');
    expect(SHELL).toContain("window.addEventListener(SETUP_BADGE_EVENT, onBadge)");
    expect(SHELL).toContain('sub: "Where BOSS writes code and what it thinks with"');
  });
});

describe("a render never waits on Repo and models", () => {
  it("gives up on a hung summary within its time box", async () => {
    const { fetchSetupSummary, SUMMARY_TIMEOUT_MS } = await import("../lib/setup-server");
    const original = globalThis.fetch;
    globalThis.fetch = ((_url: unknown, init?: RequestInit) => new Promise((_, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new Error("aborted")));
    })) as typeof fetch;
    try {
      const started = Date.now();
      expect(await fetchSetupSummary("p")).toBeNull();
      expect(Date.now() - started).toBeLessThan(SUMMARY_TIMEOUT_MS + 1000);
    } finally {
      globalThis.fetch = original;
    }
  });
});
