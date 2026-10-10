/**
 * EVERY SERVER-SIDE CALL TO THE API CARRIES THE PERSON, NOT A SHARED KEY.
 *
 * Installed once per server process around the global fetch (outside lib/feed-cache.ts, so the
 * cache sees — and partitions by — the forwarded session). A request to the control plane gets
 * withForwardedIdentity()'s headers; anything else passes untouched. In the browser this does
 * nothing: the browser's own cookie reaches app/v1/[...path], which forwards it.
 *
 * While the identity stub is on (lib/identity/stub/enabled.ts), the identity paths are answered
 * in process by the stub, with the same cookies, so server components render against it too.
 */
import { identityStubEnabled, isIdentityPath } from "./stub/enabled";
import { keepCsrf, parseIdentityCookies, SESSION_COOKIE, withForwardedIdentity } from "./forward";
import { ME_PATH } from "./contract";
import { isNoReuse, noReuse } from "../feed-cache";

export function installIdentityFetch(apiUrl: string): void {
  if (typeof window !== "undefined" || !apiUrl) return;
  const g = globalThis as unknown as { __bossIdentityFetch?: true; __bossOriginalFetch?: typeof fetch; fetch: typeof fetch };
  if (g.__bossIdentityFetch) return;
  g.__bossIdentityFetch = true;
  const inner = g.fetch.bind(globalThis);
  // Kept for protocol-proxy.ts directFetch when the feed cache (installed first) is off.
  g.__bossOriginalFetch ??= inner;

  g.fetch = (async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    if (!url.startsWith(apiUrl)) return inner(input as RequestInfo, init);
    const method = (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
    const given = init?.headers ?? (input instanceof Request ? input.headers : undefined);
    const headers = await withForwardedIdentity(method, given);
    const path = new URL(url).pathname;
    if (identityStubEnabled() && isIdentityPath(path)) {
      return answerFromStub(method, url, headers, init?.body ?? null);
    }
    // The feed cache's no-reuse mark (a symbol on the init) is carried through explicitly:
    // the browser's own call is never collapsed or kept (lib/feed-cache.ts, B1).
    if (input instanceof Request) {
      return inner(new Request(input, { ...init, headers }), isNoReuse(init) ? noReuse({}) : undefined);
    }
    const next: RequestInit = { ...init, headers };
    const res = await inner(url, isNoReuse(init) ? noReuse(next) : next);
    // This render's own /me carries the session's CSRF token: kept, so a write later in the
    // render needs no second /me (forward.ts sessionCsrf).
    if (method === "GET" && path === ME_PATH && res.ok) {
      const session = parseIdentityCookies(headers.get("cookie"))[SESSION_COOKIE];
      if (session) {
        try { keepCsrf(session, ((await res.clone().json()) as { csrf_token?: unknown }).csrf_token); } catch { /* not JSON: nothing to keep */ }
      }
    }
    return res;
  }) as typeof fetch;
}

/** The stub's answer as a Response (JSON body, Set-Cookie headers kept). */
export async function answerFromStub(method: string, url: string, headers: Headers, rawBody: BodyInit | null): Promise<Response> {
  const { stubHandle } = await import("./stub/server");
  let body: unknown = null;
  if (typeof rawBody === "string" && rawBody) {
    try { body = JSON.parse(rawBody); } catch { body = null; }
  }
  const u = new URL(url);
  const out = stubHandle({ method, path: u.pathname, query: u.searchParams, body, cookies: parseIdentityCookies(headers.get("cookie")), headers });
  const res = new Headers({ "content-type": "application/json", "cache-control": "no-store" });
  for (const c of out.setCookies ?? []) res.append("set-cookie", c);
  return new Response(out.status === 204 ? null : JSON.stringify(out.body), { status: out.status, headers: res });
}
