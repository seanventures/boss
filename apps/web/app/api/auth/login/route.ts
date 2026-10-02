import { NextRequest, NextResponse } from "next/server";

import {
  consoleAuthConfig, issueSession, passwordMatches, safeNext,
  sameOriginMutation, secureConsoleTransport, localHttp, sessionCookieName, SESSION_SECONDS,
} from "../../../../lib/console-session";

/**
 * WRONG-PASSWORD THROTTLE, per client address, in this process.
 *
 * The console is one shared installer password guarding every key the server holds, and a
 * wrong guess cost nothing, so nothing slowed a script that kept guessing. After FAILURE_LIMIT
 * misses inside the window the form says to wait, and a right password still works once the
 * window has passed. In memory on purpose: one console process, and a restart forgetting the
 * count is acceptable for a deterrent that never locks the owner out for long.
 */
const FAILURE_LIMIT = 10;
const FAILURE_WINDOW_MS = 5 * 60_000;
const failures = new Map<string, number[]>();

function clientKey(request: NextRequest): string {
  return request.headers.get("x-forwarded-for")?.split(",")[0]?.trim()
    || request.headers.get("x-real-ip")?.trim() || "local";
}

function recentFailures(key: string, now: number): number[] {
  const kept = (failures.get(key) ?? []).filter((at) => now - at < FAILURE_WINDOW_MS);
  if (kept.length) failures.set(key, kept);
  else failures.delete(key);
  return kept;
}

/** Back to the form, KEEPING where the operator was going. A wrong password used to redirect to
 *  /login?error=invalid and drop `next`, so the retry landed on the home page instead of the deep
 *  link they had opened. */
function backToLogin(error: string, next: string): NextResponse {
  const params = new URLSearchParams({ error });
  if (next !== "/") params.set("next", next);
  return new NextResponse(null, { status: 303, headers: {
    Location: `/login?${params.toString()}`, "Cache-Control": "no-store",
  } });
}

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
  const destination = safeNext(typeof form.get("next") === "string" ? String(form.get("next")) : null);
  const key = clientKey(request);
  // Monotonic and process-local, which is all a five-minute window in one process needs.
  const now = performance.now();
  if (recentFailures(key, now).length >= FAILURE_LIMIT) {
    return backToLogin("throttled", destination);
  }
  if (typeof password !== "string" || !(await passwordMatches(password, config))) {
    failures.set(key, [...recentFailures(key, now), now]);
    return backToLogin("invalid", destination);
  }
  failures.delete(key);
  const response = new NextResponse(null, { status: 303, headers: {
    Location: destination, "Cache-Control": "no-store",
  } });
  response.cookies.set(sessionCookieName(request), await issueSession(config), {
    httpOnly: true, secure: !localHttp(request), sameSite: "strict", path: "/",
    maxAge: SESSION_SECONDS,
  });
  return response;
}
