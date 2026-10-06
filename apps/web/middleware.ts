import { NextRequest, NextResponse } from "next/server";

import {
  collectorMachinePost, collectorPathOf, localHttp, networkTrustedConsole, sameOriginMutation,
  secureConsoleTransport,
} from "./lib/console-session";
import { hasSessionCookie } from "./lib/identity/forward";
import { isPublicConsolePath, PATH_HEADER } from "./lib/identity/pages";
import { INTENT_HEADER, intentFromActionBody } from "./lib/api-write";
import { consoleParams } from "./app/legacy-areas";

/**
 * Lift a server action's press into a request header.
 *
 * A browser cannot add headers to a server action's POST, but SubmitButton puts the press's key
 * in the form (`__boss_intent`), and lib/call-action.ts sends it as the first argument of
 * app/press-relay.ts when code invokes an action. The action's API calls happen on the server, where only request
 * headers are reachable, so the key is moved here — the one place every action passes — and
 * lib/api-write.ts derives each call's Idempotency-Key from it. Next tees the body for
 * middleware, so reading a clone leaves the action's own copy intact. Large bodies (uploads) are
 * not read: they carry no press worth the cost, and behave as before.
 */
async function pressIntentHeaders(request: NextRequest): Promise<Headers | null> {
  if (request.method !== "POST" || !request.headers.get("next-action")) return null;
  const length = Number(request.headers.get("content-length") ?? "0");
  if (!length || length > 256_000) return null;
  try {
    const intent = intentFromActionBody(await request.clone().text());
    if (!intent) return null;
    const headers = new Headers(request.headers);
    headers.set(INTENT_HEADER, intent);
    return headers;
  } catch {
    return null;
  }
}

/**
 * ROUTING ONLY (IDENTITY.md §4.1): send a visitor with no session to the sign-in page, require
 * HTTPS when remote, refuse cross-site writes. It decides nothing about WHO may do WHAT — the API
 * does, on every call, from the session cookie this file only checks is present.
 */
export async function middleware(request: NextRequest) {
  const path = request.nextUrl.pathname;
  if (path.startsWith("/_next/static/") || path.startsWith("/_next/image/") ||
      path === "/favicon.ico" || path === "/robots.txt" || path === "/sitemap.xml" ||
      (process.env.NODE_ENV === "development" && path.startsWith("/_next/webpack-hmr"))) {
    return NextResponse.next();
  }
  // A preview PAGE opened in a new tab is a page, not an API call: a signed-out visitor goes to
  // the login screen and comes back, rather than reading a JSON 401.
  const previewPage = request.method === "GET" && /^\/v1\/previews\/[^/]+\/web(\/|$)/.test(path)
    && (request.headers.get("accept") ?? "").includes("text/html");
  const api = (path.startsWith("/api/") || path.startsWith("/v1/")) && !previewPage;
  // A collector route spelled any other way than plainly (`%69ngest`, `//ingest`): the matcher
  // could not exclude it, so Next is already teeing its body here. Refused, connection closed —
  // the plain spellings (with or without a trailing slash or a query) never reach middleware and
  // are checked in the /v1 route (re-check 4, item 1).
  if (collectorPathOf(request.url) && !PLAIN_COLLECTOR.test(path)) {
    return NextResponse.json({ detail: "collector_path_not_plain" },
      { status: 404, headers: { Connection: "close", "Cache-Control": "no-store" } });
  }
  // Another install's heartbeat to this collector: a machine POST, no session, no Origin
  // (lib/console-session.ts collectorMachinePost). Straight to the /v1 proxy, which adds no key.
  if (collectorMachinePost(request)) return NextResponse.next();
  if (!secureConsoleTransport(request) && !networkTrustedConsole()) {
    return NextResponse.json({ error: "The console requires HTTPS for remote access." },
      { status: 426, headers: { "Cache-Control": "no-store" } });
  }
  // The sign-in pages and the public sign-in API (rate-limited and CSRF-checked by the API).
  if (isPublicConsolePath(path)) return passThrough(request);
  // A NETWORK-TRUSTED INSTALL (shared-server, before and up to 30 days after its claim) has no
  // cookie to check: the API's network bridge decides who the visitor is (§4.12, S5). Once the
  // bridge ends, the API answers 401 and the console sends the visitor to sign in like anyone.
  if (networkTrustedConsole() || hasSessionCookie(request.headers.get("cookie"))) {
    return passThrough(request);
  }
  if (api) return NextResponse.json({ error: "unauthenticated" },
    { status: 401, headers: { "Cache-Control": "no-store" } });
  // Keep a local tunnel on its original browser host. Behind a TLS proxy,
  // redirect to the configured public origin: Next's internal listener is HTTP.
  const local = localHttp(request);
  const login = new URL("/login", local || !process.env.BOSS_CONSOLE_PUBLIC_ORIGIN ? request.url : process.env.BOSS_CONSOLE_PUBLIC_ORIGIN);
  login.searchParams.set("next", path + request.nextUrl.search);
  const response = local ? NextResponse.rewrite(login) : NextResponse.redirect(login);
  response.headers.set("Cache-Control", "no-store");
  return response;
}

/**
 * THE OLD IDENTITY AREA IS NOW /identity. `?area=enterprise` (and the `sso`/`identity` aliases
 * people type) answer with a redirect to the Identity home, keeping the org — a bookmark to the
 * providers screen lands on Sign-in & SSO.
 */
function identityRedirect(request: NextRequest): NextResponse | null {
  if (request.method !== "GET" || request.nextUrl.pathname !== "/") return null;
  const area = request.nextUrl.searchParams.get("area");
  if (area !== "enterprise" && area !== "identity" && area !== "sso") return null;
  const target = new URL(area === "sso" ? "/identity/sign-in" : "/identity", behindProxy(request) ? browserOrigin(request) : request.url);
  const org = request.nextUrl.searchParams.get("org");
  if (org) target.searchParams.set("org", org);
  return new NextResponse(null, { status: 307, headers: { Location: target.toString(), "Cache-Control": "no-store" } });
}

/**
 * A link to a retired area is rewritten by an HTTP redirect, before anything renders.
 *
 * page.tsx's redirect() runs after the shell has streamed, so the browser followed it as a
 * client navigation and dropped the URL's fragment: `?area=delivery#hardware-manager` (the old
 * "Where apps install" anchor) landed on Infra › Fleet with no `#hardware-manager` left to open
 * This computer (P-I-07). A real 307 keeps the fragment (RFC 7231 §7.1.2). Relative Location, so a
 * console behind a proxy redirects to its own public host. Document loads only: an RSC fetch or
 * a server action is the client's own navigation, which the shell rewrites itself.
 */
function legacyRedirect(request: NextRequest): NextResponse | null {
  if (request.method !== "GET" || request.nextUrl.pathname !== "/") return null;
  // A CLIENT NAVIGATION IS NOT REDIRECTED (re-check item 5). Next strips the `RSC` header before
  // middleware sees it, so that test never fired and router.push to an alias area got a 307
  // instead of a page. A client navigation is recognised by what survives: the `_rsc` query, the
  // router's state header, a fetch's `Sec-Fetch-Dest: empty`. It passes through, and page.tsx
  // renders the area the URL means (consoleParams) with a 200.
  if (isClientNavigation(request)) return null;
  const meant = consoleParams(request.nextUrl.searchParams);
  if (!meant.changed) return null;
  // DIRECT TO THIS SERVER (no proxy in front, a local tunnel, 127.0.0.1): REWRITE, don't redirect.
  // Next hands middleware an internal `localhost` host, so no Location built here can be the host
  // the browser typed — a browser on 127.0.0.1 was sent to localhost, another origin, signed out.
  // A rewrite renders the target with a 200 on the browser's own address (fragment kept: no
  // navigation happens), and the shell writes the meant URL into the address bar.
  if (!behindProxy(request)) {
    const target = new URL(request.url);
    target.search = `?${meant.params}`;
    return NextResponse.rewrite(target, { headers: { "Cache-Control": "no-store" } });
  }
  // AN ABSOLUTE LOCATION, ON THE BROWSER'S OWN ORIGIN. Next's middleware adapter parses a
  // redirect's Location with `new NextURL(location)` and NO base: a relative `/?area=budget`
  // throws "Invalid URL" and every alias answered 500 (shared-server, `?area=govern`). The origin is
  // the public one when the console sits behind a proxy, never the internal listener's.
  return new NextResponse(null, {
    status: 307,
    headers: { Location: `${browserOrigin(request)}/?${meant.params}`, "Cache-Control": "no-store" },
  });
}

/** The origin the browser used: the configured public one, else what the proxy forwarded, else
 *  the Host header (a local tunnel), else the request's own. */
/** A public origin is configured, or a proxy says which host the browser asked for. */
export function behindProxy(request: NextRequest): boolean {
  if (process.env.BOSS_CONSOLE_PUBLIC_ORIGIN) return true;
  const fwd = (request.headers.get("x-forwarded-host") || "").split(",")[0].trim();
  return Boolean(fwd) && !LOOPBACK.test(fwd);
}

/** Sec-Fetch-Dest and Accept survive Next's header stripping; RSC, the router state and `_rsc`
 *  do not (they are read too, for the server actions and tests that send them). */
export function isClientNavigation(request: NextRequest): boolean {
  const h = request.headers;
  return Boolean(request.nextUrl.searchParams.has("_rsc") || h.get("rsc") || h.get("next-action")
    || h.get("next-router-state-tree") || h.get("next-router-prefetch")
    || h.get("sec-fetch-dest") === "empty" || (h.get("accept") || "").includes("text/x-component"));
}

const LOOPBACK = /^(?:127(?:\.\d{1,3}){3}|localhost|\[::1\]|::1)(?::\d+)?$/i;

export function browserOrigin(request: NextRequest): string {
  const configured = (process.env.BOSS_CONSOLE_PUBLIC_ORIGIN || "").replace(/\/+$/, "");
  if (configured) return configured;
  const host = request.headers.get("x-forwarded-host") || request.headers.get("host");
  const proto = (request.headers.get("x-forwarded-proto") || request.nextUrl.protocol.replace(":", "") || "http")
    .split(",")[0].trim();
  return host ? `${proto}://${host.split(",")[0].trim()}` : request.nextUrl.origin;
}

/** A request that may reach the console: still never a cross-site write. */
async function passThrough(request: NextRequest) {
  if (!sameOriginMutation(request)) {
    return NextResponse.json({ error: "Cross-origin console mutation denied." },
      { status: 403, headers: { "Cache-Control": "no-store" } });
  }
  const moved = identityRedirect(request) ?? legacyRedirect(request);
  if (moved) return moved;
  // THE PAGE BEING OPENED, for the Identity and My-account layouts' guards (lib/identity/pages):
  // a layout is not told its pathname. Set here, never from the client (any client copy is
  // overwritten), and stripped before any call reaches the API (every X-Boss-* is).
  const forwarded = (await pressIntentHeaders(request)) ?? new Headers(request.headers);
  forwarded.set(PATH_HEADER, request.nextUrl.pathname);
  // THE CSP (IDENTITY.md §14.1 I, A10): pages only — never the /v1 proxy, which also serves the
  // previewed apps, nor the hand-written /api routes. Next reads the nonce from the request's
  // CSP header and stamps it on its own scripts; nothing else may run.
  const page = !request.nextUrl.pathname.startsWith("/v1/") && !request.nextUrl.pathname.startsWith("/api/");
  const policy = page ? contentSecurityPolicy(btoa(crypto.randomUUID())) : null;
  if (policy) forwarded.set("content-security-policy", policy);
  const response = NextResponse.next({ request: { headers: forwarded } });
  if (policy) response.headers.set("Content-Security-Policy", policy);
  response.headers.set("Cache-Control", "private, no-store");
  return response;
}

/**
 * Scripts: only Next's own, carrying this response's nonce ('strict-dynamic' lets them load their
 * chunks). Styles may be inline (the console sets custom properties and generated rules), fonts
 * come from Google Fonts, the page talks only to its own origin, and nothing may frame it. Dev
 * adds what Next's dev server needs (eval for fast refresh, ws for HMR).
 */
export function contentSecurityPolicy(nonce: string, dev = process.env.NODE_ENV === "development"): string {
  return [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${dev ? " 'unsafe-eval'" : ""}`,
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com data:",
    "img-src 'self' data: blob: https:",
    `connect-src 'self'${dev ? " ws: wss:" : ""}`,
    "frame-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
  ].join("; ");
}

/**
 * Every path but the Improve BOSS collector's three machine POSTs. Next TEES a request's body for
 * middleware — buffering it whatever the middleware does — so a 150 MB POST to the collector
 * cost this process ~200 MB before the route's own cap could refuse it (re-check item 3). Those
 * three paths need nothing from middleware: no session (another install has none), no CSRF
 * check (they carry no credential: the /v1 proxy attaches no key to them).
 */
export const config = { matcher: ["/((?!v1/telemetry/(?:ingest|delete|share)/*$).*)"] };

/** The spellings the matcher above excludes — the same pattern, anchored. */
const PLAIN_COLLECTOR = /^\/v1\/telemetry\/(?:ingest|delete|share)\/*$/;
