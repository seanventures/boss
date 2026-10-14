/**
 * Pointing an arrow at another step, asked of the engine's validator before it is made.
 *
 * WHETHER THE SPEC CAN SAY IT is the model's (`rewireLink`, lib/workflow-graph.ts): which field
 * the arrow is, and the shapes the canvas keeps whole. WHETHER THE RESULT IS A VALID WORKFLOW is
 * the engine's (`normalise_workflow_spec` and the project checks behind POST check): a cycle
 * outside a Repeat, a step nothing reaches any more, an input read from a step that no longer
 * runs before it. The console does not keep a second copy of those rules. It checks the flow as
 * it would be, and refuses the link when that check finds a problem the flow on screen does not
 * already have — saying the validator's own words — so a link is never made that Save would
 * then refuse, and a refusal is never silent.
 *
 * Problems the draft already has (an empty Branch check, a step not set up yet) do not block a
 * link: only the ones the link would add. Warnings never do.
 */
import type { CheckResult, FlowLink, Problem, Spec } from "./workflow-editor-types";
import { rewireLink } from "./workflow-graph";

export type RewireCheck = (spec: Spec) => Promise<CheckResult>;
export type RewireAnswer = { ok: true; spec: Spec } | { ok: false; reason: string };

/** Said when the check could not be asked (offline, the API restarting): nothing is linked. */
export const CHECK_FAILED = "Could not check that link, so it was not made: try again";

const key = (p: Problem) => `${p.step_id ?? ""}|${p.field ?? ""}|${p.message}`;
const blocking = (p: Problem) => p.severity !== "warning";

/** The problems in `after` that `before` does not have (errors only). */
export function addedProblems(before: Problem[], after: Problem[]): Problem[] {
  const had = new Set(before.filter(blocking).map(key));
  return after.filter((p) => blocking(p) && !had.has(key(p)));
}

/** A validator message as the canvas says it: its first sentence, capitalised, no trailing stop. */
export function sayProblem(p: Problem): string {
  const text = p.message.trim().replace(/\.$/, "");
  return text ? text[0].toUpperCase() + text.slice(1) : "The workflow would not be valid";
}

/**
 * The spec with `link` pointed at `target`, or why not. `known` is what the validator already
 * said about `spec` (the editor's last check, when it was of this very spec); without it, `spec`
 * is checked alongside the result.
 */
export async function checkRewire(spec: Spec, link: FlowLink, target: string | null, check: RewireCheck,
                                  known?: Problem[] | null): Promise<RewireAnswer> {
  const out = rewireLink(spec, link, target);
  if ("refused" in out) return { ok: false, reason: out.refused };
  if (out.spec === spec) return { ok: true, spec };
  let before: Problem[];
  let after: Problem[];
  try {
    const [was, would] = await Promise.all([
      known ? Promise.resolve(known) : check(spec).then((r) => r.problems),
      check(out.spec).then((r) => r.problems),
    ]);
    before = was;
    after = would;
  } catch {
    return { ok: false, reason: CHECK_FAILED };
  }
  const added = addedProblems(before, after);
  return added.length ? { ok: false, reason: sayProblem(added[0]) } : { ok: true, spec: out.spec };
}
