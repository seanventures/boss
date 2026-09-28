import { NextRequest, NextResponse } from "next/server";

import {
  consoleAuthConfig, consoleOrigin, sameOriginMutation, secureConsoleTransport,
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
    const login = new URL("/login", consoleOrigin(request));
    login.searchParams.set("next", path + request.nextUrl.search);
    const redirect = NextResponse.redirect(login);
    redirect.headers.set("Cache-Control", "no-store");
    return redirect;
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
