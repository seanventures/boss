import { NextRequest, NextResponse } from "next/server";

import {
  consoleAuthConfig, localHttp, sameOriginMutation, secureConsoleTransport,
  sessionCookieName, validSession,
} from "./lib/console-session";
import { INTENT_HEADER, intentFromActionBody } from "./lib/api-write";

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
  const api = path.startsWith("/api/") || path.startsWith("/v1/");
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
  if (!sameOriginMutation(request)) {
    return NextResponse.json({ error: "Cross-origin console mutation denied." },
      { status: 403, headers: { "Cache-Control": "no-store" } });
  }
  const intentHeaders = await pressIntentHeaders(request);
  const response = intentHeaders
    ? NextResponse.next({ request: { headers: intentHeaders } })
    : NextResponse.next();
  response.headers.set("Cache-Control", "private, no-store");
  return response;
}

export const config = { matcher: ["/:path*"] };
