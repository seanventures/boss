/**
 * "Tooltips, not paragraphs", as a check on what a screen actually renders.
 *
 * The operator: "lots of text — make it tooltip / info, no one reads details, keep it concise".
 * Walks every element's own visible text under a root and returns each run longer than the
 * budget. Text inside a tooltip (`[data-tip]`, `[role=tooltip]`, `[title]`), a text box, or
 * screen-reader-only text is not counted: that is where explanations belong. Neither is text the
 * operator wrote (`[data-user-text]`): a workflow's name or description is theirs, not copy.
 * Nor is the Improve BOSS consent wording (`[data-consent]`): it is quoted verbatim from
 * docs/product/RUN_SCORECARDS.md, because what a person agrees to must not be paraphrased.
 *
 * One walker for the whole Workflow Studio, used by tests/workflow-copy-budget.test.tsx and the
 * editor's own render tests, so every surface is held to the same rule.
 */

export const MAX_WORDS = 6;

const SKIP = "[data-tip], [role='tooltip'], [title], textarea, script, style, option, datalist, "
  + "[hidden], [aria-hidden='true'], .wf-sr, [data-user-text], [data-consent]";

/** Visible text runs (an element's own text) over the budget. */
export function overBudget(root: Element, budget = MAX_WORDS): string[] {
  const out: string[] = [];
  for (const el of [root, ...root.querySelectorAll("*")]) {
    if (el.closest(SKIP)) continue;
    const own = [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent ?? "").join("");
    const words = own.replace(/[·▸•|]/g, " ").trim().split(/\s+/).filter(Boolean);
    if (words.length > budget) out.push(own.trim());
  }
  return out;
}

/** Any paragraph inside the editor. There should be none. */
export function paragraphs(root: ParentNode): string[] {
  return [...root.querySelectorAll(".wf-editor p")].map((p) => p.textContent ?? "");
}
