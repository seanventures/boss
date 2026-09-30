import { NextRequest, NextResponse } from "next/server";

import {
  consoleAuthConfig, issueSession, passwordMatches, safeNext,
  sameOriginMutation, secureConsoleTransport, localHttp, sessionCookieName, SESSION_SECONDS,
} from "../../../../lib/console-session";

export async function POST(request: NextRequest) {
  const config = consoleAuthConfig();
  if (!config) return NextResponse.json({ error: "Console authentication is not configured." },
    { status: 503 });
  if (!secureConsoleTransport(request)) return NextResponse.json(
    { error: "The console requires HTTPS for remote access." }, { status: 426 });
  if (!sameOriginMutation(request)) return NextResponse.json(
    { error: "Cross-origin console sign-in denied." }, { status: 403 });
  const form = await request.formData();
  const password = form.get("password");
  if (typeof password !== "string" || !(await passwordMatches(password, config))) {
    return new NextResponse(null, { status: 303, headers: {
      Location: "/login?error=invalid", "Cache-Control": "no-store",
    } });
  }
  const destination = safeNext(typeof form.get("next") === "string" ? String(form.get("next")) : null);
  const response = new NextResponse(null, { status: 303, headers: {
    Location: destination, "Cache-Control": "no-store",
  } });
  response.cookies.set(sessionCookieName(request), await issueSession(config), {
    httpOnly: true, secure: !localHttp(request), sameSite: "strict", path: "/",
    maxAge: SESSION_SECONDS,
  });
  return response;
}
