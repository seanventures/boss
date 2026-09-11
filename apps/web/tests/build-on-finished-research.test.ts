import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * "I want to take that PRD and go to implementation instead of repeating the research again."
 *
 * A finished Research-Led Discovery run left the operator with a dossier and no way to spend it:
 * the only button on a completed run was "Run this workflow again", which starts the research
 * over. The runtime could already adopt an existing dossier; nothing could ask it to.
 */
const UI = codeOnly("app/delivery-workflows-ui.tsx");

describe("a finished run can be built on", () => {
  it("offers it, and says what it will and will not cost", () => {
    expect(UI).toContain('data-testid="dw-build-on-this"');
    expect(UI).toContain("it does not run again");
  });

  it("only on a run that actually produced a dossier", () => {
    // Offered on a run with no research, it would start an ordinary delivery run while telling
    // the operator they had skipped the expensive step.
    expect(UI).toContain("const researched = run.steps.some(");
    expect(UI).toContain("&& researched ?");
  });

  it("and it looks at what the step PRODUCED, not what it is called", () => {
    // `kind` on a capability step is the literal string "capability" — "research" is the step's
    // id — so keying on the name rendered nothing on a run whose dossier was right there. And a
    // customer can rename their steps; a discovery id in the output is the contract.
    const at = UI.indexOf("const researched = run.steps.some(");
    const block = UI.slice(at, at + 500);
    expect(block).toContain('typeof produced.discovery_id === "string"');
    expect(block).toContain('awaited?.kind === "discovery"');
    expect(block).not.toContain('step.kind !== "research"');
  });

  it("and not while the run is still going", () => {
    const at = UI.indexOf('data-testid="dw-build-on-this"');
    expect(UI.slice(at - 400, at)).toContain("!ACTIVE_RUN_STATES.has(run.status)");
  });

  it("names the source instead of claiming the work already happened", () => {
    // `adopt_*` from a client is stripped at the API, on purpose. The button sends a source id
    // and lets the server draw the conclusion.
    const at = UI.indexOf("onBuildOnThis={(run) =>");
    const block = UI.slice(at, at + 400);
    expect(block).toContain("from_run: run.id");
    expect(block).not.toContain("adopt_discovery_run_id");
  });

  it("and it carries the directive, not the old adoptions", () => {
    const at = UI.indexOf("onBuildOnThis={(run) =>");
    expect(UI.slice(at, at + 400)).toContain("inputForRerun(run.input)");
  });
});
