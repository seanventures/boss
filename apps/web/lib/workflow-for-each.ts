/**
 * A For each on a run: how far through its list it is.
 *
 * The engine keeps `count` (how many items the list had) and `done` (how many have finished) on
 * a waiting `for_each` step's output (engine/boss_engine/services/workflows.py
 * `_advance_for_each`), so a run can say "3 of 10 done" without counting step runs itself.
 */
type RunStepLike = { kind?: string | null; step_kind?: string | null; status?: string | null;
                     output?: unknown };

/** "3 of 10 done" for a For each that is going through its list, or null for anything else. */
export function forEachProgress(step: RunStepLike): string | null {
  if ((step.kind ?? step.step_kind) !== "for_each") return null;
  const out = step.output && typeof step.output === "object" ? (step.output as Record<string, unknown>) : {};
  const count = Number(out.count);
  const done = Number(out.done ?? 0);
  if (!Number.isFinite(count) || count <= 0 || !Number.isFinite(done)) return null;
  return `${Math.min(done, count)} of ${count} done`;
}
