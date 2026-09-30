// Shared client helpers to reflect in-page selections (task, meeting) in the URL
// as real browser history entries — so the address bar updates AND the Back
// button returns to the previous view. Uses pushState (a new history entry);
// pair with a `popstate` listener in the component to sync state on back/forward.
export function pushUrlParam(key: string, value: string | null): void {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  if (value) url.searchParams.set(key, value);
  else url.searchParams.delete(key);
  window.history.pushState(null, "", url.toString());
}

export function readUrlParam(key: string): string | undefined {
  if (typeof window === "undefined") return undefined;
  return new URLSearchParams(window.location.search).get(key) ?? undefined;
}
