/**
 * A starter brief, read rather than dumped.
 *
 * Compared against lovable.dev/templates, which shows 204 templates in a filterable grid —
 * screenshot, title, tagline, category — ours were sixteen cards on a horizontal conveyor
 * showing a drawing, a title and a one-word tag. The operator's word for it was "very normal",
 * and the comparison bears that out on three counts: you cannot scan a conveyor, you cannot
 * filter it, and a card that shows only a title tells you nothing about what you are about to
 * commit a build to.
 *
 * The odd part is that the missing information was already written. Every brief opens with
 * "Who it is for:" — a better tagline than anything a screenshot conveys — and closes with
 * "Done when:", which is the acceptance test. Both were in the payload and neither was on the
 * card. This reads them out.
 *
 * WHERE WE ARE NOT COPYING LOVABLE. Their card shows a screenshot of a finished thing. Ours
 * cannot, because the thing does not exist yet — BOSS builds it, to a specification, and the
 * specification is the product. So the detail view shows what it must do and how you will know
 * it worked, which is what an operator approving a build actually needs to read.
 */

/** The line that says who this is for — the tagline the card never showed. */
export function audienceOf(brief: string): string {
  const m = /^\s*Who it is for:\s*(.+)$/m.exec(brief ?? "");
  return m ? m[1].trim().replace(/\.$/, "") : "";
}

/** The bullet list under "What it must do" — the scope, as a list rather than a wall. */
export function mustDoOf(brief: string): string[] {
  const body = (brief ?? "").split(/^\s*What it must do:\s*$/m)[1];
  if (!body) return [];
  const out: string[] = [];
  for (const line of body.split("\n")) {
    const bullet = /^\s*-\s+(.*\S)\s*$/.exec(line);
    if (bullet) { out.push(bullet[1]); continue; }
    if (out.length && line.trim()) break;     // the list has ended
  }
  return out;
}

/** The acceptance test. The one sentence that decides whether the build worked. */
export function doneWhenOf(brief: string): string {
  const m = /^\s*Done when:\s*([\s\S]+?)(?:\n\s*\n|$)/m.exec(brief ?? "");
  return m ? m[1].trim().replace(/\s+/g, " ") : "";
}

/** Everything after "Done when", which is the design note the author left. */
export function noteOf(brief: string): string {
  const parts = (brief ?? "").split(/^\s*Done when:.*$/m);
  if (parts.length < 2) return "";
  const rest = parts[1].split("\n").slice(1).join(" ").trim();
  return rest.replace(/\s+/g, " ");
}

/** The first line — "Build an incident timeline builder." — without its imperative. */
export function summaryOf(brief: string): string {
  const first = (brief ?? "").split("\n")[0]?.trim() ?? "";
  return first.replace(/^Build (a|an|the)\s+/i, "").replace(/\.$/, "");
}

export type TemplateFacets = {
  audience: string;
  mustDo: string[];
  doneWhen: string;
  note: string;
};

/** Everything a card and its detail view need, parsed once. */
export function facetsOf(brief: string): TemplateFacets {
  return {
    audience: audienceOf(brief),
    mustDo: mustDoOf(brief),
    doneWhen: doneWhenOf(brief),
    note: noteOf(brief),
  };
}
