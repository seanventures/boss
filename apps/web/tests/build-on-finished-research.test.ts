import { readFileSync } from "node:fs";

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
/** Where the "is there research to build on" rule lives now — once, for the button AND for the
 *  start endpoint's `from_run`. Its console copy offered the button on a cancelled run whose
 *  research had failed. engine/tests/test_a_runs_actions_come_from_its_state.py is its table. */
const ENGINE = readFileSync("../../engine/boss_engine/services/workflow_run_actions.py", "utf8");

describe("a finished run can be built on", () => {
  it("offers it, and says what it will and will not cost", () => {
    expect(UI).toContain('testId: "dw-build-on-this"');
    // The sentence moved onto the dot beside the button rather than a permanent line under it;
    // what matters is that the operator can still read it before pressing, not where it sits.
    expect(UI).toContain("run again, and it does not cost anything a second time");
    const at = UI.indexOf('testId: "dw-build-on-this"');
    expect(UI.slice(at, at + 900)).toContain("explain:");
  });

  it("only on a run whose research SUCCEEDED and left a dossier with findings", () => {
    // Offered on a run with no research, it would start an ordinary delivery run while telling
    // the operator they had skipped the expensive step. Offered on research that FAILED — a
    // pre-2026-10-02 step recorded `succeeded` with output `failed_continued` — it promised a
    // dossier that did not exist. The rule checks the step's own verdict and the dossier.
    expect(ENGINE).toContain('FAILED_OUTPUT_STATUSES = frozenset({"failed", "failed_continued"');
    expect(ENGINE).toContain('if status != "completed":');
    expect(ENGINE).toContain('if not facts.get("has_findings"):');
    // And the console no longer has a rule of its own.
    expect(UI).not.toContain("const researched");
  });

  it("and it looks at what the step PRODUCED, not what it is called", () => {
    // `kind` on a capability step is the literal string "capability" — "research" is the step's
    // id — and a customer can rename their steps; a discovery id in the output is the contract.
    const at = ENGINE.indexOf("def _discovery_of(");
    const block = ENGINE.slice(at, at + 600);
    expect(block).toContain('produced.get("discovery_id")');
    expect(block).toContain('awaited.get("kind") == "discovery"');
  });

  it("and not while the run is still going", () => {
    // REWRITTEN to the verdict form: absent on a run that has not finished — and the start
    // endpoint's `from_run` now refuses through that same verdict instead of only reading the
    // research rule.
    const at = ENGINE.indexOf("    if not finished:\n        out.append(_verdict(\"build_on_research\"");
    expect(at).toBeGreaterThan(-1);
    expect(ENGINE.slice(at, at + 200)).toContain("ABSENT");
  });

  it("names the source instead of claiming the work already happened", () => {
    // `adopt_*` from a client is stripped at the API, on purpose. The button sends a source id
    // and lets the server draw the conclusion.
    const at = UI.indexOf("onBuildOnThis={(run) =>");
    const block = UI.slice(at, at + 700);
    expect(block).toContain("from_run: run.id");
    expect(block).not.toContain("adopt_discovery_run_id");
  });

  it("starts Standard Product Delivery by key, not by the class research already occupies", () => {
    // `work_class: "project.new"` is shared. After Research-Led Discovery is applied as the
    // default — or when nothing is mapped yet and start resolves the class — the button would
    // either commission another discovery or 409, while promising a build.
    const at = UI.indexOf("onBuildOnThis={(run) =>");
    const block = UI.slice(at, at + 700);
    expect(block).toContain('workflow_key: "standard_product_delivery"');
  });

  it("and it carries the directive, not the old adoptions", () => {
    const at = UI.indexOf("onBuildOnThis={(run) =>");
    expect(UI.slice(at, at + 700)).toContain("inputForRerun(run.input)");
  });
});
