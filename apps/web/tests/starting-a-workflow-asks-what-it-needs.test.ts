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

// The Runs composer lives in its own module since the Workflows area became Runs + Library.
const UI = codeOnly("app/workflows/new-project-composer.tsx");
const AREA = codeOnly("app/delivery-workflows-ui.tsx");
const LIBRARY = codeOnly("app/workflows/library.tsx");

const DRAFT: NewProjectDraft = {
  title: "", directive: "", audience: "", criteria: [], workflowKey: "",
  repository: "", links: [], agents: [], inputs: {},
} as NewProjectDraft;

describe("a workflow that declares inputs opens the form", () => {
  it("Start one does not fire a sample when the workflow needs values", () => {
    // Every Start (a Library card, the editor) opens the composer on that workflow.
    expect(AREA).toContain("setStartWith(key)");
    expect(AREA).toContain("onStart={(key, example) => openComposerOn(key, example)}");
    expect(AREA).toContain("onStart={(key) => { setEditing(null); setAreaUrl(EDITOR_PARAMS, \"replace\"); openComposerOn(key); }}");
  });

  it("and the composer opens on the step that collects them, already on that workflow", () => {
    // They pressed a button on that workflow's card; asking "which path?" again is asking a
    // question they have answered. A catalogue template's one required answer is its brief,
    // collected on the first step, so it opens there.
    // Its start form is the first step (rendered: tests/the-start-form-is-the-workflows-own).
    // One screen: the form is on it from the start, already on that workflow.
    expect(UI).toContain("const firstPath = startWith ?? defaultPath(\"new\", installedKeys);");
    expect(UI).toContain("{ ...EMPTY_DRAFT, workflowKey: firstPath, inputs: firstPath ? defaultsFor(firstPath)");
  });

  it("no Start fires a sample: an example is shown before it runs", () => {
    // REWRITTEN (2026-10-02). This asserted that a workflow declaring nothing "still starts on
    // a sample". That is the behaviour the boss-1 map found spending a build in the operator's
    // repository on a random demo idea — for six templates a new-product brief that did not
    // fit them. Every catalogue template now declares its brief, Start opens the form, and the
    // template's own example is the explicit "Try an example", filled in to read first.
    for (const src of [UI, AREA, LIBRARY]) expect(src).not.toContain("sample: true");
    // "Try example" on a standard's card hands its example to the same composer.
    expect(LIBRARY).toContain("p.onStart(w.workflow_key, example)");
    expect(LIBRARY).toContain('data-testid={`wf-card-example-${w.workflow_key}`}');
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

  it("and the button is never silently dead", () => {
    // start() did `if (!text) return` when there was no brief — a press that does nothing at
    // all. It now returns only when the same `blocked` that disables the button is set, and a
    // workflow that is not a product brief sends its own fields, not a made-up run title (the
    // video example's `title` is the YouTube title).
    expect(UI).toContain("if (busy || (blocked && !(adding && proposal?.proposal))) return;");
    expect(UI).toContain("if (!o.brief) return { ...typed, ...where };");
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
