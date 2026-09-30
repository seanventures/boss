/**
 * "Start one" on a workflow that needs a video link cannot start it blind.
 *
 * WHAT IT DID. The button sent `sample: true` — "surprise me" — which makes the API pick a
 * sample directive for the workflow's WORK CLASS. That exists for the catalogue's
 * software-building paths. On a workflow that asks for a video link and editing instructions it
 * is either refused outright (409, no sample for this class) or, worse, injects a software brief
 * and the run dies on its first step with no `video_url`, having told the operator nothing.
 *
 * Either way the operator never got to say what they wanted — which is the whole point of a
 * workflow whose middle step is "do what I asked".
 */
import { describe, expect, it } from "vitest";

import { blockingReason, type NewProjectDraft } from "../app/delivery-workflows-ui";
import { workflowInputFields } from "../lib/workflow-inputs";
import { codeOnly } from "./_helpers/source";

const UI = codeOnly("app/delivery-workflows-ui.tsx");

const DRAFT: NewProjectDraft = {
  title: "", directive: "", audience: "", criteria: [], workflowKey: "",
  repository: "", links: [], agents: [], inputs: {},
} as NewProjectDraft;

describe("a workflow that declares inputs opens the form", () => {
  it("Start one does not fire a sample when the workflow needs values", () => {
    expect(UI).toContain("if (workflowInputFields(chosen?.input_schema).length)");
    expect(UI).toContain("setStartWith(key)");
  });

  it("and the composer opens on the step that collects them, already on that workflow", () => {
    // They pressed a button on that workflow's card; asking "which path?" again is asking a
    // question they have answered.
    expect(UI).toContain("useState(startWith ? 1 : 0)");
    expect(UI).toContain("{ ...EMPTY_DRAFT, workflowKey: startWith }");
  });

  it("a workflow that declares nothing still starts on a sample", () => {
    // That button was built for the catalogue, and it works there.
    expect(UI).toContain("sample: true");
  });
});

describe("the brief is not demanded of a workflow that has its own inputs", () => {
  it("twenty characters of prose is required for a panel, and only for a panel", () => {
    expect(blockingReason(0, DRAFT)).toContain("Describe what you want built");
  });

  it("but not when the workflow declares what it needs", () => {
    // Its steps read the fields it declared; the real instruction is one of them. Demanding a
    // second brief nobody reads is a check meant for different work.
    expect(blockingReason(0, DRAFT, true)).toBe("");
  });

  it("and the run is still named, so the button is never silently dead", () => {
    // briefFrom() returns "" with no directive, and start() did `if (!text) return` — a press
    // that does nothing at all, which is the worst possible answer to a press.
    expect(UI).toContain('[chosen?.name ?? draft.workflowKey, supplied].filter(Boolean)');
  });
});

describe("a field that wants a paragraph gets one", () => {
  const schema = {
    required: ["video_url", "instructions"],
    properties: {
      video_url: { title: "Video link", format: "uri",
                   examples: ["https://drive.google.com/file/d/ID/view"] },
      instructions: {
        title: "What you want done",
        description: "Plain English.",
        examples: ["Keep 0:28 to 1:48, make it portrait for phones, and put the "
                   + "ending slide on the end."],
      },
      title: { title: "YouTube title" },
    },
  };

  it("infers prose from a long example, for authors who never thought about the form", () => {
    const fields = workflowInputFields(schema);
    const byName = Object.fromEntries(fields.map((f) => [f.name, f]));
    expect(byName.instructions.kind).toBe("multiline");
    // A link is long but is not prose — it is a url, and gets a url control.
    expect(byName.video_url.kind).toBe("url");
    expect(byName.title.kind).toBe("text");
  });

  it("and an author can say so explicitly", () => {
    const [field] = workflowInputFields(
      { properties: { notes: { format: "textarea" } }, required: [] });
    expect(field.kind).toBe("multiline");
  });

  it("the form draws a textarea for it", () => {
    expect(UI).toContain('field.kind === "multiline" ? (');
  });

  it("the help and the example still come from the workflow's own declaration", () => {
    const [, instructions] = workflowInputFields(schema);
    expect(instructions.label).toBe("What you want done");
    expect(instructions.help).toBe("Plain English.");
    expect(instructions.example).toContain("Keep 0:28 to 1:48");
  });
});
