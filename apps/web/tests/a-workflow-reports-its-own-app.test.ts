import { describe, expect, it } from "vitest";

import { identityKey } from "../app/app-identity";
import { targetsForThisApp } from "../app/delivery-workflows-ui";
import { codeOnly } from "./_helpers/source";

/**
 * The Meeting cost meter workflow said it was watching three apps.
 *
 *     Set up monitoring · succeeded
 *     watching Incident-Timeline Builder · staging, Meeting cost meter · staging,
 *              Vendor-Status-Watcher · staging
 *     monitor Incident-Timeline Builder   monitor Meeting cost meter   monitor Vendor-Status-Watcher
 *
 * A workflow run is about ONE app. Two thirds of that was somebody else's work reported as this
 * step's result — and once the step grew links, a cosmetic inaccuracy became a navigational one:
 * three prominent links to the wrong apps.
 *
 * The capability is fixed at the source, so new runs record only their own target. This filters
 * the DISPLAY as well, because a completed run's output is frozen: the run on screen lists all
 * three and always will, so without this the fix would not reach the thing that was reported.
 */
const UI = codeOnly("app/delivery-workflows-ui.tsx");

const TARGETS = [
  { app_name: "Incident-Timeline Builder", environment: "staging" },
  { app_name: "Meeting cost meter", environment: "staging" },
  { app_name: "Vendor-Status-Watcher", environment: "staging" },
];

describe("a step reports the run's own app", () => {
  it("the other apps are dropped", () => {
    const mine = targetsForThisApp(TARGETS, "Meeting cost meter");
    expect(mine.map((t) => t.app_name)).toEqual(["Meeting cost meter"]);
  });

  it("the slug a deploy records matches the name a target records", () => {
    // The deploy says `meeting-cost-meter`; the target says `Meeting cost meter`. Comparing
    // them literally would match nothing and blank every step.
    expect(targetsForThisApp(TARGETS, "meeting-cost-meter").map((t) => t.app_name))
      .toEqual(["Meeting cost meter"]);
  });

  it("punctuation is not identity", () => {
    // "Incident Timeline Builder." and "Incident-Timeline Builder" were once two apps with two
    // PRs and two bills. The same normalisation that fixed that decides this.
    expect(targetsForThisApp(TARGETS, "Incident Timeline Builder.").map((t) => t.app_name))
      .toEqual(["Incident-Timeline Builder"]);
  });
});

describe("what it does when it cannot tell", () => {
  it("a run with no name shows everything rather than nothing", () => {
    expect(targetsForThisApp(TARGETS, "")).toHaveLength(3);
  });

  it("a run whose app matches nothing shows what was recorded", () => {
    // An adopted run, or an app renamed after the step ran. Blanking a step that does have
    // something to say is a worse failure than over-reporting, because it looks like the step
    // did nothing.
    expect(targetsForThisApp(TARGETS, "Something Else Entirely")).toHaveLength(3);
  });

  it("an empty target list stays empty", () => {
    expect(targetsForThisApp([], "Meeting cost meter")).toEqual([]);
  });
});

describe("both places the targets are shown are filtered", () => {
  it("the links", () => {
    expect(UI).toContain("targetsForThisApp(output.targets as unknown[], runAppName)");
  });

  it("and the watching summary", () => {
    expect(UI).toContain("targetsForThisApp(out.targets, runAppName)");
  });

  it("the run's title is what identifies its app", () => {
    expect(UI).toContain('const runAppTitle = typeof runInput.title === "string"');
    expect(UI).toContain("stepLinks(merged, runAppTitle)");
    expect(UI).toContain("observedGates(step, runAppTitle)");
  });
});

describe("the two identity implementations agree", () => {
  // If the TS and Python halves disagree, the display filter hides rows the engine kept —
  // which is worse than showing the wrong ones, because nothing says anything is missing.
  it.each([
    ["Meeting cost meter", "meeting-cost-meter"],
    ["Incident-Timeline Builder", "Incident Timeline Builder."],
    ["Vendor-Status-Watcher", "vendor status watcher"],
    ["Shorty", "shorty"],
  ])("%s and %s are one app", (a, b) => {
    expect(identityKey(a)).toBe(identityKey(b));
  });

  it.each([
    ["Meeting cost meter", "Meeting cost meters"],
    ["HookWatch", "HookWatcher"],
  ])("%s and %s are not", (a, b) => {
    expect(identityKey(a)).not.toBe(identityKey(b));
  });

  it("it collapses to letters and digits, single-spaced", () => {
    expect(identityKey("  Foo—Bar!! 42 ")).toBe("foo bar 42");
  });
});
