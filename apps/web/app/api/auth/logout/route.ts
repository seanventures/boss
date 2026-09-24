import { NextRequest, NextResponse } from "next/server";

import { consoleOrigin, localHttp, sessionCookieName } from "../../../../lib/console-session";

export async function POST(request: NextRequest) {
  const response = NextResponse.redirect(new URL("/login", consoleOrigin(request)), 303);
  response.cookies.set(sessionCookieName(request), "", {
    httpOnly: true, secure: !localHttp(request), sameSite: "strict", path: "/", maxAge: 0,
  });
  response.headers.set("Cache-Control", "no-store");
  return response;
}
