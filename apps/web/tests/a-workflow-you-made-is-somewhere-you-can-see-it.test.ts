import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * The workflow BOSS just published existed everywhere except where you would look for it.
 *
 * Reported, with a screenshot of the builder open on "Family channel video, 4 steps":
 *
 *     "What is this, the workflow itself does not exist in Catalogue and how is it coming to
 *      Edit mode in the workflow, it is completely wrong."
 *
 * The Edit picker was right. `editable = installed.filter((i) => !i.is_catalogue)` is exactly
 * the operator's own workflows, which is what an editor should offer — the standards are
 * forked, never edited, so that "reset to the standard" keeps meaning something.
 *
 * The CATALOGUE was wrong. Its grid maps `catalogue`, which holds shipped entries only, and
 * the operator's own were reduced to the tail of a sentence: "· 1 of your own". So a workflow
 * that was real, published, editable and startable appeared on screen as the digit 1, and the
 * only way to reach it was a dropdown on another tab.
 *
 * Two lists that disagree about what exists is a product telling you it is broken.
 */
const UI = codeOnly("app/delivery-workflows-ui.tsx");

describe("a workflow you made appears where workflows are listed", () => {
  it("the operator's own workflows are DRAWN, not counted", () => {
    expect(UI).toContain('data-testid="dw-mine"');
    // As cards in a grid, the same as the shipped ones — because they are the same kind of
    // thing to the person looking at them.
    expect(UI).toContain("dw-mine-${item.workflow_key}");
    // And the number-only version is gone: it was the whole bug.
    expect(UI).not.toContain("${mine} of your own");
    expect(UI).not.toContain("mine > 0 ?");
  });

  it("each one says how big it is and which version is live", () => {
    const block = UI.slice(UI.indexOf('data-testid="dw-mine"'));
    const card = block.slice(0, block.indexOf("</section>"));
    expect(card).toContain("item.step_count");
    expect(card).toContain("item.active_version");
  });

  it("a definition with no published version cannot be started", () => {
    // A definition exists before its first version does. Offering "Start one" there hands the
    // operator an error instead of a run.
    const block = UI.slice(UI.indexOf('data-testid="dw-mine"'));
    expect(block.slice(0, 2400)).toContain("!item.active_version");
    expect(block.slice(0, 2400)).toContain("No version yet");
  });

  it("editing from the card lands in the same builder the picker uses", () => {
    // Both doors lead to one thing. The card passes a DEFINITION ID because that is what the
    // builder's "Editing" select is keyed by — passing a workflow_key would silently select
    // nothing and look like another dead button.
    expect(UI).toContain("onEditMine");
    expect(UI).toContain(
      'onEditMine={(definitionId) => { setBuilderTarget(definitionId); setTab("design"); }}');
  });

  it("the Edit picker still offers exactly the operator's own workflows", () => {
    // NOT the thing to "fix". A standard is forked, never edited in place.
    expect(UI).toContain("installed.filter((i) => !i.is_catalogue)");
  });
});
