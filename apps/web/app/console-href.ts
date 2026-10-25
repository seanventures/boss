"use client";

/**
 * Build an in-console link WITHOUT losing the rest of the query string.
 *
 * `href="?area=meetings"` does not mean "change the area". It means "replace the entire query
 * string", which silently drops `org=` — and the console, finding no org, shows "Select your
 * organization". Pressing View meeting therefore logged you out of your own org mid-journey.
 *
 * Every in-console link must go through here so the next one written cannot reintroduce it.
 */
export function consoleHref(
  params: Record<string, string | undefined>,
  /** The current query string. Passed in on the server, where `window` does not exist —
   *  without it the FIRST render emits an org-less href and only hydration repairs it. */
  search?: string,
): string {
  const next = new URLSearchParams(
    search ?? (typeof window === "undefined" ? "" : window.location.search),
  );
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined) next.delete(key);
    else next.set(key, value);
  }
  // Switching area must not carry the previous area's selection with it.
  if (params.area && !("task" in params)) next.delete("task");
  if (params.area && !("run" in params)) next.delete("run");
  return `?${next.toString()}`;
}

/**
 * Should a click on this link be handled inside the console instead of navigating?
 *
 * Split out from the handler so the cases that matter can be pinned by tests rather than
 * discovered in a browser: a full navigation re-renders all fifty areas (~6.5MB) and takes
 * about ten seconds, and hijacking the WRONG link breaks opening a preview or a PR.
 */
export function isInConsoleLink(
  href: string | null,
  opts: {
    pathname: string;
    currentPathname: string;
    target?: string | null;
    knownAreas: readonly string[];
    /** The current query string: a link that names ANOTHER project is a page load. */
    currentSearch?: string;
    /** The link asked for a real load (`data-console-load`, the project menu). */
    load?: boolean;
  },
): boolean {
  if (!href) return false;
  // Only same-document query links. "http…" (previews, PRs) and real routes navigate.
  if (!href.startsWith("?") && !href.startsWith("/?")) return false;
  if (opts.target === "_blank") return false;
  if (opts.load) return false;
  if (opts.pathname !== opts.currentPathname) return false;
  const next = new URLSearchParams(href.slice(href.indexOf("?") + 1));
  // ANOTHER PROJECT IS NOT A REVEAL. Every area on screen holds this project's data; revealing
  // one under the new project's URL left the Graph on "Loading Graph…" until a background
  // refresh re-rendered every opened area 13-22 s later (tester round 3). A browser load renders
  // the asked-for area alone, for the new project, in about a second.
  if (opts.currentSearch !== undefined) {
    const cur = new URLSearchParams(opts.currentSearch);
    if (next.has("project_id") && next.get("project_id") !== cur.get("project_id")) return false;
  }
  const area = next.get("area");
  return Boolean(area && opts.knownAreas.includes(area));
}
