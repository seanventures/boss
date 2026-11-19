/**
 * AN ANSWER'S SHORT PART AND ITS DETAILS (engine/boss_engine/services/reply_style.py).
 *
 * The agent answers an action request with one sentence and a button, and puts anything
 * supporting after a line that is exactly `Details:`. Everything below that line is folded
 * behind a "Details" toggle, so the person reads the sentence and the button, not the files and
 * ids behind them. The marker is the contract with the engine's prompt: change both together.
 *
 * Tolerant of how a model writes the line (`Details:`, `**Details**`, `### Details`), and only a
 * line of its own counts: "Details: see below" mid-paragraph is prose.
 */
export const DETAILS_MARKER = "Details:";

const MARKER_LINE = /^[ \t]*(?:#{1,6}[ \t]*)?(?:\*\*|__)?Details:?(?:\*\*|__)?:?[ \t]*$/im;

export function splitDetails(text: string): { lead: string; details: string } {
  const source = text ?? "";
  const hit = MARKER_LINE.exec(source);
  if (!hit) return { lead: source, details: "" };
  const lead = source.slice(0, hit.index).trimEnd();
  const details = source.slice(hit.index + hit[0].length).trim();
  // Nothing above the marker is not a fold (no answer would be left to read), and nothing
  // below it is a stray line: either way the text is shown whole, without the marker.
  if (!lead || !details) return { lead: lead || details || source, details: "" };
  return { lead, details };
}
