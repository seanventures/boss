import { NextRequest, NextResponse } from "next/server";

import {
  consoleAuthConfig, issueSession, passwordMatches, safeNext,
  sameOriginMutation, secureConsoleTransport, localHttp, sessionCookieName, SESSION_SECONDS,
} from "../../../../lib/console-session";
import { recordFailure, recordSuccess, throughGate } from "../../../../lib/login-throttle";

// Wrong-password backoff: see lib/login-throttle.ts for why it keys on nothing a client sends
// and never refuses the right password.
/** Back to the form, KEEPING where the operator was going. A wrong password used to redirect to
 *  /login?error=invalid and drop `next`, so the retry landed on the home page instead of the deep
 *  link they had opened. */
function backToLogin(error: string, next: string, slowed = false): NextResponse {
  const params = new URLSearchParams({ error });
  if (slowed) params.set("slowed", "1");
  if (next !== "/") params.set("next", next);
  return new NextResponse(null, { status: 303, headers: {
    Location: `/login?${params.toString()}`, "Cache-Control": "no-store",
  } });
}

export async function POST(request: NextRequest) {
  const config = consoleAuthConfig();
  // Not configured: back to the sign-in page, which says so in words and links the guide. This
  // answered raw JSON, which is what an operator saw in the browser after posting the form.
  if (!config) return new NextResponse(null, { status: 303, headers: {
    Location: "/login", "Cache-Control": "no-store",
  } });
  if (!secureConsoleTransport(request)) return NextResponse.json(
    { error: "The console requires HTTPS for remote access." }, { status: 426 });
  if (!sameOriginMutation(request)) return NextResponse.json(
    { error: "Cross-origin console sign-in denied." }, { status: 403 });
  const form = await request.formData();
  const password = form.get("password");
  const destination = safeNext(typeof form.get("next") === "string" ? String(form.get("next")) : null);
  // Monotonic and process-local, which is all a five-minute window in one process needs.
  const now = performance.now();
  const { result: matched, slowed } = await throughGate(now, async () =>
    typeof password === "string" && (await passwordMatches(password, config)));
  if (!matched) {
    recordFailure(performance.now());
    return backToLogin("invalid", destination, slowed);
  }
  recordSuccess();
  const response = new NextResponse(null, { status: 303, headers: {
    Location: destination, "Cache-Control": "no-store",
  } });
  response.cookies.set(sessionCookieName(request), await issueSession(config), {
    httpOnly: true, secure: !localHttp(request), sameSite: "strict", path: "/",
    maxAge: SESSION_SECONDS,
  });
  return response;
}
