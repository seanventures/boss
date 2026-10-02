import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

import { POST as login } from "../app/api/auth/login/route";
import {
  FREE_FAILURES, MAX_DELAY_MS, resetLoginThrottleForTests, trackedFailureCountForTests,
} from "../lib/login-throttle";
import { codeOnly } from "./_helpers/source";

/**
 * Sign-in keeps the deep link through a wrong password, slows a guesser, and there is a way out.
 *
 * Console walk, 2026-10-02: a wrong password redirected to /login?error=invalid and dropped
 * `next`, so the retry landed on the home page instead of the run the operator had opened; a
 * wrong guess cost nothing however many were made; and /api/auth/logout had no caller — the
 * only way to end a session on a shared machine was to clear cookies.
 */
const PASSWORD = "a".repeat(48);

function configured() {
  vi.stubEnv("BOSS_CONSOLE_PASSWORD", PASSWORD);
  vi.stubEnv("BOSS_CONSOLE_SESSION_SECRET", "b".repeat(64));
  vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", "https://boss.example");
}

const waits: number[] = [];
beforeEach(() => {
  waits.length = 0;
  resetLoginThrottleForTests(async (ms) => { waits.push(ms); });
});
afterEach(() => vi.unstubAllEnvs());

function attempt(password: string, next: string, ip: string) {
  const body = new URLSearchParams({ password, next });
  return login(new NextRequest("https://boss.example/api/auth/login", {
    method: "POST",
    body,
    headers: {
      "content-type": "application/x-www-form-urlencoded",
      origin: "https://boss.example",
      "x-forwarded-for": ip,
      "x-forwarded-proto": "https",
    },
  }));
}

describe("a wrong password", () => {
  it("returns to the form with the deep link still attached", async () => {
    configured();
    const response = await attempt("wrong", "/?area=code_tasks&run=crun_1", "10.0.0.1");
    expect(response.status).toBe(303);
    const location = response.headers.get("location")!;
    expect(location).toContain("error=invalid");
    expect(new URLSearchParams(location.split("?")[1]).get("next"))
      .toBe("/?area=code_tasks&run=crun_1");
  });

  it("never carries an off-site next back to the form", async () => {
    configured();
    const response = await attempt("wrong", "//evil.example", "10.0.0.2");
    expect(response.headers.get("location")).toBe("/login?error=invalid");
  });

  // REWRITTEN (review of milestone/console-b, 2026-10-02). This test used to assert that ten
  // misses from one X-Forwarded-For address refused even the right password from it, and that
  // another address was untouched. That was the wrong design: the header is the client's to
  // write, so a guesser rotated it and was never counted, and anyone could lock the owner out by
  // guessing in the owner's address. The backoff now keys on nothing a client sends, slows every
  // attempt once misses pile up, and never refuses the right password.
  it("slows every guesser down, whatever address it claims, and says so", async () => {
    configured();
    for (let i = 0; i < FREE_FAILURES; i++) await attempt("wrong", "/", `10.0.1.${i}`);
    expect(waits).toEqual([]);
    const slowedMiss = await attempt("wrong", "/", "10.0.9.9");
    expect(waits.length).toBe(1);
    expect(waits[0]).toBeGreaterThan(0);
    expect(slowedMiss.headers.get("location")).toBe("/login?error=invalid&slowed=1");
    // Rotating the claimed address does not reset anything; the wait only grows, to a cap.
    for (let i = 0; i < 40; i++) await attempt("wrong", "/", `192.0.2.${i}`);
    expect(waits.at(-1)).toBe(MAX_DELAY_MS);
    expect(codeOnly("app/login/page.tsx")).toContain("The right password still signs you in");
  });

  it("never refuses the right password, even after someone guessed in the owner's name", async () => {
    configured();
    for (let i = 0; i < FREE_FAILURES + 5; i++) await attempt("wrong", "/", "10.0.0.3");
    const owner = await attempt(PASSWORD, "/?area=code_tasks", "10.0.0.3");
    expect(owner.headers.get("location")).toBe("/?area=code_tasks");
    expect(owner.headers.get("set-cookie")).toBeTruthy();
    // The owner signing in clears the misses, so the next sign-in is not slowed.
    waits.length = 0;
    await attempt(PASSWORD, "/", "10.0.0.3");
    expect(waits).toEqual([]);
  });

  it("does not grow without limit however many addresses are claimed", async () => {
    configured();
    for (let i = 0; i < 200; i++) await attempt("wrong", "/", `198.51.100.${i % 250}`);
    expect(trackedFailureCountForTests()).toBeLessThanOrEqual(FREE_FAILURES + 32);
    expect(codeOnly("app/api/auth/login/route.ts")).not.toContain("x-forwarded-for");
  });

  it("checks concurrent guesses one at a time instead of all at once", async () => {
    configured();
    for (let i = 0; i < FREE_FAILURES; i++) await attempt("wrong", "/", "10.0.0.7");
    let inFlight = 0;
    let most = 0;
    const seen: number[] = [];
    resetLoginThrottleForTests(async (ms) => {
      inFlight += 1;
      most = Math.max(most, inFlight);
      await new Promise((resolve) => setTimeout(resolve, 5));
      inFlight -= 1;
      seen.push(ms);
    });
    for (let i = 0; i < FREE_FAILURES; i++) await attempt("wrong", "/", "10.0.0.7");
    await Promise.all(Array.from({ length: 6 }, (_, i) => attempt("wrong", "/", `10.0.2.${i}`)));
    expect(seen.length).toBeGreaterThanOrEqual(6);
    expect(most).toBe(1);
  });
});

describe("console sign-in that is not configured", () => {
  it("sends the form back to the page that says so, not raw JSON", async () => {
    const response = await attempt("anything", "/", "10.0.0.8");
    expect(response.status).toBe(303);
    expect(response.headers.get("location")).toBe("/login");
  });
});

describe("sign out", () => {
  it("is in the console's System menu and posts to the logout route", () => {
    const shell = codeOnly("app/ConsoleShell.tsx");
    expect(shell).toContain('<form action="/api/auth/logout" method="post" data-testid="bx-sign-out">');
    // Only when sign-in is configured: without a password there is no session to end.
    expect(shell).toContain("{signOutAvailable ? (");
    expect(codeOnly("app/page.tsx")).toContain("signOutAvailable={consoleAuthConfig() !== null}");
    expect(shell).toContain(">Sign out</button>");
  });
});
