/**
 * A TITLE SHORTENED AT A WORD, NEVER INSIDE ONE (KG round 6: run titles read "loan repaym…").
 *
 * `wordCut` keeps whole words up to `max` characters and ends with an ellipsis; the caller shows
 * `full` as the tooltip. A title the API already shortened mid-word ("…scan: loan repaym… · Oct
 * 25, 02:50", learning_writeback.run_name before round 6) is healed the same way: the broken
 * word goes, the ellipsis stays.
 */
const TRAILING = /[\s,;:·–—\-/]+$/;

/** Drop the partial word in front of each ellipsis a shortener left inside a word. */
export function healCut(text: string): string {
  return text.replace(/(\S)\s+[^\s…]+…/g, "$1…");
}

export function wordCut(text: string, max = 60): { text: string; full: string; cut: boolean } {
  const full = String(text ?? "").replace(/\s+/g, " ").trim();
  const healed = healCut(full);
  if (healed.length <= max) return { text: healed, full, cut: healed !== full };
  const room = healed.slice(0, max);
  const space = room.lastIndexOf(" ");
  // A space past 40% of the room keeps whole words; one long word is cut where it must be.
  const head = healed[max] === " " ? room : space > max * 0.4 ? room.slice(0, space) : room;
  return { text: `${head.replace(TRAILING, "")}…`, full, cut: true };
}

/** A run's name and the " · Oct 25, 02:50" the API adds (learning_writeback.run_title), apart:
 *  only the name is shortened, the time always shows. */
export function splitWhen(label: string): [string, string] {
  const m = /^(.*?)( · [A-Z][a-z]{2} \d{1,2}, \d{2}:\d{2}(?: [A-Z][A-Za-z+\-:0-9]*)?)$/.exec(label);
  return m ? [m[1], m[2]] : [label, ""];
}
