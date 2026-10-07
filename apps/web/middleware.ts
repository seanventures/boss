import { NextRequest, NextResponse } from "next/server";

import {
  collectorMachinePost, consoleAuthConfig, localHttp, networkTrustedConsole, sameOriginMutation,
  secureConsoleTransport,
  sessionCookieName, validSession,
} from "./lib/console-session";
import { INTENT_HEADER, intentFromActionBody } from "./lib/api-write";
import { resolveLegacyArea } from "./app/legacy-areas";

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

/** Gate the entire console before any route can attach the server's BOSS_API_KEY. */
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
  // Another install's heartbeat to this collector: a machine POST, no session, no Origin
  // (lib/console-session.ts collectorMachinePost). Straight to the /v1 proxy, which adds no key.
  if (collectorMachinePost(request)) return NextResponse.next();
  if (networkTrustedConsole()) return passThrough(request);
  if (!secureConsoleTransport(request)) {
    return NextResponse.json({ error: "The console requires HTTPS for remote access." },
      { status: 426, headers: { "Cache-Control": "no-store" } });
  }
  if (path === "/login" || path === "/api/auth/login") {
    return NextResponse.next();
  }
  const config = consoleAuthConfig();
  if (!config) {
    return NextResponse.json({ error: "Console authentication is not configured." },
      { status: 503, headers: { "Cache-Control": "no-store" } });
  }
  const token = request.cookies.get(sessionCookieName(request))?.value;
  if (!(await validSession(token, config))) {
    if (api) return NextResponse.json({ error: "Console sign-in required." },
      { status: 401, headers: { "Cache-Control": "no-store" } });
    // Keep a local tunnel on its original browser host. Behind a TLS proxy,
    // redirect to the configured public origin: Next's internal listener is HTTP.
    const local = localHttp(request);
    const login = new URL("/login", local ? request.url : process.env.BOSS_CONSOLE_PUBLIC_ORIGIN!);
    login.searchParams.set("next", path + request.nextUrl.search);
    const response = local ? NextResponse.rewrite(login) : NextResponse.redirect(login);
    response.headers.set("Cache-Control", "no-store");
    return response;
  }
  return passThrough(request);
}

/**
 * A link to a retired area is rewritten by an HTTP redirect, before anything renders.
 *
 * page.tsx's redirect() runs after the shell has streamed, so the browser followed it as a
 * client navigation and dropped the URL's fragment: `?area=delivery#hardware-manager` (the old
 * "Where apps install" anchor) landed on Infra › Fleet with no `#hardware-manager` left to open
 * This computer (P-I-07). The answer is a small page that moves itself (`location.replace`, which
 * keeps the fragment and the origin; see below for why not a 307). Document loads only: an RSC
 * fetch or a server action is the client's own navigation, which the shell rewrites itself.
 */
function legacyRedirect(request: NextRequest): NextResponse | null {
  if (request.method !== "GET" || request.nextUrl.pathname !== "/") return null;
  if (request.headers.get("rsc") || request.headers.get("next-action")) return null;
  const legacy = resolveLegacyArea(request.nextUrl.searchParams);
  if (!legacy) return null;
  // A RELATIVE MOVE, MADE BY THE BROWSER. A 307 with a relative Location passed the unit test
  // and 500ed in a production build (Next's middleware adapter parses Location as an absolute
  // URL: "TypeError: Invalid URL"); an absolute one is rewritten by that same adapter onto the
  // server's own bind name ("localhost"), which moves a browser on 127.0.0.1 to another origin
  // and drops its session. So the answer is a page that moves itself: `location.replace` keeps
  // the fragment (`#hardware-manager`) and the origin, and the refresh is the no-script path.
  const target = `/?${legacy}`;
  const html = `<!doctype html><meta charset="utf-8"><meta name="robots" content="noindex">`
    + `<meta http-equiv="refresh" content="0;url=${target.replace(/&/g, "&amp;")}">`
    + `<title>Moved</title><script>location.replace(${JSON.stringify(target)}+location.hash)</script>`
    + `<a href="${target.replace(/&/g, "&amp;")}">Continue</a>`;
  return new NextResponse(html, {
    status: 200,
    headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store",
               "X-Boss-Moved-To": target },
  });
}

/** A request that may reach the console: still never a cross-site write. */
async function passThrough(request: NextRequest) {
  if (!sameOriginMutation(request)) {
    return NextResponse.json({ error: "Cross-origin console mutation denied." },
      { status: 403, headers: { "Cache-Control": "no-store" } });
  }
  const moved = legacyRedirect(request);
  if (moved) return moved;
  const intentHeaders = await pressIntentHeaders(request);
  const response = intentHeaders
    ? NextResponse.next({ request: { headers: intentHeaders } })
    : NextResponse.next();
  response.headers.set("Cache-Control", "private, no-store");
  return response;
}

export const config = { matcher: ["/:path*"] };
