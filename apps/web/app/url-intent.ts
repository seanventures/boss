/**
 * What the URL is ASKING the console to do, as a pure function of the query string.
 *
 * The console navigates by mutating `window.location` and dispatching `popstate`, and every
 * panel reads the URL back. That works for parameters that name a PLACE — `?area=`, `?run=`,
 * `?agent=` all describe what you are looking at, survive a reload, and are meant to be re-read
 * on every navigation.
 *
 * `?new=1` is not a place. It is a one-shot COMMAND: "open the new-project form". Writing it and
 * leaving it in the URL meant every later navigation re-delivered it, because navigation copies
 * the existing query string. Clicking a project in the top strip set `?run=<id>` on a URL that
 * still said `new=1`, fired popstate, and the form reader ran again — opening the form and
 * clearing the selection, so the project you clicked vanished and you landed on an empty form
 * instead. A command that is never consumed is delivered forever.
 *
 * Extracted here so this is testable without a browser: the bug was a sequence of URLs, and a
 * sequence of URLs is a string problem. See url-intent.test.ts.
 */

/** Parameters that are a one-shot instruction rather than a description of where you are. */
export const ONE_SHOT_PARAMS = ["new"] as const;

export type NewProjectIntent = {
  /** Open the new-project form. */
  open: boolean;
  /** The query string to replace the current one with, having consumed the command. Identical
   *  to the input when there was nothing to consume, so a caller can skip the history write. */
  nextSearch: string;
};

/**
 * Read — and consume — the request to open the new-project form.
 *
 * Consuming is the whole point: the caller must write `nextSearch` back with replaceState so a
 * later navigation cannot re-deliver it.
 */
export function readNewProjectIntent(search: string): NewProjectIntent {
  const params = new URLSearchParams(search);
  const open = params.get("new") === "1";
  if (!open) return { open: false, nextSearch: search };
  for (const key of ONE_SHOT_PARAMS) params.delete(key);
  const rest = params.toString();
  return { open: true, nextSearch: rest ? `?${rest}` : "" };
}

/** The project being looked at, or null. A place, not a command — never consumed. */
export function readRunParam(search: string): string | null {
  const run = new URLSearchParams(search).get("run");
  return run && run.trim() ? run.trim() : null;
}

/**
 * The query string for navigating to a project: names the place, and drops any pending one-shot
 * command. Asking to SEE something is not asking to start something new, and a stale command
 * carried along by the copy would win over the click that was just made.
 */
export function searchForProject(search: string, runId: string | null): string {
  const params = new URLSearchParams(search);
  params.set("area", "code_tasks");
  if (runId) params.set("run", runId);
  else params.delete("run");
  for (const key of ONE_SHOT_PARAMS) params.delete(key);
  const rest = params.toString();
  return rest ? `?${rest}` : "";
}

/**
 * The query string for starting a new project: carries the command, and drops whatever you were
 * looking at. `?run=` and `?agent=` name a specific project, and the readers for those only ever
 * SET so as not to fight the back button — so a stale one would survive and re-open that
 * project's detail on top of the form.
 */
export function searchForNewProject(search: string): string {
  const params = new URLSearchParams(search);
  params.set("area", "code_tasks");
  params.set("new", "1");
  params.delete("run");
  params.delete("agent");
  return `?${params.toString()}`;
}


/** Parameters that identify WHO you are looking at this console as. Losing one drops the
 *  operator on the organisation picker mid-session, which reads as being logged out. */
export const IDENTITY_PARAMS = ["org", "org_id"] as const;

/**
 * A link to somewhere else in the console, keeping who you are.
 *
 * Every href built from scratch — `?area=code_tasks&run=…` — silently dropped `?org=`, and the
 * page gates on that: no org in the URL means the organisation picker. So the workflow's own
 * "open the project" and "activity log" links, and the redirect after creating a project,
 * threw the operator back to "Select your organization" and looked like a session expiring.
 *
 * Built from the CURRENT query string so identity survives, with the one-shot commands dropped
 * because a link to a place is not a request to start something.
 */
export function consoleHref(
  currentSearch: string, params: Record<string, string | null | undefined>,
): string {
  const next = new URLSearchParams();
  const now = new URLSearchParams(currentSearch);
  for (const key of IDENTITY_PARAMS) {
    const value = now.get(key);
    if (value) next.set(key, value);
  }
  for (const [key, value] of Object.entries(params)) {
    if (value) next.set(key, value);
  }
  const rest = next.toString();
  return rest ? `?${rest}` : "";
}
