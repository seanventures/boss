/**
 * A BUILD'S REVIEW COMMENTS, ONCE EACH (recipe-box walk, 2026-11-18). Review rounds re-raise
 * the same finding: the first build page read "Comments 11 open · 0 fixed" with
 * tests/recipes.test.js:43 listed three times in near-identical words. One finding is one row:
 * the same file and line (or no place at all) and nearly the same words fold into the first, and
 * the row knows every id it stands for, so Resolve or Dismiss settles all of them.
 */
import type { ReviewComment } from "./cockpit";

export type FoldedComment = ReviewComment & {
  /** Every comment id this row stands for, its own first. */
  ids: string[];
  /** How many repeats folded into it (0 = it was raised once). */
  repeats: number;
};

const words = (text: string): string[] =>
  text.toLowerCase().replace(/[`*_"'“”‘’.,:;()[\]{}!?]/g, " ").split(/\s+/).filter((w) => w.length > 2);

/** Nearly the same words: the smaller set mostly inside the larger (Dice over word sets). */
export function sameFinding(a: string, b: string): boolean {
  const x = new Set(words(a));
  const y = new Set(words(b));
  if (!x.size || !y.size) return a.trim().toLowerCase() === b.trim().toLowerCase();
  let shared = 0;
  for (const w of x) if (y.has(w)) shared += 1;
  return (2 * shared) / (x.size + y.size) >= 0.6;
}

const place = (c: ReviewComment): string => `${c.path ?? ""}:${c.line ?? ""}`;

/** One row per finding, in the order first raised. An open repeat keeps the row open. */
export function foldComments(items: ReviewComment[]): FoldedComment[] {
  const out: FoldedComment[] = [];
  for (const c of items) {
    const twin = out.find((r) => place(r) === place(c) && sameFinding(r.body, c.body));
    if (!twin) { out.push({ ...c, ids: [c.id], repeats: 0 }); continue; }
    twin.ids.push(c.id);
    twin.repeats += 1;
    if (c.status === "open" && twin.status !== "open") twin.status = "open";
  }
  return out;
}

/** "4 open · 1 fixed" over the folded rows (the engine's word counts every repeat). */
export function commentsWord(items: ReviewComment[], fallback = "None"): string {
  if (!items.length) return fallback;
  const rows = foldComments(items);
  const open = rows.filter((r) => r.status === "open").length;
  const fixed = rows.filter((r) => r.status === "addressed").length;
  return `${open} open · ${fixed} fixed`;
}

/** What "Let BOSS fix these" asks the follow-up build to do: every open finding, with its place. */
export function fixFindingsInstructions(items: ReviewComment[]): string {
  const open = foldComments(items).filter((r) => r.status === "open");
  if (!open.length) return "";
  const lines = open.map((r) => `- ${r.path ? `${r.path}${r.line ? `:${r.line}` : ""}: ` : ""}${r.body.trim()}`);
  return `Fix the open review findings on this build. Address each one in the code and its tests:\n${lines.join("\n")}`;
}
