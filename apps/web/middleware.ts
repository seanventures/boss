import { NextRequest, NextResponse } from "next/server";

import {
  consoleAuthConfig, localHttp, sameOriginMutation, secureConsoleTransport,
  sessionCookieName, validSession,
} from "./lib/console-session";

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
  const response = NextResponse.next();
  response.headers.set("Cache-Control", "private, no-store");
  return response;
}

export const config = { matcher: ["/:path*"] };
