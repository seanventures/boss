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
 * This computer (P-I-07). A real 307 keeps the fragment (RFC 7231 §7.1.2). The Location is on the
 * host the browser used (its proxy's forwarded host), so a console behind a proxy stays on it. Document loads only: an RSC fetch or
 * a server action is the client's own navigation, which the shell rewrites itself.
 */
function legacyRedirect(request: NextRequest): NextResponse | null {
  if (request.method !== "GET" || request.nextUrl.pathname !== "/") return null;
  if (request.headers.get("rsc") || request.headers.get("next-action")) return null;
  const legacy = resolveLegacyArea(request.nextUrl.searchParams);
  if (!legacy) return null;
  // ABSOLUTE, ON THE HOST THE BROWSER USED. A relative Location passed the unit test and 500ed
  // in a production build ("TypeError: Invalid URL" in Next's middleware adapter), so every
  // retired-area link — ?area=govern, ?area=delivery, ?area=pipeline — failed on a real console.
  // The host and scheme are the request's own (or its proxy's forwarded ones), never the API's.
  const host = request.headers.get("x-forwarded-host") || request.headers.get("host")
    || request.nextUrl.host;
  const proto = (request.headers.get("x-forwarded-proto") || request.nextUrl.protocol)
    .replace(/:$/, "");
  return new NextResponse(null, {
    status: 307,
    headers: { Location: `${proto}://${host}/?${legacy}`, "Cache-Control": "no-store" },
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
