/** A run, reduced to only what naming an app needs. */
export type AppRun = {
  title?: string | null;
  finalize_ctx?: Record<string, unknown> | null;
};

/**
 * What an app IS, pulled out of the run that built it.
 *
 * The benchmark cards read well because they lead with the product -- a kind, a name, a line
 * about what it does -- and keep run mechanics underneath. Every other app card led with
 * "RUNNING / 3 attempts" and a title that was still a whole PRD ("Web app — # PRD: BugBox —
 * Single-Project Issue Tracker **Status:** Ready for hu"), so the list read as pipeline
 * exhaust rather than as the things that had been built.
 */
export function appIdentity(run: AppRun): { name: string; kind?: string; blurb?: string } {
  const bench = ((run.finalize_ctx ?? {}) as Record<string, unknown>).qa_benchmark as
    { name?: string; tagline?: string; summary?: string } | undefined;
  if (bench?.name) {
    return { name: bench.name, kind: bench.tagline, blurb: bench.summary };
  }
  const raw = (run.title ?? "").replace(/^\[\d+\/\d+\]\s*/, "").replace(/\s+/g, " ").trim();
  // Strip the wrappers BOSS puts in front of a PRD title, then split name from subtitle.
  const cleaned = raw
    .replace(/^Web app\s*—\s*/i, "")
    .replace(/^#+\s*/, "")
    .replace(/^PRD:\s*/i, "")
    .replace(/^Follow-up:\s*deferred review findings\s*—\s*/i, "")
    .trim();
  const [head, ...rest] = cleaned.split(/\s+—\s+|:\s+/);
  const name = (head || cleaned || "Untitled app").slice(0, 46);
  // Everything after the name, minus the PRD boilerplate that follows it.
  const tail = rest.join(" — ").replace(/\*\*Status:\*\*.*$/i, "").trim();
  return { name, kind: tail ? tail.slice(0, 44) : undefined };
}
