/**
 * What a fresh demo run shows, read right (rc10 independent tester, 2026-10-12).
 *
 * Each case here is one defect the tester saw on screen, pinned so it cannot come back:
 * a security banner covered by the project strip, durations in raw minutes, buttons run
 * together.
 */
import { describe, expect, it } from "vitest";

import { humanDuration } from "../lib/datetime";
import { codeOnly, cssOnly } from "./_helpers/source";

describe("a security notice is never covered", () => {
  it("the project strip is not offset over the page's first line", () => {
    const css = [cssOnly("app/globals.css"), cssOnly("app/design-system.css")].join("\n");
    // Every rule for the strip itself: none may shift it down over the banner below it.
    const rules = [...css.matchAll(/\.bx-strip\s*\{([^}]*)\}/g)].map((m) => m[1]);
    expect(rules.length).toBeGreaterThan(0);
    for (const body of rules) expect(body).not.toMatch(/(^|;)\s*top:\s*\d/);
  });
});

describe("one human duration everywhere", () => {
  it("reads as a person says it", () => {
    expect(humanDuration(42)).toBe("42s");
    expect(humanDuration(725)).toBe("12m 5s");
    expect(humanDuration(857 * 60)).toBe("14h 17m");
    expect(humanDuration(2011.4 * 60)).toBe("1d 9h");
    expect(humanDuration(-1)).toBe("");
  });

  it("the run report and the step log use it, never raw minutes", () => {
    expect(codeOnly("app/run-report-ui.tsx")).toContain("humanDuration(");
    expect(codeOnly("app/run-report-ui.tsx")).not.toMatch(/elapsed_minutes\}\s*min/);
    expect(codeOnly("app/workflows/step-run.tsx")).not.toMatch(/secs \/ 60\)\}m/);
  });
});

describe("an approval card says names and sentences", async () => {
  const { approverName, impactWord, reasonInWords } = await import("../app/decision-inbox-ui");

  it("names who approved, never an id", () => {
    expect(approverName({ approvedBy: "usr_kdzhawzy7", approvedByName: "Paramaguru Gnanaguru" }))
      .toBe("Paramaguru Gnanaguru");
    expect(approverName({ approvedBy: "ceo", approvedByName: null })).toBe("the owner");
    expect(approverName({ approvedBy: "usr_unknown1", approvedByName: null })).toBe("a person");
  });

  it("says a reason code as words", () => {
    expect(reasonInWords("host_deploy_requires_approval")).toBe("a deploy to a host needs a person's approval");
    expect(reasonInWords("missing_reservation")).toBe("no budget was reserved for this work");
    expect(reasonInWords("Something a person wrote.")).toBe("Something a person wrote.");
  });

  it("never cuts a chip's word in half", () => {
    expect(impactWord("Create budget reservation for the build")).toBe("Create budget");
    expect(impactWord("governance gate")).toBe("governance gate");
  });
});

describe("a machine's card says each thing once, and all of it on hover", () => {
  it("a host named by its address shows the address once; a long name has a tooltip", () => {
    const src = codeOnly("app/infra/rack.tsx");
    expect(src).toContain("rack.address && rack.address !== rack.name");
    expect(src).toMatch(/className="inf-trunc" data-copy="data" title=\{rack\.name\}/);
  });
});

describe("two buttons never run together", () => {
  it("Share this run and Download diagnostics sit in one spaced group", () => {
    expect(codeOnly("app/improve/share-run.tsx")).toContain('className="share-run-buttons"');
    expect(cssOnly("app/improve/improve.css")).toMatch(/\.share-run-buttons\s*\{[^}]*gap:/);
  });
});
