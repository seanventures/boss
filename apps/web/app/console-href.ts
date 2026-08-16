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
  },
): boolean {
  if (!href) return false;
  // Only same-document query links. "http…" (previews, PRs) and real routes navigate.
  if (!href.startsWith("?") && !href.startsWith("/?")) return false;
  if (opts.target === "_blank") return false;
  if (opts.pathname !== opts.currentPathname) return false;
  const area = new URLSearchParams(href.slice(href.indexOf("?") + 1)).get("area");
  return Boolean(area && opts.knownAreas.includes(area));
}
