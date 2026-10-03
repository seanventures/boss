import { describe, expect, it } from "vitest";

import { groupLibrary } from "../app/workflows/library";
import type { WorkflowSummary } from "../lib/workflow-editor-types";

/**
 * The workflow BOSS just published existed everywhere except where you would look for it.
 *
 *     "What is this, the workflow itself does not exist in Catalogue and how is it coming to
 *      Edit mode in the workflow, it is completely wrong."
 *
 * The Catalogue grid mapped shipped entries only and reduced the operator's own to "· 1 of
 * your own", so a workflow that was real, published and startable appeared as the digit 1.
 * Then it moved to a Yours tab, a third list. REWRITTEN for Workflow Studio: one Library, one
 * card design, yours first. Two lists that disagree about what exists is a product telling you
 * it is broken; there is now one list.
 */
const wf = (over: Partial<WorkflowSummary>): WorkflowSummary => ({
  definition_id: `def_${over.workflow_key}`, workflow_key: "k", name: "K", is_catalogue: false,
  version: 1, step_count: 1, steps: [], run_count: 0, ...over,
});

describe("a workflow you made appears where workflows are listed", () => {
  const items = [
    wf({ workflow_key: "spd", name: "Standard Product Delivery", is_catalogue: true }),
    wf({ workflow_key: "family_video", name: "Family channel video" }),
    wf({ workflow_key: "backend", name: "Backend Service Setup", is_catalogue: true }),
    wf({ workflow_key: "draft_only", name: "Not published yet", version: null }),
  ];

  it("lists yours before the standards, in the same kind of card", () => {
    const { yours, standards } = groupLibrary(items);
    expect(yours.map((e) => e.item.workflow_key)).toEqual(["family_video", "draft_only"]);
    expect(standards.map((e) => e.item.workflow_key)).toEqual(["backend", "spd"]);
  });

  it("keeps a workflow with no published version in the list", () => {
    // A definition exists before its first version does; it is still yours to open.
    expect(groupLibrary(items).yours.some((e) => e.item.workflow_key === "draft_only")).toBe(true);
  });

  it("never lists one workflow twice", () => {
    const { yours, standards } = groupLibrary(items);
    const all = [...yours, ...standards].flatMap((e) => [e.item, ...e.copies]).map((w) => w.workflow_key);
    expect(new Set(all).size).toBe(all.length);
    expect(all.length).toBe(items.length);
  });
});
