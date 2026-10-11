/**
 * BOSS AS AN OIDC PROVIDER ON THE PUBLIC ORIGIN (F6, stream S6).
 *
 * The issuer is `{public origin}/oidc/<org>`: apps and browsers reach the provider on the
 * console's origin, and this forwards those requests to the API. Each protocol endpoint carries
 * only what the protocol defines:
 *
 *   browser steps   authorize, resume, consent, end_session: the console session cookie
 *                   (`__Host-boss_session`, the workforce pool's sign-in) and nothing else, plus
 *                   Origin and Sec-Fetch-Site, which the consent POST's CSRF check reads
 *   app calls       token, revocation, userinfo: the client's Authorization (Basic or Bearer)
 *                   and the form; never a cookie
 *   public          discovery and the JWKS: nothing
 *
 * Set-Cookie is relayed only for the console session (a workforce step-up rotates it) and the
 * customer pools' own cookies, and only from browser steps. Bodies are capped; the API answers redirects
 * itself (relayed, never followed). The API's own page headers (CSP, X-Frame-Options) pass
 * through; middleware.ts adds no console CSP here.
 */
import { clientHop, proxyHops } from "./client-hop";
import { rawRequestUrl } from "./raw-request-url";

export const OIDC_BODY_CAP = 64 * 1024;
const OIDC = /^\/oidc\/[A-Za-z0-9_-]{1,100}\/(?:\.well-known\/openid-configuration|jwks|authorize|resume|consent|token|userinfo|revocation|end_session|account|hosted\/[a-z-]{2,40})$/;
const BROWSER_STEPS = new Set(["authorize", "resume", "consent", "end_session", "account", "hosted"]);
const APP_CALLS = new Set(["token", "revocation", "userinfo"]);
const SESSION_COOKIE = "__Host-boss_session";
/** A customer pool's own cookies (never a console credential; the API reads them only on /oidc/). */
const APP_COOKIES = ["__Host-boss_app_session", "__Host-boss_app_preauth"];

/** Is this exactly one of the provider's endpoints (no other spelling)? */
export function isOidcPath(pathname: string): boolean {
  return OIDC.test(pathname);
}

function endpointOf(pathname: string): string {
  const parts = pathname.split("/");
  return parts[3] ?? "";
}

/** Only the console session cookie and the app cookies, and only for the browser steps. */
export function sessionCookieOnly(cookieHeader: string | null): string {
  if (!cookieHeader) return "";
  const allowed = [SESSION_COOKIE, ...APP_COOKIES];
  return cookieHeader.split(";").map((p) => p.trim()).filter((p) => allowed.some((n) => p.startsWith(`${n}=`))).join("; ");
}

/** Set-Cookie values the API may set through this proxy: the console session (a workforce
 * step-up rotates it) and the app cookies. Anything else is dropped. */
export function relayedCookie(value: string): boolean {
  return [SESSION_COOKIE, ...APP_COOKIES].some((n) => value.startsWith(`${n}=`));
}

export function oidcRequestHeaders(pathname: string, incoming: Headers): Headers {
  const out = new Headers();
  for (const name of ["accept", "content-type", "user-agent"]) {
    const value = incoming.get(name);
    if (value) out.set(name, value);
  }
  const endpoint = endpointOf(pathname);
  if (BROWSER_STEPS.has(endpoint)) {
    const cookie = sessionCookieOnly(incoming.get("cookie"));
    if (cookie) out.set("cookie", cookie);
    for (const name of ["origin", "sec-fetch-site"]) {
      const value = incoming.get(name);
      if (value) out.set(name, value);
    }
  }
  if (APP_CALLS.has(endpoint)) {
    const auth = incoming.get("authorization");
    if (auth) out.set("authorization", auth);
  }
  const hop = clientHop(incoming.get("x-forwarded-for"), proxyHops());
  if (hop) out.set("x-forwarded-for", hop);
  return out;
}

const RELAYED = ["content-type", "cache-control", "pragma", "location", "www-authenticate", "retry-after",
  "content-security-policy", "x-frame-options", "x-content-type-options", "referrer-policy"];

function answer(status: number, error: string): Response {
  return Response.json({ error }, { status, headers: { "Cache-Control": "no-store" } });
}

export async function forwardOidc(req: Request, opts: { apiUrl: string; fetchImpl?: typeof fetch }): Promise<Response> {
  // Exactly as sent: NextRequest.url rewrites a 127.0.0.1 redirect_uri in the query (P1-1).
  const url = rawRequestUrl(req);
  if (!isOidcPath(url.pathname)) return answer(404, "not_found");
  if (!opts.apiUrl) return answer(503, "BOSS_API_URL is not set on the web server");
  const method = req.method.toUpperCase();
  let body: Uint8Array | undefined;
  if (method === "POST") {
    const declared = Number(req.headers.get("content-length") ?? "");
    if (Number.isFinite(declared) && declared > OIDC_BODY_CAP) return answer(413, "invalid_request");
    const buf = new Uint8Array(await req.arrayBuffer());
    if (buf.byteLength > OIDC_BODY_CAP) return answer(413, "invalid_request");
    body = buf;
  } else if (method !== "GET" && method !== "HEAD") {
    return answer(405, "invalid_request");
  }
  const g = globalThis as unknown as { __bossOriginalFetch?: typeof fetch };
  const doFetch = opts.fetchImpl ?? g.__bossOriginalFetch ?? globalThis.fetch;
  let upstream: Response;
  try {
    upstream = await doFetch(`${opts.apiUrl}${url.pathname}${url.search}`, {
      method, headers: oidcRequestHeaders(url.pathname, req.headers), body: body as BodyInit | undefined,
      cache: "no-store", redirect: "manual",
    });
  } catch {
    return answer(502, "temporarily_unavailable");
  }
  const headers = new Headers();
  for (const name of RELAYED) {
    const value = upstream.headers.get(name);
    if (value) headers.set(name, value);
  }
  if (BROWSER_STEPS.has(endpointOf(url.pathname))) {
    for (const c of upstream.headers.getSetCookie?.() ?? []) if (relayedCookie(c)) headers.append("set-cookie", c);
  }
  return new Response(upstream.body, { status: upstream.status, headers });
}
