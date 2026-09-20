import { describe, expect, it } from "vitest";

import {
  missingRequired, suppliedInputs, workflowInputFields,
} from "../lib/workflow-inputs";

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
