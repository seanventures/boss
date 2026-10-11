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
export const STUB_COOKIES: readonly string[] = ["boss_stub_claim", "boss_stub_bare", "boss_stub_signup", "boss_stub_shared"];

export const CSRF_HEADER_LC = "x-boss-csrf";
/**
 * Tells the API this console is a network-trusted build (BOSS_CONSOLE_AUTH=network, shared-server),
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
 * A PREVIEW IS A CAPABILITY URL, NOT A SESSION (IDENTITY.md §4.1), AND IT LIVES ON ITS OWN ORIGIN
 * (security finding #4). `/v1/previews/<id>/web/…` (and the overlay's `/feedback`) serve someone
 * else's app. They used to be proxied on the console's origin, where `/v1/previews/x/%77eb/` slipped
 * past this matcher: the sandbox CSP was not relayed and the app read the CSRF token from
 * /v1/identity/me. Now the console never serves one: app/v1/[...path] redirects every preview path
 * to the preview origin (previewOrigin below; apps/api/app/preview_origin.py serves it, stripped of
 * every cookie), and nothing — cookie, CSRF, network marker — is forwarded for it.
 *
 * Matching happens on the NORMALISED path (normalisePath): percent-decoded until stable, `\` read
 * as `/`, repeated slashes collapsed, dot segments removed, and case-insensitive — every spelling
 * a server might route as a preview is judged as one.
 */
export const PREVIEW_CAPABILITY_PATH = /^\/v1\/previews\/[^/]+\/(?:web(?:\/.*)?|feedback\/?)$/is;

/**
 * A request path as a server could come to read it. Decoded repeatedly (`%2577` → `%77` → `w`;
 * `%2F`/`%2f` → `/`), backslashes as slashes, `//` collapsed, `.`/`..` segments resolved (never
 * above the root). A malformed escape stops the decoding where it is; the result is still judged.
 */
export function normalisePath(pathname: string): string {
  let path = pathname.split("?")[0].split("#")[0];
  for (let i = 0; i < 8; i += 1) {
    let next: string;
    try {
      next = decodeURIComponent(path);
    } catch {
      next = path.replace(/%([0-9a-f]{2})/gi, (_, hex: string) => String.fromCharCode(parseInt(hex, 16)));
    }
    if (next === path) break;
    path = next;
  }
  path = path.replace(/\\/g, "/").replace(/[\t\n\r\0]/g, "");
  const out: string[] = [];
  for (const segment of path.split("/")) {
    if (segment === "" || segment === ".") continue;
    if (segment === "..") { out.pop(); continue; }
    out.push(segment);
  }
  const trailing = /\/(?:\.{1,2})?$/.test(path) && out.length > 0 ? "/" : "";
  return `/${out.join("/")}${trailing}`;
}

export function isPreviewCapabilityPath(pathname: string): boolean {
  return PREVIEW_CAPABILITY_PATH.test(pathname) || PREVIEW_CAPABILITY_PATH.test(normalisePath(pathname));
}

/** BOSS_PREVIEW_PORT, or null when previews are switched off ("0"/"off"). Default 3001. */
export function previewPort(): number | null {
  const raw = (process.env.BOSS_PREVIEW_PORT ?? "").trim().toLowerCase();
  if (["0", "off", "false", "no", "none"].includes(raw)) return null;
  const port = Number(raw || "3001");
  return Number.isInteger(port) && port > 0 && port < 65536 ? port : 3001;
}

/**
 * Where a browser that reached the console at `host` opens previews: BOSS_PREVIEW_ORIGIN, or the
 * console's own host on BOSS_PREVIEW_PORT (console http://localhost:3000 → previews
 * http://localhost:3001). Null when neither can be said. Mirrors preview_service.preview_origin_for.
 */
export function previewOrigin(host: string | null | undefined, protocol: string): string | null {
  const configured = (process.env.BOSS_PREVIEW_ORIGIN ?? "").trim();
  if (configured) {
    try {
      const url = new URL(configured);
      if ((url.protocol === "http:" || url.protocol === "https:") && !url.username && !url.password) return url.origin;
    } catch { /* fall through to the derived origin */ }
  }
  const port = previewPort();
  if (port === null) return null;
  const found = /^(\[[0-9a-f:.]+\]|[a-z0-9.-]+)(?::\d{1,5})?$/i.exec((host ?? "").trim());
  if (!found) return null;
  return `${protocol === "https" || protocol === "https:" ? "https" : "http"}://${found[1]}:${port}`;
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

/** The console's origin as the browser addressed it: BOSS_CONSOLE_PUBLIC_ORIGIN, else what a proxy
 *  forwarded, else the Host header. Null when none can be said (middleware.ts browserOrigin). */
export function consoleOriginOf(headers: Headers): string | null {
  const configured = (process.env.BOSS_CONSOLE_PUBLIC_ORIGIN ?? "").trim().replace(/\/+$/, "");
  if (configured) return configured;
  const host = (headers.get("x-forwarded-host") || headers.get("host") || "").split(",")[0].trim();
  if (!host || !/^(\[[0-9a-f:.]+\]|[a-z0-9.-]+)(?::\d{1,5})?$/i.test(host)) return null;
  const proto = (headers.get("x-forwarded-proto") || "http").split(",")[0].trim().toLowerCase();
  return `${proto === "https" ? "https" : "http"}://${host}`;
}

/**
 * THE ORIGIN A SERVER-SIDE WRITE CARRIES (round 2, R2-WEB 4). The API's CSRF check on a write wants
 * `Origin` = the console's origin plus the session's X-Boss-CSRF. A browser POST (a server action,
 * a proxied write) sends its Origin, and that is what is forwarded. A browser GET does not, yet a
 * GET to a route handler or a page can need a WRITING GET, which the console calls by its POST twin
 * (lib/writing-get.ts). Those left with no Origin and were refused `403 csrf_failed` for every
 * role (/api/kit, /api/mcp, /api/run-acts: "Can't reach BOSS API" on Skills & MCP).
 *
 * The GET carries the proof the API itself accepts for a writing GET: `Sec-Fetch-Site:
 * same-origin` (the console's own page fetched it), or a top-level navigation the person started
 * (`none` + `navigate`: a typed URL, a reload). Then the console speaks for its own origin. A
 * cross-site GET (a link on another site, an image tag) gets no origin, so its write is refused
 * exactly as before. tests/a-writing-get-carries-its-csrf.test.ts.
 *
 * A PAGE OPENED FROM A LINK STILL RENDERS (first-run, 2026-10-10: Approvals said "Decision API
 * unavailable · API returned 403" for the Owner while the browser's own call answered 200). A
 * page's server render READS through writing GETs' twins (the decision inbox, the kit, the
 * status), and the person opens pages from links: a PR on GitHub, the preview on its own port
 * (same-site), an email, or a console reached over plain http, where a browser sends no fetch
 * metadata at all. Each of those navigations left the render's twin with no Origin, so the page
 * drew "API unavailable" for every role. `pageRead` is that case and nothing wider: the incoming
 * request is a PAGE (middleware's path header, never /api or /v1) opened as a top-level document
 * (`Sec-Fetch-Mode: navigate`, or no metadata and an HTML/RSC Accept), and the call is a writing
 * GET's twin (a read that does its own upkeep), never an ordinary write. A cross-site image,
 * script or fetch is still refused; so is a route handler (/api/kit) reached by a navigation.
 * tests/a-page-opened-from-a-link-reads-its-data.test.ts.
 */
export function forwardedWriteOrigin(incoming: Headers, opts: { pageRead?: boolean } = {}): string | null {
  const origin = (incoming.get("origin") ?? "").trim();
  if (origin) return origin.toLowerCase() === "null" ? null : origin;
  const site = (incoming.get("sec-fetch-site") ?? "").trim().toLowerCase();
  const mode = (incoming.get("sec-fetch-mode") ?? "").trim().toLowerCase();
  if (site === "same-origin" || (site === "none" && mode === "navigate")) return consoleOriginOf(incoming);
  if (opts.pageRead && isDocumentNavigation(incoming)) return consoleOriginOf(incoming);
  return null;
}

/** A top-level document load (any site), or — where the browser sent no fetch metadata (plain
 *  http) — a request that asked for a page (HTML) or a client navigation's payload (RSC). */
export function isDocumentNavigation(incoming: Headers): boolean {
  const mode = (incoming.get("sec-fetch-mode") ?? "").trim().toLowerCase();
  if (mode) return mode === "navigate";
  if (incoming.get("sec-fetch-site") || incoming.get("sec-fetch-dest")) return false;
  const accept = (incoming.get("accept") ?? "").toLowerCase();
  return accept.includes("text/html") || accept.includes("text/x-component");
}

/** The middleware path header names a console PAGE (not the /api handlers, not the /v1 proxy). */
export function isPagePath(path: string | null | undefined): boolean {
  if (!path || !path.startsWith("/")) return false;
  return !/^\/(api|v1)(\/|$)/.test(path);
}

/** A call to a writing GET's POST twin (lib/writing-get.ts): `<path>/load`. */
export function isWritingGetTwin(path: string | null | undefined): boolean {
  return Boolean(path && /\/load\/?$/.test(path.split("?")[0]));
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

/**
 * A SESSION'S CSRF TOKEN, LEARNED FROM THE /me THE RENDER ALREADY MADE.
 *
 * A server-side write (or a POST read twin) needs the session's CSRF token. It used to be read
 * with a SECOND /v1/identity/me whenever the kept copy was over 60 s old, so a render past that
 * mark asked /me twice (perf: `overview duplicate_calls 1`). The token is an HMAC of the session
 * id and cannot change while the session lives, so the render's own /me — which every render
 * still makes, fresh (the-live-proof-cannot-be-borrowed.test.ts) — refreshes it here
 * (server-fetch.ts), and sessionCsrf() asks /me only when nothing in this process has seen it.
 * Never the browser; never an empty token (a cross-context /me answers "").
 */
const CSRF_STORE_KEY = "__bossCsrfBySession";
export const CSRF_KEEP_MS = 60_000;

function csrfStore(): Map<string, { token: string; at: number }> {
  const g = globalThis as Record<string, unknown>;
  return (g[CSRF_STORE_KEY] ??= new Map()) as Map<string, { token: string; at: number }>;
}

export function keepCsrf(session: string, token: unknown): void {
  if (!session || typeof token !== "string" || !token) return;
  const store = csrfStore();
  if (store.size > 500) store.clear();
  store.set(session, { token, at: Date.now() });
}

export function keptCsrf(session: string): string | null {
  const kept = session ? csrfStore().get(session) : undefined;
  return kept && Date.now() - kept.at < CSRF_KEEP_MS ? kept.token : null;
}

/** The session's CSRF token: kept (from this render's own /me), else one /me of its own. */
export async function sessionCsrf(session: string, apiUrl: string): Promise<string | null> {
  const kept = keptCsrf(session);
  if (kept || !session || !apiUrl) return kept;
  const me = await fetch(`${apiUrl}/v1/identity/me`, { cache: "no-store" });
  if (!me.ok) return null;
  const token = ((await me.json()) as { csrf_token?: unknown }).csrf_token;
  keepCsrf(session, token);
  return keptCsrf(session);
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
  /** The console's origin as the browser addressed it (consoleOriginOf), for every call: sent as
   *  X-Forwarded-Host/Proto, so the API answers about the browser's address — /v1/auth/options'
   *  host and claim origin — never about the `api:8000` a server-side fetch dials (round 3, W1).
   *  The API believes it only from a trusted proxy, as it does the /v1 proxy's. */
  consoleOrigin?: string | null;
  /** The client's address as the console's own proxies reported it (lib/client-hop.ts, the
   *  trusted-proxy rule: BOSS_CONSOLE_PROXY_HOPS), or null when none can be believed. Sent as the
   *  ONE X-Forwarded-For hop on every server-side call (B2): a server-side render otherwise reached
   *  the API from the console's own private address, whoever the browser was. Undefined: not
   *  known (a caller that does not resolve it), and whatever the caller set is left alone. */
  clientHop?: string | null;
};

/** X-Forwarded-Host/Proto for an origin, or null when it is not one. */
export function forwardedHostHeaders(origin: string | null | undefined): { host: string; proto: string } | null {
  if (!origin) return null;
  try {
    const url = new URL(origin);
    if ((url.protocol !== "http:" && url.protocol !== "https:") || url.username || url.password || !url.host) return null;
    return { host: url.host, proto: url.protocol.replace(":", "") };
  } catch {
    return null;
  }
}

/** `target` is the API path being called (server-fetch.ts passes it), so a resolver can tell a
 *  writing GET's twin from an ordinary write (forwardedWriteOrigin's pageRead). */
export type IdentityResolver = (write: boolean, target?: { path: string }) => Promise<ForwardedIdentity | null>;

const KEY = "__bossIdentityResolver";

export function installIdentityResolver(resolver: IdentityResolver): void {
  (globalThis as Record<string, unknown>)[KEY] = resolver;
}

export async function resolveForwardedIdentity(write: boolean, target?: { path: string }): Promise<ForwardedIdentity | null> {
  const resolver = (globalThis as Record<string, unknown>)[KEY] as IdentityResolver | undefined;
  if (!resolver) return null;
  try {
    return await resolver(write, target);
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
export async function withForwardedIdentity(method: string, given: HeadersInit | undefined, path?: string): Promise<Headers> {
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
  const who = await resolveForwardedIdentity(write, path ? { path } : undefined);
  if (who?.cookie) out.set("cookie", who.cookie);
  if (who && who.clientHop !== undefined) {
    // THE CLIENT'S ADDRESS, NOT ANYONE'S CLAIM: never a forwarded chain from the caller, only
    // the hop the trusted-proxy rule picked (or none).
    for (const h of ["x-forwarded-for", "x-real-ip", "forwarded"]) out.delete(h);
    if (who.clientHop) out.set("x-forwarded-for", who.clientHop);
  }
  const addressed = forwardedHostHeaders(who?.consoleOrigin);
  if (addressed && !out.has("x-forwarded-host")) {
    out.set("x-forwarded-host", addressed.host);
    out.set("x-forwarded-proto", addressed.proto);
  }
  if (write && who) {
    if (who.csrf && !out.has(CSRF_HEADER_LC)) out.set(CSRF_HEADER_LC, who.csrf);
    if (who.origin) {
      out.set("origin", who.origin);
      out.set("sec-fetch-site", "same-origin");
    }
  }
  return out;
}

/** A stable, non-reversible tag for "whose response is this", for the feed cache's key: the
 *  SHA-256 of the session token (B3: a 32-bit hash was the only partition between two people's
 *  in-flight reads; a collision must be infeasible, not merely unlikely). */
export async function sessionTag(headers: Headers): Promise<string> {
  const cookie = parseIdentityCookies(headers.get("cookie"));
  const token = cookie[SESSION_COOKIE] ?? "";
  if (!token) return "anon";
  return tokenTag(token);
}

/** SHA-256 hex of a credential, for keys and partitions; the credential never leaves memory. */
export async function tokenTag(token: string): Promise<string> {
  const digest = await globalThis.crypto.subtle.digest("SHA-256", new TextEncoder().encode(token));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}
