import { NextRequest, NextResponse } from "next/server";

import { localHttp, sessionCookieName } from "../../../../lib/console-session";

export async function POST(request: NextRequest) {
  const response = new NextResponse(null, { status: 303, headers: {
    Location: "/login", "Cache-Control": "no-store",
  } });
  response.cookies.set(sessionCookieName(request), "", {
    httpOnly: true, secure: !localHttp(request), sameSite: "strict", path: "/", maxAge: 0,
  });
  return response;
}
