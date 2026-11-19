/**
 * A PIECE OF WORK'S ONE NEXT STEP: the Work row's primary button and the work drawer's top card
 * read it from here, so they never disagree (park-bench-map 2026-11-19: the row said "Approve
 * deploy", the drawer it opened said "Ready to merge · Merge change #3").
 *
 *   merge    "Merge change #3" / "Merge PR #3" (hint "deploys to staging after" when Ship follows)
 *   decide   the engine's action label ("Approve deploy", "Approve plan", "Decide")
 *
 * null when the work does not wait on a person.
 */
import { changeHref, changeRef, isLocalChangeUrl } from "./repo-links";
import type { WorkItem } from "./project-experience";

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
};

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
    const words = mp.failed ? `Merge failed: ${mp.failed}`
      : mp.follow_up_of ? `Follow-up fix after #${mp.follow_up_of} · ready to merge`
      : mp.followups?.words ? `Ready to merge · ${mp.followups.words}` : "Ready to merge";
    const hint = shipsAfter(w) ? "deploys to staging after" : "";
    if (l.decision) {
      return { kind: "merge", label: `Merge ${changeRef(mp.external_url, mp.number, true)}`, words, hint, href: l.decision, external: false };
    }
    return { kind: "merge", words, hint, href: changeHref(mp.external_url) ?? "", external: !local,
             label: local ? `Open ${changeRef(mp.external_url, mp.number, true)}` : `Merge ${changeRef(mp.external_url, mp.number)} ↗` };
  }
  const words = w.why?.who === "you" ? w.why.words : "Waiting on you";
  const label = w.action_label || "Decide";
  return { kind: "decide", label, words, hint: "", href: l.decision || l.workflow_run || "", external: false };
}
