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
