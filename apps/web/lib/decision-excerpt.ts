/**
 * A sentence-boundary-safe cut for decision-card excerpts (AML video polish [7]): never mid-word,
 * never mid-sentence. A cut mid-word on camera reads as broken; a cut after the last whole
 * sentence at or before `max` reads as deliberate.
 */

function wordCut(text: string, max: number): string {
  const slice = text.slice(0, max);
  const lastSpace = slice.lastIndexOf(" ");
  return lastSpace > 0 ? slice.slice(0, lastSpace) : slice;
}

function isSentenceEnd(text: string, i: number): boolean {
  const ch = text[i];
  if (ch !== "." && ch !== "!" && ch !== "?") return false;
  if (ch === ".") {
    // A decimal point inside a figure like "$272,918.64" is not a sentence boundary.
    const prev = text[i - 1];
    const next = text[i + 1];
    if (prev && /\d/.test(prev) && next && /\d/.test(next)) return false;
  }
  return true;
}

export function sentenceCut(text: string, max = 600): { text: string; full: string; cut: boolean } {
  if (text.length <= max) return { text, full: text, cut: false };
  const slice = text.slice(0, max);
  let boundary = -1;
  for (let i = slice.length - 1; i >= 0; i -= 1) {
    if (isSentenceEnd(text, i)) { boundary = i; break; }
  }
  const excerpt = boundary >= 0 ? slice.slice(0, boundary + 1) : wordCut(text, max);
  return { text: excerpt, full: text, cut: true };
}
