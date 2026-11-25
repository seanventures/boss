// @vitest-environment happy-dom
/**
 * The merge button says why it is held, beside the label, not only on the title.
 * A blank or whitespace reason says nothing. The visible reason is trimmed.
 * More than six words is a paragraph. Six or fewer is a span.
 * The title and the label stay.
 *
 * Breaks if the held reason stays title-only, a padded reason is shown untrimmed,
 * a blank reason still renders, a long reason is a span, or the button stops
 * calling mergeHeldSentence(cs.merge.held?.reason).
 */
import { join } from "node:path";
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { Decide, mergeHeldSentence } from "../app/changes/[projectId]/[number]/change-set-view";
import { parseChangeSet } from "../lib/repo-links";
import { codeOnly } from "./_helpers/source";
import { mountAs, stubFetch, tid, unmount } from "./_helpers/multirepo-fixtures";

const LONG = "the staging probes have been failing since last night";
const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SHORT = "probe down";
const LABEL = "Checks running…";
const OPEN_TITLE = "A person merges · BOSS runs the governed merge";

const SRC = codeOnly(join(__dirname, "../app/changes/[projectId]/[number]/change-set-view.tsx"));

afterEach(() => unmount());

async function show(held: { state: string; label: string; reason: string } | null): Promise<void> {
  unmount();
  const merge = held
    ? { how: "approve", decision_id: "dec_m1", held }
    : { how: "approve", decision_id: "dec_m1" };
  const cs = parseChangeSet({
    number: 7, state: "open", source: "github", base_branch: "main", merge,
    decision: { id: "dec_m1", status: "pending", question: "Approve the change" },
  });
  stubFetch(() => undefined);
  await mountAs(<Decide cs={cs} projectId="hd" number="7" reread={async () => cs} compact />);
}

function why(): HTMLElement | null {
  return tid("change-set-held-why");
}

describe("mergeHeldSentence", () => {
  it("is the trimmed reason, or nothing when the reason is blank", () => {
    expect(mergeHeldSentence(`  ${LONG}  `)).toBe(LONG);
    expect(mergeHeldSentence(SHORT)).toBe(SHORT);
    expect(mergeHeldSentence(SIX)).toBe(SIX);
    expect(mergeHeldSentence(SEVEN)).toBe(SEVEN);
    expect(mergeHeldSentence(null)).toBeNull();
    expect(mergeHeldSentence(undefined)).toBeNull();
    expect(mergeHeldSentence("")).toBeNull();
    expect(mergeHeldSentence(" \n\t ")).toBeNull();
  });
});

describe("the merge button says why it is held", () => {
  it("shows a trimmed reason beside the label and keeps the title", async () => {
    const padded = `  ${LONG}  `;
    await show({ state: "waiting", label: LABEL, reason: padded });
    const merge = tid("change-set-merge") as HTMLButtonElement;
    expect(merge).not.toBeNull();
    expect(merge.disabled).toBe(true);
    expect(merge.getAttribute("title")).toBe(padded);
    expect(merge.textContent).toContain(LABEL);
    expect(merge.textContent).not.toContain(LONG);
    expect(merge.querySelector("[data-testid='change-set-held-why']")).toBeNull();
    const sentence = why();
    expect(sentence).not.toBeNull();
    expect(sentence!.tagName).toBe("P");
    expect(sentence!.textContent).toBe(LONG);
    expect(sentence!.hasAttribute("data-user-text")).toBe(true);
  });

  it("shows six or fewer words as a span, and seven as a paragraph", async () => {
    await show({ state: "waiting", label: LABEL, reason: SIX });
    expect(why()?.tagName).toBe("SPAN");
    expect(why()?.textContent).toBe(SIX);
    expect(why()?.hasAttribute("data-user-text")).toBe(true);
    expect((tid("change-set-merge") as HTMLButtonElement).textContent).toContain(LABEL);
    expect(tid("change-set-merge")?.getAttribute("title")).toBe(SIX);

    await show({ state: "waiting", label: LABEL, reason: `  ${SHORT}  ` });
    expect(why()?.tagName).toBe("SPAN");
    expect(why()?.textContent).toBe(SHORT);
    expect(tid("change-set-merge")?.getAttribute("title")).toBe(`  ${SHORT}  `);

    await show({ state: "waiting", label: LABEL, reason: SEVEN });
    expect(why()?.tagName).toBe("P");
    expect(why()?.textContent).toBe(SEVEN);
    expect(why()?.hasAttribute("data-user-text")).toBe(true);
  });

  it("says nothing when the reason is blank and keeps the label", async () => {
    await show({ state: "waiting", label: LABEL, reason: "" });
    expect(why()).toBeNull();
    const empty = tid("change-set-merge") as HTMLButtonElement;
    expect(empty.textContent).toContain(LABEL);
    expect(empty.getAttribute("title")).toBe(OPEN_TITLE);
    expect(empty.textContent).not.toContain(OPEN_TITLE);

    await show({ state: "waiting", label: LABEL, reason: " \n\t " });
    expect(why()).toBeNull();
    const space = tid("change-set-merge") as HTMLButtonElement;
    expect(space.textContent).toContain(LABEL);
    expect(space.getAttribute("title")).toBe(" \n\t ");
    expect(document.body.textContent ?? "").not.toContain(LONG);
  });

  it("says Merge, with no held reason, when the merge is not held", async () => {
    await show(null);
    const merge = tid("change-set-merge") as HTMLButtonElement;
    expect(merge).not.toBeNull();
    expect(merge.disabled).toBe(false);
    expect(merge.textContent).toContain("Merge");
    expect(merge.textContent).not.toContain(LABEL);
    expect(merge.getAttribute("title")).toBe(OPEN_TITLE);
    expect(why()).toBeNull();
  });

  it("calls mergeHeldSentence with the held reason", () => {
    expect(SRC).toContain("mergeHeldSentence(cs.merge.held?.reason)");
  });
});
