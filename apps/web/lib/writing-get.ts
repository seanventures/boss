/**
 * A GET THAT WRITES IS CALLED BY ITS POST TWIN (identity int-b, apps/api/app/identity_guard.py
 * `writing_get`). Some reads seed defaults, reap, freeze or probe as they answer; the API declares
 * them `writes_on_get`, and a cookie-authenticated GET to one counts as a write for CSRF — it needs
 * `Sec-Fetch-Site: same-origin` or `X-Boss-CSRF`, which a server-side GET sends neither of, so it
 * is refused 403. Each answers POST at `<path>/load` with the same query: callers use that, through
 * the write path, so the person's CSRF token is attached.
 *
 * tests/no-console-caller-gets-a-writing-get.test.ts holds every caller to it, from the API's own
 * list (tests/fixtures/identity-api-routes.json `writes_on_get`).
 */
export const WRITING_GET_TWIN_SUFFIX = "/load";

/** `/v1/kit?project_id=x` → `/v1/kit/load?project_id=x` (an absolute URL works too). */
export function loadPath(path: string): string {
  const cut = path.search(/[?#]/);
  const base = cut < 0 ? path : path.slice(0, cut);
  return `${base.replace(/\/+$/, "")}${WRITING_GET_TWIN_SUFFIX}${cut < 0 ? "" : path.slice(cut)}`;
}
