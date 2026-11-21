/**
 * A PIECE OF WORK'S ONE NEXT STEP: the Work row's primary button and the work drawer's top card
 * read it from here, so they never disagree (park-bench-map 2026-11-19: the row said "Approve
 * deploy", the drawer it opened said "Ready to merge · Merge change #3").
 *
 *   merge    "Merge change #3" / "Merge PR #3" (hint "deploys to staging after" when Ship follows)
 *   decide   the engine's action label ("Approve deploy", "Approve plan", "Decide")
 *
 * null when the work does not wait on a person.
 *
 * TWO HONEST MERGES (the Work panel wireframe, park-bench-map run #2):
 *   - a later attempt still fixing the same change (merge_pr.fixing): "BOSS is fixing 4 findings
 *     on change #3 — merge now or wait"; Merge stays offered, but as the SECOND choice
 *     (`secondary`), and the first is watching the fix (`wait`).
 *   - no must-have fully met (merge_pr.acceptance.met == 0): never "Ready to merge"; it says
 *     what is partial ("0 of 4 must-haves met, 4 partly").
 *   - opened with its checks failing (merge_pr.checks_failing, the record its merge card shows):
 *     "Change #3 is open · 2 checks not met: security, tests". Merge stays the button and goes to the
 *     merge card, which takes the person's reason (a forced merge, never a silent one).
 *
 * Precedence: failed > fixing > checks failing > no must-have met > follow-ups.
 */
import { changeHref, changeRef, isLocalChangeUrl } from "./repo-links";
import type { ChecksFailingWork, WorkItem } from "./project-experience";

/** "3 checks not met: review, consistency, must-haves" — the one phrase the Work row, the
 *  panel's header and its Review & tests node all use for a change opened with failing checks. */
export function checksNotMet(c: ChecksFailingWork): string {
  const n = c.gates.length;
  return `${n} check${n === 1 ? "" : "s"} not met: ${c.words}`;
}

export type NextStep = {
  kind: "merge" | "decide";
  /** The button's words. */
  label: string;
  /** The card's heading ("Ready to merge · 5 follow-ups go to the More tests pass next"). */
  words: string;
  /** What happens after, in a few words ("deploys to staging after"), or "". */
  hint: string;
  /** Where the action is taken: BOSS's decision, else the change itself. */
  href: string;
  /** The href leaves the console (a GitHub pull request). */
  external: boolean;
  /** Merge is offered second: BOSS is still fixing this change (merge_pr.fixing). */
  secondary?: boolean;
  /** The first choice while BOSS fixes it: watch the fix (its live log). */
  wait?: { label: string; href: string } | null;
};

type Acceptance = { met: number; partial: number; missing: number; total: number };

/** "0 of 4 must-haves met, 4 partly" — what is partial, in words. */
export function mustHaveWords(acc: Acceptance): string {
  const bits = [`${acc.met} of ${acc.total} must-have${acc.total === 1 ? "" : "s"} met`];
  if (acc.partial) bits.push(`${acc.partial} partly`);
  if (acc.missing) bits.push(`${acc.missing} not met`);
  return bits.join(", ");
}

type MergePr = NonNullable<WorkItem["links"]["merge_pr"]>;

/** The pull request a merge waits on: the engine's merge_pr; an older payload's "Merge" row its
 *  pr. A pull request already merged is not that wait (merged_pr). */
export function mergeWaitOf(w: Pick<WorkItem, "links" | "action_label">): MergePr | null {
  const l = w.links;
  return l.merge_pr ?? (w.action_label === "Merge" && l.pr && !l.merged_pr
    ? { ...l.pr, follow_up_of: null, failed: "" } : null);
}

/** Ship follows the merge by itself (a Ship station still to come, nobody asked first). */
function shipsAfter(w: Pick<WorkItem, "stages">): boolean {
  return w.stages.some((st) => st.key === "ship" && (st.state === "todo" || st.state === "now"));
}

export function workNextStep(w: Pick<WorkItem, "state" | "links" | "action_label" | "stages" | "why">): NextStep | null {
  if (w.state !== "needs_you") return null;
  const l = w.links;
  const mp = mergeWaitOf(w);
  if (mp) {
    const local = isLocalChangeUrl(mp.external_url);
    const ref = changeRef(mp.external_url, mp.number, true);
    const fix = mp.fixing ?? null;
    const acc = mp.acceptance ?? null;
    const noneMet = !!acc && acc.total > 0 && acc.met === 0;
    const Ref = `${ref[0].toUpperCase()}${ref.slice(1)}`;
    const failing = mp.checks_failing ?? null;
    const words = mp.failed ? `Merge failed: ${mp.failed}`
      : fix ? `BOSS is fixing ${fix.findings ? `${fix.findings} finding${fix.findings === 1 ? "" : "s"}` : "it"} on ${ref} — merge now or wait`
      : failing ? `${Ref} is open · ${checksNotMet(failing)}`
      : noneMet ? `${Ref} is open · ${mustHaveWords(acc!)}`
      : mp.follow_up_of ? `Follow-up fix after #${mp.follow_up_of} · ready to merge`
      : mp.followups?.words ? `Ready to merge · ${mp.followups.words}` : "Ready to merge";
    const hint = shipsAfter(w) ? "deploys to staging after" : "";
    const wait = fix ? { label: "Watch the fix", href: l.log || l.build || "" } : null;
    const extra = fix ? { secondary: true, wait } : {};
    if (l.decision) {
      return { kind: "merge", label: `Merge ${ref}`, words, hint, href: l.decision, external: false, ...extra };
    }
    return { kind: "merge", words, hint, href: changeHref(mp.external_url) ?? "", external: !local, ...extra,
             label: local ? `Open ${ref}` : `Merge ${changeRef(mp.external_url, mp.number)} ↗` };
  }
  const words = w.why?.who === "you" ? w.why.words : "Waiting on you";
  const label = w.action_label || "Decide";
  return { kind: "decide", label, words, hint: "", href: l.decision || l.workflow_run || "", external: false };
}
