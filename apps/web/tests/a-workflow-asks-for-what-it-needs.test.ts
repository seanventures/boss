import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { blockingReason } from "../app/delivery-workflows-ui";
import {
  inputsWithTheDirective, missingRequired, suppliedInputs, workflowInputFields,
} from "../lib/workflow-inputs";

const UI_SRC = readFileSync(
  new URL("../app/delivery-workflows-ui.tsx", import.meta.url), "utf8");

/** A draft with nothing in it, for the reasons `blockingReason` gives back. */
const EMPTY: Parameters<typeof blockingReason>[1] = {
  title: "", directive: "", audience: "", criteria: [], links: [],
  workflowKey: "", repository: "", agents: [], inputs: {},
};

/**
 * The start form renders FROM the workflow's declaration, so a workflow whose steps read
 * `from_input` — fetch this video, trim to this clip, publish to this channel — can be started
 * by someone who does not already know the field names.
 *
 * Two shapes exist in one database and neither is going away. Reading `.keys()` on the JSON
 * Schema one yields ["required"], which once told operators to supply an input called
 * "required".
 */

const JSON_SCHEMA = {
  required: ["video_url"],
  properties: {
    video_url: { title: "Video to publish", type: "string", format: "uri",
                 description: "Google Drive share link",
                 examples: ["https://drive.google.com/file/d/abc"] },
    start_clip: { title: "Start clip", type: "string" },
  },
};

const NAME_TO_TYPE = { video_url: "string", title: "string" };

describe("a workflow asks for what it needs", () => {
  it("reads the JSON Schema shape", () => {
    const fields = workflowInputFields(JSON_SCHEMA);
    expect(fields.map((f) => f.name).sort()).toEqual(["start_clip", "video_url"]);
  });

  it("never offers 'required' or 'properties' as fields to fill in", () => {
    const names = workflowInputFields(JSON_SCHEMA).map((f) => f.name);
    expect(names).not.toContain("required");
    expect(names).not.toContain("properties");
  });

  it("carries the label, help and example the author wrote", () => {
    const video = workflowInputFields(JSON_SCHEMA).find((f) => f.name === "video_url")!;
    expect(video.label).toBe("Video to publish");
    expect(video.help).toBe("Google Drive share link");
    expect(video.example).toBe("https://drive.google.com/file/d/abc");
    expect(video.kind).toBe("url");
  });

  it("knows which one the run will be refused without", () => {
    const fields = workflowInputFields(JSON_SCHEMA);
    expect(fields.find((f) => f.name === "video_url")!.required).toBe(true);
    expect(fields.find((f) => f.name === "start_clip")!.required).toBe(false);
  });

  it("asks for a required name even when it has no property block", () => {
    // Otherwise the run is refused for a field the form never showed.
    const fields = workflowInputFields({ required: ["channel"], properties: {} });
    expect(fields.map((f) => f.name)).toEqual(["channel"]);
    expect(fields[0].required).toBe(true);
  });

  it("reads the name-to-type shape and demands nothing from it", () => {
    const fields = workflowInputFields(NAME_TO_TYPE);
    expect(fields.map((f) => f.name).sort()).toEqual(["title", "video_url"]);
    expect(fields.every((f) => !f.required)).toBe(true);
  });

  it("makes a readable label out of a bare field name", () => {
    expect(workflowInputFields({ start_clip: "string" })[0].label).toBe("Start clip");
  });

  it("asks nothing of a workflow that declares nothing", () => {
    for (const empty of [null, undefined, {}, "", []]) {
      expect(workflowInputFields(empty)).toEqual([]);
    }
  });

  it("sends only what was filled in", () => {
    const fields = workflowInputFields(JSON_SCHEMA);
    const sent = suppliedInputs(fields, { video_url: "https://x", start_clip: "  " });
    expect(sent).toEqual({ video_url: "https://x" });   // a blank box is not an answer
  });

  it("names what is still missing, by label rather than field name", () => {
    const fields = workflowInputFields(JSON_SCHEMA);
    expect(missingRequired(fields, {})).toEqual(["Video to publish"]);
    expect(missingRequired(fields, { video_url: "https://x" })).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// The guard that never ran
// ---------------------------------------------------------------------------
// "Can you please follow the prompt in this file <link> and produce the outcome" went in the
// Building box, the operator pressed Start the project, and the RUNTIME refused it:
//
//     WorkflowValidationError: this workflow needs 1 input(s) it was not given: instructions
//
// The form had a check for exactly this. It was written with `??`, and `blockingReason` returns
// "" rather than null when nothing is blocking — so the right-hand side never evaluated and the
// check was dead code under a button that looked ready to press.
describe("a required input left blank", () => {
  const UI = readFileSync(new URL("../app/delivery-workflows-ui.tsx", import.meta.url), "utf8");

  it("is caught by the form rather than by the runtime", () => {
    expect(UI).toContain("|| (stillBlank.length ? `Needs ${stillBlank.join(\", \")}` : \"\")");
    expect(UI).not.toContain("?? (missingRequired(");
  });

  it("disables the start buttons, not just the message", () => {
    expect(UI).not.toContain("disabled={busy || !!blockingReason(0, draft)}");
    const starts = UI.match(/disabled=\{busy \|\| !!blocked\}/g) ?? [];
    expect(starts.length).toBeGreaterThanOrEqual(2);
  });

  it("blockingReason really does return an empty string, which is why ?? was wrong", () => {
    expect(blockingReason(0, { ...EMPTY, directive: "a".repeat(40) })).toBe("");
    expect(blockingReason(1, EMPTY)).toBe("");
  });
});

describe("the directive stands in for a single blank prose input", () => {
  const FIELDS = workflowInputFields({
    required: ["instructions"],
    properties: {
      instructions: { title: "What you want done", format: "textarea" },
      video_url: { title: "Video link" },
    },
  });

  it("is used when the operator wrote it once, in the first box", () => {
    const out = inputsWithTheDirective(FIELDS, {}, "Follow the prompt in this file and publish");
    expect(out.instructions).toBe("Follow the prompt in this file and publish");
  });

  it("does not overwrite what the operator actually typed in the field", () => {
    const out = inputsWithTheDirective(
      FIELDS, { instructions: "Keep 3:42 to 4:50" }, "something else entirely");
    expect(out.instructions).toBe("Keep 3:42 to 4:50");
  });

  it("declines when two required fields are blank, because it cannot tell which", () => {
    const two = workflowInputFields({
      required: ["instructions", "channel"],
      properties: { instructions: { format: "textarea" }, channel: {} },
    });
    expect(inputsWithTheDirective(two, {}, "a directive")).toEqual({});
  });

  it("declines for a field that does not take prose", () => {
    const numeric = workflowInputFields({
      required: ["seconds"], properties: { seconds: { type: "number" } },
    });
    expect(inputsWithTheDirective(numeric, {}, "a directive")).toEqual({});
  });

  it("sends nothing extra when there is no directive", () => {
    expect(inputsWithTheDirective(FIELDS, {}, "   ")).toEqual({});
  });

  it("is what gets sent, and what the review shows", () => {
    expect(UI_SRC).toContain("...inputsToSend,");
    expect(UI_SRC).toContain('data-testid="dw-review-inputs"');
  });
});
