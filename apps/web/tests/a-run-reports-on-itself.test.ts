import { describe, expect, it } from "vitest";

import { codeOnly, cssOnly } from "./_helpers/source";

/**
 * The end-of-run report is about the THING THAT WAS BUILT, not about what it cost.
 *
 * The first version led with four figures and a cost table, and the operator's verdict was
 * exact:
 *
 *   "I think the report is so poor, this is not I was expecting... What I want in the report
 *    is the details of what was done from research, meeting outcome, what feature was builded
 *    and tested, test pass results, even end user acceptance criteria, and how to use the
 *    app... then what else we could build on top of the existing implementation - what can be
 *    improved, P1, P2 and other guidance for next steps"
 *
 * Every one of those was already recorded — in the panel's PRD, in the code task's state, in
 * the preview the run started. Nothing was missing except the joining.
 */
const UI = codeOnly("app/run-report-ui.tsx");
const CSS = cssOnly("app/globals.css");
const HOST = codeOnly("app/delivery-workflows-ui.tsx");
const ROUTE = codeOnly("app/api/workflow-report/route.ts");

describe("the run report leads with the product", () => {
  it("names the thing that was built", () => {
    // Not the directive sliced at 110 characters, which reads as a broken sentence.
    expect(UI).toContain("product.name");
  });

  it("says what it is before it says what it cost", () => {
    expect(UI).toContain("run-report-summary");
    expect(UI.indexOf("run-report-summary")).toBeLessThan(UI.indexOf("Run facts"));
  });

  it("offers a link to try it", () => {
    expect(UI).toContain("run-report-preview");
    expect(UI).toContain("preview_status");
  });
});

describe("what the operator asked to see", () => {
  it.each([
    ["what shipped", "What shipped"],
    ["how to use it", "How to use it"],
    ["whether it works", "Does it work"],
    ["acceptance criteria", "Acceptance criteria"],
    ["next steps", "What to do next"],
  ])("has a section for %s", (_what, heading) => {
    expect(UI).toContain(heading);
  });

  it("walks the screens in order rather than printing a flowchart", () => {
    // The PRD's journey section is usually nothing but a mermaid diagram, so it cannot
    // answer "how do I use this". The screens can.
    expect(UI).toContain("product.screens");
    expect(UI).toContain("<ol className=\"rr-screens\">");
  });

  it("shows what the reviewer actually found, not just the verdict word", () => {
    // `adversarial_verdict` alone says "refuted" and hides that what was refuted was a
    // BLOCKER in the core merge feature.
    expect(UI).toContain("review_summary");
    expect(UI).toContain("What the reviewer found");
  });

  it("states what was written against what actually ran", () => {
    // The gap between them is the most useful line in the report: twelve test files written,
    // and the recorded verification was `npm install`.
    expect(UI).toContain("Recorded\n          verification:");
    expect(UI).toContain("test_files");
    expect(UI).toContain("commands_run");
  });

  it("never ticks an acceptance criterion nothing checked", () => {
    expect(UI).toContain("Not verified against the build");
    expect(UI).not.toContain("✓");
  });
});

describe("next steps are ranked and visible", () => {
  it("counts P1s on the collapsed summary, so they are visible without opening it", () => {
    expect(UI).toContain("run-report-urgent-count");
    expect(UI).toMatch(/urgent[\s\S]{0,200}nextSteps\.filter/);
  });

  it("offers to build the fixes rather than leaving a note to retype", () => {
    expect(UI).toContain("run-report-build");
    expect(UI).toContain("Build from this");
    expect(UI).toContain("<Hammer ");
  });

  it("carries the finished research forward instead of paying for it twice", () => {
    // `from_run` is resolved by the server, which adopts the dossier — never the panel.
    expect(UI).toContain("from_run: runId");
    expect(UI).toContain('action: "start"');
  });

  it("picks the fuller path when something is unproven", () => {
    // A P1 means unproven or unsafe, not untidy.
    expect(UI).toMatch(/P1"\)\s*\?\s*\n?\s*"major_feature"\s*:\s*"minor_feature"/);
  });

  it("builds the directive from the findings themselves", () => {
    expect(UI).toContain("`- [${n.priority}] ${n.item}`");
  });

  it("confirms with the run it started rather than a silent success", () => {
    expect(UI).toContain("run-report-built");
    expect(UI).toContain("builtRunId");
  });

  it("colours P1 and P2 differently", () => {
    expect(CSS).toContain('.rr-next li[data-priority="P1"] b');
    expect(CSS).toContain('.rr-next li[data-priority="P2"] b');
  });
});

describe("the panel's plumbing", () => {
  it("uses the eyebrow type the operator picked from Setup", () => {
    // "can we use the one from Setup -> Already on this install (I like this font)"
    expect(UI).toContain('className="eyebrow rr-title"');
    expect(CSS).toContain(".eyebrow {");
  });

  it("is collapsed until somebody asks for it", () => {
    // "Even the whole Run report should be collapsed... otherwise it will be lots of text
    //  which already got feedback that it is a lot of text". It is a lot of text — that is what a
    //  report is — so none of it opens uninvited.
    expect(UI).toContain('<details className="rr rr-root"');
    expect(UI).not.toMatch(/className="rr-section" open/);
    expect(UI).not.toContain("open={urgent.length > 0}");
  });

  it("says enough while shut to be worth opening", () => {
    // Name, headline numbers, and whether anything is urgent.
    expect(UI).toContain("rr-root-name");
    expect(UI).toContain("run-report-urgent-count");
    expect(UI).toContain("const money =");
  });

  it("keeps the heading a heading, inside the summary", () => {
    // <summary> takes flow content, so the landmark survives the collapse. Dropping to a
    // <span> for the type would trade it away for nothing.
    expect(UI).toMatch(/<h3 className="eyebrow rr-title">/);
    const summary = UI.slice(UI.indexOf("rr-root-summary"), UI.indexOf("rr-root-flag"));
    expect(summary).toContain("<h3");
  });

  it("offers the report as a PDF, built on request", () => {
    expect(UI).toContain("Download as PDF");
    // REWRITTEN: /api/artifacts/ was the bug — no handler serves it, so every PDF 404'd. The
    // API's /v1/artifacts/{id}/pdf, through the console's /v1 proxy, is what works.
    expect(UI).toContain("`/v1/artifacts/");
    expect(UI).not.toContain("/api/artifacts/");
    expect(UI).toContain('method: "POST"');
  });

  it("reads through the console's own proxy, not the browser", () => {
    expect(ROUTE).toContain("API_URL");
    expect(ROUTE).toContain("authHeaders");
  });

  it("never lets a failed report break the page it reports on", () => {
    expect(UI).toContain("if (!report) return null;");
    expect(UI).toContain("if (!report.ok)");
  });

  it("appears only once a step has finished", () => {
    expect(HOST).toContain("<RunReportPanel runId={run.id}");
    expect(HOST).toMatch(/run\.steps\.some\([\s\S]{0,120}?succeeded/);
  });

  it("reports time the machine took, without crying wolf at zero", () => {
    expect(UI).toContain("run-report-lost");
    expect(UI).toContain("lost > 0 ?");
  });
});
