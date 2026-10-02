import { afterEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

import { POST as login } from "../app/api/auth/login/route";
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

  it("slows a guesser down and says how long to wait", async () => {
    configured();
    for (let i = 0; i < 10; i++) await attempt("wrong", "/", "10.0.0.3");
    const blocked = await attempt(PASSWORD, "/", "10.0.0.3");
    expect(blocked.headers.get("location")).toContain("error=throttled");
    // Another address is not punished for this one's guesses.
    const other = await attempt(PASSWORD, "/", "10.0.0.4");
    expect(other.headers.get("location")).toBe("/");
    expect(codeOnly("app/login/page.tsx")).toContain("Wait five minutes, then sign in");
  });
});

describe("sign out", () => {
  it("is in the console's System menu and posts to the logout route", () => {
    const shell = codeOnly("app/ConsoleShell.tsx");
    expect(shell).toContain('<form action="/api/auth/logout" method="post" data-testid="bx-sign-out">');
    expect(shell).toContain(">Sign out</button>");
  });
});
