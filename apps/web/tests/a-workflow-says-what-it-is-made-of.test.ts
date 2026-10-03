/**
 * Four things an operator could not see on 2026-09-24, reported from the console:
 * a step count with no step names, an Edit button landing on Create, step rows that named
 * nothing, and custom steps that could be defined and deleted but never changed.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { draftFromSpecStep, specFromDraft, type DraftStep } from "../lib/workflow-spec";

const ui = readFileSync(join(__dirname, "../app/delivery-workflows-ui.tsx"), "utf8");
/** The define/edit step form, moved out of the workflow screen into its own file. */
const form = readFileSync(join(__dirname, "../app/workflows/define-step-form.tsx"), "utf8");
/** What the builder knows about steps, for the save tests below. */
const CTX = {
  capabilities: [{ key: "notify", required_inputs: ["message"] }],
  customSteps: [{ step_key: "publish", inputs: [{ name: "media_artifact_id" }] }],
};
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
    expect(form).toContain("editing?: CustomStep | null;");
    expect(form).toContain('{editing ? "Save changes" : "Define"}');
  });

  it("repopulates the form from the step being edited", () => {
    expect(form).toContain('useState(editing?.label ?? "")');
    expect(form).toContain('useState(editing?.knowledge ?? "")');
    expect(form).toContain("useState<string[]>((editing?.inputs ?? []).map((i) => i.name))");
    expect(form).toContain("useState<string[]>(editing?.produces ?? [])");
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
    expect(ui).toContain("inputs?: Record<string, unknown>;");
  });
});

describe("a step can be given to an agent", () => {
  it("offers the agents on every work step", () => {
    // BOSS's own pipelines read as agents doing the work because a capability's captain becomes
    // the actor. A customer's step ignored it and the builder never offered it, so their
    // workflows had no agent in them anywhere.
    expect(ui).toContain("dw-draft-captain-");
    expect(ui).toContain("Agent for ${step.id}");
  });

  it("offers the list the publish-time check will actually accept", () => {
    // Taken from the capabilities payload rather than the agent directory: offering a role that
    // would then be refused at save is worse than offering none.
    expect(ui).toContain("stepRoles: string[];");
    expect(ui).toContain('setStepRoles((caps as { roles?: string[] }).roles ?? [])');
  });

  it("says what empty means rather than leaving it blank", () => {
    // REWRITTEN: empty no longer means "runs as the workflow". It means BOSS picks the agent
    // holding what the step needs, or — on a built-in step — whoever starts the run.
    expect(ui).toContain("whoever starts the run (built-in step)");
    expect(ui).toContain("— BOSS picks");
    expect(ui).toContain("a person (default)");
  });

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

  it("an agent step means something different by 'no agent'", () => {
    // On every other kind, empty means the anonymous workflow runner. On an agent step it means
    // "whichever agent fits this instruction", which BOSS works out from what each one holds —
    // so the same blank option cannot read the same way.
    expect(ui).toContain("whichever agent fits — BOSS picks");
  });

  it("an agent step's instruction can be asked for or pinned", () => {
    // Both are things operators say: the same workflow doing a different job each run, or a
    // workflow that always does the one thing.
    expect(ui).toContain("ask me when the run starts");
    expect(ui).toContain("always do this");
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

  it("says so when the agent on an agent step holds nothing", () => {
    // There is no one skill to check — the whole of what the agent holds is what it may use —
    // so the thing worth saying is when it holds NOTHING, because then the step cannot work.
    expect(ui).toContain("holds no skills, so it cannot do anything");
  });

  it("keeps the captain a workflow was saved with", () => {
    // REWRITTEN to load a step rather than read the loader's source.
    expect(draftFromSpecStep({ id: "s", kind: "capability", captain: "media_publisher",
                               binding: { custom_step: "publish" } }).captain)
      .toBe("media_publisher");
  });
});

describe("a step's inputs can be wired", () => {
  it("offers all three sources for every input", () => {
    // Every input was written as "ask the operator by name", so a workflow built here could not
    // feed one step into the next — the one thing a workflow is for.
    expect(ui).toContain("asked when the run starts");
    expect(ui).toContain("from an earlier step");
    expect(ui).toContain("a fixed value");
  });

  it("only offers steps that run BEFORE this one", () => {
    // A workflow reading from a step that has not run cannot resolve, and the runtime would
    // fail it mid-run rather than at save. So the choice is not offered.
    expect(ui).toContain("draft.slice(0, index)");
  });

  it("says so rather than going quiet when there is no earlier step", () => {
    expect(ui).toContain("from an earlier step (none yet)");
  });

  it("lets a fixed value pin a channel", () => {
    // The engine passes literals straight through, which is how a publish step chooses between
    // two connected YouTube channels.
    expect(ui).toContain('placeholder="e.g. youtube_2"');
    expect(ui).toContain("dw-wire-fixed-");
  });

  it("suggests the fields a step declares without limiting to them", () => {
    // `produces` is what a step DECLARES; a skill can return more, and a closed picker would
    // make the rest unreachable.
    expect(ui).toContain("dw-fields-${step.id}");
    expect(ui).toContain('placeholder="which field"');
  });

  it("opens one step's wiring at a time", () => {
    // A canvas with every step expanded is a form, and the thing being edited is the ORDER as
    // much as the wiring.
    expect(ui).toContain("wiringStep === step.id ? null : step.id");
  });
});
