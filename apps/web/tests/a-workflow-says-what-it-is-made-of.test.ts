/**
 * Four things an operator could not see on 2026-09-24, reported from the console:
 * a step count with no step names, an Edit button landing on Create, step rows that named
 * nothing, and custom steps that could be defined and deleted but never changed.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const ui = readFileSync(join(__dirname, "../app/delivery-workflows-ui.tsx"), "utf8");
const proxy = readFileSync(join(__dirname, "../app/api/delivery-workflows/route.ts"), "utf8");

describe("the Edit button goes to the editor", () => {
  it("leaves create mode when a workflow was named to edit", () => {
    // Making Create the default tab left Edit on a card doing nothing visible: it set the
    // target and switched tabs, and the tab opened on Create regardless.
    expect(ui).toContain("if (target) setCreating(false);");
  });

  it("is keyed on the target alone so it does not fight the tabs", () => {
    // Re-running when `creating` changes would snap the operator back out of Create every
    // time they pressed it.
    const effect = ui.slice(ui.indexOf("if (target) setCreating(false);"));
    expect(effect.slice(0, 400)).toContain("}, [target]);");
  });
});

describe("a step row says what the step does", () => {
  it("names what it runs, not only what it was called", () => {
    // "Plan the edit · yours · auto" was the whole of what a row said. Every field below was
    // already on the client and none of it was drawn.
    expect(ui).toContain("dw-draft-runs");
    expect(ui).toContain("runs ${mine.risk_source}");
  });

  it("shows the inputs the step needs", () => {
    expect(ui).toContain("needs {mine.inputs.map");
  });

  it("says WHY a step is locked to manual", () => {
    // A disabled control with no reason reads as a broken control.
    expect(ui).toContain("always needs a person");
    expect(ui).toContain("dw-draft-forced");
  });

  it("surfaces the operator's own notes on the step", () => {
    expect(ui).toContain("carries your notes");
  });
});

describe("a card draws its steps", () => {
  it("lists them in order rather than counting them", () => {
    expect(ui).toContain("dw-card-steps");
    expect(ui).toContain("dw-card-step-label");
  });

  it("marks the step that will stop for a person", () => {
    // The other thing worth knowing before pressing Start.
    expect(ui).toContain("needs you");
  });
});

describe("a custom step can be changed", () => {
  it("offers Edit beside Remove", () => {
    expect(ui).toContain("dw-step-edit-");
    expect(ui).toContain("dw-step-actions");
  });

  it("uses ONE form for defining and editing", () => {
    // An edit form that drifted from the define form would accept fields the other rejects,
    // and the operator would meet two sets of rules for the same object.
    expect(ui).toContain("editing?: CustomStep | null;");
    expect(ui).toContain('{editing ? "Save changes" : "Define"}');
  });

  it("repopulates the form from the step being edited", () => {
    expect(ui).toContain('useState(editing?.label ?? "")');
    expect(ui).toContain('useState(editing?.knowledge ?? "")');
    expect(ui).toContain("(editing?.inputs ?? []).map((i) => i.name).join");
  });

  it("rebuilds the form when a different step is picked", () => {
    // Otherwise the first step's values stay in the fields.
    expect(ui).toContain('key={editing?.step_key ?? "new"}');
  });

  it("sends an edit as PATCH with the key in the path", () => {
    // The key is what workflow specs bind to, so it travels as an identifier and is never one
    // of the fields an edit can change.
    expect(ui).toContain('method: "PATCH"');
    expect(proxy).toContain("export async function PATCH");
    expect(proxy).toContain("custom-steps/${encodeURIComponent(key)}");
  });

  it("forwards the API's own message rather than replacing it", () => {
    const patch = proxy.slice(proxy.indexOf("export async function PATCH"));
    expect(patch.slice(0, 1400)).toContain("detail || `API returned ${r.status}`");
  });
});

describe("opening a workflow does not destroy how its steps feed each other", () => {
  it("keeps the wiring a step was saved with", () => {
    // toDraft dropped binding.inputs and save rebuilt every one as {from_input: <name>}, so
    // viewing a workflow whose steps feed each other and pressing Save replaced
    // {from_step: "fetch-link", field: "media_artifact_id"} with a demand that the operator
    // type a media artifact id by hand. Nothing on screen had been changed.
    expect(ui).toContain("inputs: (binding.inputs ?? undefined)");
  });

  it("only defaults the wiring for a step that has none", () => {
    // The from_input default is a starting point for a step just dropped on the canvas, not a
    // rule to impose on one that already has wiring.
    expect(ui).toContain("inputs: step.inputs ?? Object.fromEntries(");
    // Both branches: the customer's own steps and the built-in capabilities.
    const occurrences = ui.split("inputs: step.inputs ?? Object.fromEntries(").length - 1;
    expect(occurrences).toBe(2);
  });

  it("carries it on the draft rather than re-deriving it", () => {
    expect(ui).toContain("inputs?: Record<string, unknown>;");
  });
});
