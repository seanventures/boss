/**
 * THE NEXT.JS PASS-THROUGH (IDENTITY.md §4.1, D1, A9): the console holds no credential of its
 * own. Every call it makes to the API — from the browser through app/v1/[...path], or from a
 * server component, route handler or server action — carries the signed-in person's own
 * cookie, and nothing else that could name a principal.
 *
 *   - FORWARDED: the identity cookies (`__Host-boss_session`, `__Host-boss_preauth`,
 *     `__Host-boss_device`) and, on writes, `X-Boss-CSRF` plus the browser's `Origin` and
 *     `Sec-Fetch-Site`, which the API's CSRF check reads (§4.3).
 *   - STRIPPED: `Authorization` (one credential per request: the console sends a cookie, never
 *     a bearer), every other `X-Boss-*`, and every header a proxy might use to assert identity
 *     (`X-User-Id`, `X-Forwarded-User`, `Remote-User`, `Tailscale-User-*`, …). The API ignores
 *     them anyway; stripping them here is the second wall (A9).
 *   - GONE: `BOSS_API_KEY` and the HMAC console cookie. There is no shared key to attach.
 *
 * This module is isomorphic and imports nothing from next/headers; the server's request-scoped
 * reader is installed once by instrumentation.ts (installIdentityResolver), the same way
 * lib/api-write.ts learns the press.
 */

export const SESSION_COOKIE = "__Host-boss_session";
export const PREAUTH_COOKIE = "__Host-boss_preauth";
export const DEVICE_COOKIE = "__Host-boss_device";
export const IDENTITY_COOKIES: readonly string[] = [SESSION_COOKIE, PREAUTH_COOKIE, DEVICE_COOKIE];
/** Dev-only cookies the identity stub reads (lib/identity/stub). Forwarded only to the stub. */
export const STUB_COOKIES: readonly string[] = ["boss_stub_claim", "boss_stub_bare", "boss_stub_signup"];

export const CSRF_HEADER_LC = "x-boss-csrf";
/**
 * Tells the API this console is a network-trusted build (BOSS_CONSOLE_AUTH=network, goldeneye),
 * so S5's network bridge applies even where the API's own env cannot be changed. The API honours
 * it only from a trusted proxy. Sent by the console itself on every call when — and only when —
 * the console runs that way; any client-sent copy is stripped like every X-Boss-* header.
 */
export const CONSOLE_AUTH_HEADER_LC = "x-boss-console-auth";

export function consoleAuthMarker(): string | null {
  return typeof window === "undefined" && process.env.BOSS_CONSOLE_AUTH === "network" ? "network" : null;
}

const IDENTITY_HEADER = /^(authorization|proxy-authorization|cookie|x-user(-.*)?|x-forwarded-user|x-forwarded-email|x-forwarded-preferred-username|x-forwarded-groups|x-auth-request-.*|x-remote-user|remote-user|x-webauth-.*|tailscale-user-.*|cf-access-.*|x-ms-client-principal.*|x-goog-authenticated-user-.*|x-amzn-oidc-.*|x-principal.*|x-actor.*)$/i;
const HOP = new Set(["host", "connection", "content-length", "keep-alive", "transfer-encoding", "upgrade", "te", "trailer"]);

/**
 * Must this request header be dropped before the API sees it? `X-Boss-CSRF` is the one
 * `X-Boss-*` header that passes: it proves the write came from the console page.
 */
export function isStrippedHeader(name: string): boolean {
  const n = name.toLowerCase();
  if (n === CSRF_HEADER_LC) return false;
  if (n.startsWith("x-boss-")) return true;
  return HOP.has(n) || IDENTITY_HEADER.test(n);
}

/**
 * A PREVIEW IS A CAPABILITY URL, NOT A SESSION (IDENTITY.md §4.1). `/v1/previews/<id>/web/…`
 * (and the overlay's `/feedback`) serve someone else's app on the console's origin. The id is
 * the credential; nothing the person holds is ever sent there — no identity cookie, no CSRF
 * token, no network-trust marker — and nothing the previewed app answers may set a cookie on the
 * console's origin. The API sandboxes every answer (CSP `sandbox` without allow-same-origin).
 */
export const PREVIEW_CAPABILITY_PATH = /^\/v1\/previews\/[^/]+\/(?:web(?:\/.*)?|feedback\/?)$/;

export function isPreviewCapabilityPath(pathname: string): boolean {
  return PREVIEW_CAPABILITY_PATH.test(pathname);
}

/**
 * A request from a context that must not speak for the person: a sandboxed (opaque-origin)
 * document such as a preview — `Origin: null` — or a cross-site fetch that is not a navigation.
 * Top-level navigations (an IdP's redirect back, a SAML form POST) are not this: they keep their
 * cookies. A sandboxed preview's `fetch('/v1/identity/me')` is, and it gets no session.
 */
export function untrustedBrowserContext(headers: Headers): boolean {
  if ((headers.get("origin") ?? "").trim().toLowerCase() === "null") return true;
  const site = (headers.get("sec-fetch-site") ?? "").toLowerCase();
  const mode = (headers.get("sec-fetch-mode") ?? "").toLowerCase();
  return site === "cross-site" && mode !== "navigate";
}

/** Only the identity cookies, re-serialised. Any other cookie on the console's origin stays home. */
export function identityCookieHeader(cookieHeader: string | null | undefined, opts: { stub?: boolean } = {}): string {
  if (!cookieHeader) return "";
  const allowed = new Set([...IDENTITY_COOKIES, ...(opts.stub ? STUB_COOKIES : [])]);
  return cookieHeader.split(";").map((part) => part.trim()).filter((part) => {
    const eq = part.indexOf("=");
    return eq > 0 && allowed.has(part.slice(0, eq).trim());
  }).join("; ");
}

/** The cookies of a Cookie header as a record (identity and stub cookies only). */
export function parseIdentityCookies(cookieHeader: string | null | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  for (const part of identityCookieHeader(cookieHeader, { stub: true }).split(";")) {
    const eq = part.indexOf("=");
    if (eq > 0) out[part.slice(0, eq).trim()] = decodeURIComponent(part.slice(eq + 1).trim());
  }
  return out;
}

/** Does this browser carry a session at all? Routing only (middleware); the API decides. */
export function hasSessionCookie(cookieHeader: string | null | undefined): boolean {
  return Boolean(parseIdentityCookies(cookieHeader)[SESSION_COOKIE]);
}

/** What the server-side resolver knows about the request being served. */
export type ForwardedIdentity = {
  /** The identity cookies, already filtered. */
  cookie: string;
  /** The CSRF token for this session, when the caller is about to write. */
  csrf: string | null;
  /** The browser's origin (a same-origin write already passed middleware's check). */
  origin: string | null;
};

export type IdentityResolver = (write: boolean) => Promise<ForwardedIdentity | null>;

const KEY = "__bossIdentityResolver";

export function installIdentityResolver(resolver: IdentityResolver): void {
  (globalThis as Record<string, unknown>)[KEY] = resolver;
}

export async function resolveForwardedIdentity(write: boolean): Promise<ForwardedIdentity | null> {
  const resolver = (globalThis as Record<string, unknown>)[KEY] as IdentityResolver | undefined;
  if (!resolver) return null;
  try {
    return await resolver(write);
  } catch {
    return null; // outside a request (a background render): nobody to forward
  }
}

const WRITES = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/**
 * Set by the /v1 proxy on a call that must speak for NOBODY (a preview's capability URL, a
 * sandboxed or cross-site caller). The server-side fetch wrapper (server-fetch.ts) would
 * otherwise re-attach the person's cookie and CSRF from the incoming request. It can only take
 * identity away — a client-sent copy is stripped like every X-Boss-* header before the proxy
 * sets its own — and it never reaches the API.
 */
export const SPEAKS_FOR_NOBODY_HEADER = "x-boss-speaks-for-nobody";

/**
 * Headers for a server-side call to the API: the caller's own, minus anything that could name a
 * principal, plus the signed-in person's cookie (and CSRF on writes).
 */
export async function withForwardedIdentity(method: string, given: HeadersInit | undefined): Promise<Headers> {
  const out = new Headers();
  const asked = new Headers(given);
  asked.forEach((value, name) => {
    if (!isStrippedHeader(name)) out.set(name, value);
  });
  if (asked.has(SPEAKS_FOR_NOBODY_HEADER)) {
    out.delete(CSRF_HEADER_LC);
    return out;
  }
  const marker = consoleAuthMarker();
  if (marker) out.set(CONSOLE_AUTH_HEADER_LC, marker);
  const write = WRITES.has(method.toUpperCase());
  const who = await resolveForwardedIdentity(write);
  if (who?.cookie) out.set("cookie", who.cookie);
  if (write && who) {
    if (who.csrf && !out.has(CSRF_HEADER_LC)) out.set(CSRF_HEADER_LC, who.csrf);
    if (who.origin) {
      out.set("origin", who.origin);
      out.set("sec-fetch-site", "same-origin");
    }
  }
  return out;
}

/** A stable, non-reversible tag for "whose response is this", for the feed cache's key. */
export function sessionTag(headers: Headers): string {
  const cookie = parseIdentityCookies(headers.get("cookie"));
  const token = cookie[SESSION_COOKIE] ?? "";
  if (!token) return "anon";
  // FNV-1a: not a security boundary, only a cache partition; the token never leaves memory.
  let h = 0x811c9dc5;
  for (let i = 0; i < token.length; i += 1) {
    h ^= token.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(36);
}
