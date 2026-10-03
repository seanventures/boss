/**
 * Four things an operator could not see on 2026-09-24, reported from the console:
 * a step count with no step names, an Edit button landing on Create, step rows that named
 * nothing, and custom steps that could be defined and deleted but never changed.
 *
 * REWRITTEN for Workflow Studio: the builder rows and cards these read are gone. A card's
 * steps are the Library's mini-lane (tests/workflow-library.test.tsx); a step's agent, task,
 * inputs and approval are the inspector's (tests/inspector.test.tsx). Kept here: custom steps
 * can be changed, and a save keeps what a workflow was saved with.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { draftFromSpecStep, specFromDraft, type DraftStep } from "../lib/workflow-spec";

/** The Library's Steps view, where a custom step is defined, edited and removed. */
const library = readFileSync(join(__dirname, "../app/workflows/library.tsx"), "utf8");
/** The define/edit step form, moved out of the workflow screen into its own file. */
const form = readFileSync(join(__dirname, "../app/workflows/define-step-form.tsx"), "utf8");
/** What the builder knows about steps, for the save tests below. */
const CTX = {
  capabilities: [{ key: "notify", required_inputs: ["message"] }],
  customSteps: [{ step_key: "publish", inputs: [{ name: "media_artifact_id" }] }],
};
const proxy = readFileSync(join(__dirname, "../app/api/delivery-workflows/route.ts"), "utf8");

describe("a custom step can be changed", () => {
  it("offers Edit beside Remove", () => {
    // The Library's Steps view: a pencil and a bin on every row.
    expect(library).toContain("wf-step-edit-${c.step_key}");
    expect(library).toContain("wf-step-remove-${c.step_key}");
  });

  it("uses ONE form for defining and editing", () => {
    // An edit form that drifted from the define form would accept fields the other rejects,
    // and the operator would meet two sets of rules for the same object.
    expect(form).toContain("editing?: CustomStep | null;");
    expect(form).toContain('{editing ? "Save changes" : "Define"}');
  });

  it("repopulates the form from the step being edited", () => {
    expect(form).toContain('useState(editing?.label ?? "")');
    expect(form).toContain('useState(editing?.knowledge ?? "")');
    expect(form).toContain("(editing?.inputs ?? []).map((i) => i.name).join");
  });

  it("sends an edit as PATCH with the key in the path", () => {
    // The key is what workflow specs bind to, so it travels as an identifier and is never one
    // of the fields an edit can change.
    expect(proxy).toContain("export async function PATCH");
    expect(proxy).toContain("custom-steps/${encodeURIComponent(key)}");
  });

  it("forwards the API's own message rather than replacing it", () => {
    const patch = proxy.slice(proxy.indexOf("export async function PATCH"));
    // REWRITTEN (review of the readiness change): every proxy here now reads the API's refusal
    // through the one shared helper, which forwards a sentence verbatim and falls back to
    // "API returned <status>" only when there is none — the same rule, in one place.
    expect(patch.slice(0, 1400)).toContain("refusalFrom(r.status, parsed)");
    const helper = readFileSync("lib/start-refusal.ts", "utf8");
    expect(helper).toContain("if (flat) return { error: flat };");
    expect(helper).toContain("`API returned ${status}`");
    // AND IN WORDS: the helper reads the detail through apiErrorText's reader (lib/api-error.ts),
    // so a validation array is "field: message", never "[object Object]" (console-b stream).
    expect(readFileSync("lib/start-refusal.ts", "utf8")).toContain('import { detailText } from "./api-error";');
  });
});

describe("opening a workflow does not destroy how its steps feed each other", () => {
  // REWRITTEN AS BEHAVIOUR. These asserted lines of the builder's source; the save is now a
  // merge in lib/workflow-spec.ts, so they load a spec, round-trip it and compare.
  it("keeps the wiring a step was saved with", () => {
    // toDraft dropped binding.inputs and save rebuilt every one as {from_input: <name>}, so
    // viewing a workflow whose steps feed each other and pressing Save replaced
    // {from_step: "fetch-link", field: "media_artifact_id"} with a demand that the operator
    // type a media artifact id by hand. Nothing on screen had been changed.
    const wired = { media_artifact_id: { from_step: "fetch-link", field: "media_artifact_id" } };
    const loaded = { entry_step: "publish", steps: [{ id: "publish", kind: "capability",
      binding: { custom_step: "publish", inputs: wired }, approval: { mode: "auto" }, next: [] }] };
    const saved = specFromDraft(loaded.steps.map(draftFromSpecStep), { ...CTX, loaded });
    const step = (saved.steps as Record<string, unknown>[])[0];
    expect((step.binding as Record<string, unknown>).inputs).toEqual(wired);
  });

  it("only defaults the wiring for a step that has none", () => {
    // The from_input default is a starting point for a step just dropped on the canvas, not a
    // rule to impose on one that already has wiring. Both kinds: the customer's own steps and
    // the built-in capabilities.
    const fresh: DraftStep[] = [
      { id: "publish", kind: "capability", customStep: "publish" },
      { id: "tell", kind: "capability", capability: "notify" },
    ];
    const steps = specFromDraft(fresh, CTX).steps as { binding: { inputs: unknown } }[];
    expect(steps[0].binding.inputs)
      .toEqual({ media_artifact_id: { from_input: "media_artifact_id" } });
    expect(steps[1].binding.inputs).toEqual({ message: { from_input: "message" } });
  });

  it("carries it on the draft rather than re-deriving it", () => {
    expect(readFileSync(join(__dirname, "../lib/workflow-spec.ts"), "utf8"))
      .toContain("inputs?: Record<string, unknown>;");
  });
});

describe("a step can be given to an agent", () => {
  it("writes a captain into the spec only when one was chosen", () => {
    // An empty string would make every step look deliberately assigned to nobody.
    //
    // ON ALL FOUR KINDS: a built-in capability, a step the customer defined, an agent step that
    // carries an instruction instead of binding a skill, and a control block — which is where
    // an Approval gate lives, and the one step where naming who answers is the entire point.
    // That branch dropped it, so a gate could never keep `human`. REWRITTEN to run the save.
    const kinds: DraftStep[] = [
      { id: "a", kind: "capability", capability: "notify" },
      { id: "b", kind: "capability", customStep: "publish" },
      { id: "c", kind: "capability", agentWork: true },
      { id: "d", kind: "decision" },
    ];
    const unnamed = specFromDraft(kinds, CTX).steps as Record<string, unknown>[];
    expect(unnamed.filter((s) => "captain" in s)).toEqual([]);
    const named = specFromDraft(kinds.map((k) => ({ ...k, captain: "qa" })), CTX)
      .steps as Record<string, unknown>[];
    expect(named.map((s) => s.captain)).toEqual(["qa", "qa", "qa", "qa"]);
  });

  it("an agent step's instruction can be asked for or pinned", () => {
    // Both are things operators say: the same workflow doing a different job each run, or a
    // workflow that always does the one thing.
    // (The control is the inspector's Task section, `Ask at start | Fixed` — tests/inspector.test.tsx.)
    // And the instruction is what gets saved as the step's specification — asked for at the
    // start unless it was pinned. REWRITTEN to run the save rather than read its source.
    const saved = specFromDraft([{ id: "w", kind: "capability", agentWork: true }], CTX);
    expect((saved.steps as { binding: unknown }[])[0].binding).toEqual({
      agent_work: true, inputs: { instructions: { from_input: "instructions" } } });
    const pinned = specFromDraft(
      [{ id: "w", kind: "capability", agentWork: true, instructions: "tidy it" }], CTX);
    expect((pinned.steps as { binding: { inputs: Record<string, unknown> } }[])[0]
      .binding.inputs.instructions).toBe("tidy it");
  });

  it("keeps the captain a workflow was saved with", () => {
    // REWRITTEN to load a step rather than read the loader's source.
    expect(draftFromSpecStep({ id: "s", kind: "capability", captain: "media_publisher",
                               binding: { custom_step: "publish" } }).captain)
      .toBe("media_publisher");
  });
});

