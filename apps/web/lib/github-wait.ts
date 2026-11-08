/**
 * WAITING FOR GITHUB (BOSSAPP-922): a build that finished while GitHub was down ends
 * `waiting_for_github` — its commits are safe on this machine and a queued job pushes them and
 * opens the pull request the moment GitHub answers (engine/boss_engine/services/github_outage.py).
 *
 * THE ONE PLACE every surface (the build page, Project › Work, Agents at work, Needs you) turns
 * that state into words and a tone. It is never "failed" and never red: the work is done and
 * saved; only the push waits.
 */

export const GITHUB_WAIT_STATUS = "waiting_for_github";
/** The Shepherd's wait code for it (stuck_work.wait_reason). */
export const GITHUB_WAIT_CODE = "github";
export const GITHUB_WAIT_LABEL = "Waiting for GitHub";
export const GITHUB_WAIT_DETAIL = "your work is saved here and will push when GitHub is back";
/** The words, whole: "Waiting for GitHub · your work is saved here and will push when GitHub is back". */
export const GITHUB_WAIT_WORDS = `${GITHUB_WAIT_LABEL} · ${GITHUB_WAIT_DETAIL}`;
/** Its tone: calm (blue), the same as work BOSS is doing on its own. Never "red"/"failed". */
export const GITHUB_WAIT_TONE = "blue" as const;

/** A run status, a build state or a wait code that means "waiting for GitHub". */
export function isWaitingForGitHub(status: string | null | undefined): boolean {
  const s = String(status ?? "").trim().toLowerCase();
  return s === GITHUB_WAIT_STATUS || s === "github_wait" || s === GITHUB_WAIT_CODE;
}

const pad = (n: number) => String(n).padStart(2, "0");

/**
 * The words for a waiting build, with when BOSS tries the push next:
 *   {label: "Waiting for GitHub", detail: "your work is saved here …", next: "next try 14:32" | "next try in 3 min" | "trying now" | ""}
 * `next` is "" when the next try is not known. Under an hour away it is said in minutes; later
 * as the local clock time.
 */
export function waitingWords(nextAttemptAt: string | null | undefined, now: Date | number = Date.now()):
  { label: string; detail: string; next: string; words: string } {
  const at = nextAttemptAt ? Date.parse(nextAttemptAt) : NaN;
  const t = typeof now === "number" ? now : now.getTime();
  let next = "";
  if (Number.isFinite(at)) {
    const mins = Math.round((at - t) / 60_000);
    if (mins <= 0) next = "trying now";
    else if (mins < 60) next = `next try in ${mins} min`;
    else {
      const d = new Date(at);
      next = `next try ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    }
  }
  return { label: GITHUB_WAIT_LABEL, detail: GITHUB_WAIT_DETAIL, next,
           words: next ? `${GITHUB_WAIT_WORDS} · ${next}` : GITHUB_WAIT_WORDS };
}
