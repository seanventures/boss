/**
 * What a workflow is CALLED when the console makes one from another.
 *
 * Forking and importing used to mangle the name: a fork appended "(yours)", a copy appended
 * "(copy)", and doing both produced "Standard Product Delivery (yours) (copy)". Three words of
 * bookkeeping in the one string an operator reads, and none of them said anything they could
 * not see from the row itself — the console already knows which workflows are catalogue
 * templates and which are the customer's own, and it knows what each was forked from.
 *
 * So: a copy is called what it is a copy OF. Only a genuine collision adds anything, and what
 * it adds is a number.
 */

/** Legacy decorations to strip when re-deriving a base name. */
const NOISE = /\s*\((?:yours|copy(?:\s+\d+)?|mine|custom)\)\s*/gi;

/** The name without the bookkeeping earlier versions added.
 *
 *  A trailing NUMBER is deliberately not stripped here: "Release 2024" is a name, and there is
 *  nothing in the string itself that distinguishes it from a collision marker. That judgement
 *  needs the project's other names, so it lives in `uniqueWorkflowName`. */
export function baseWorkflowName(name: string): string {
  return name.replace(NOISE, " ").replace(/\s+/g, " ").trim() || name.trim();
}

/**
 * A name for a new workflow derived from `sourceName`, unique among `taken`.
 *
 * The source's own name when it is free — which it is whenever the source is a catalogue
 * template the project has not customised yet, the overwhelmingly common case. Otherwise the
 * same name with a number, which is how a person would write it.
 */
export function uniqueWorkflowName(sourceName: string, taken: Iterable<string>): string {
  const used = new Set([...taken].map((n) => n.trim()));
  let base = baseWorkflowName(sourceName);
  // A trailing number is treated as a collision marker ONLY when the name without it is also
  // in the project — that is the evidence it was appended rather than part of the name. So
  // copying "Minor Feature 2" gives "Minor Feature 3", while copying "Release 2024" gives
  // "Release 2024 2" rather than mangling somebody's year into a counter.
  const numbered = base.match(/^(.*?)\s+\d+$/);
  if (numbered && used.has(numbered[1].trim())) base = numbered[1].trim();
  if (!used.has(base)) return base;
  for (let n = 2; n < 500; n += 1) {
    const candidate = `${base} ${n}`;
    if (!used.has(candidate)) return candidate;
  }
  return base;
}
