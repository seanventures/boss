/**
 * Sign-in keeps the deep link, a wrong password says one uniform thing, and there is a way out.
 *
 * Console walk, 2026-10-02: a wrong password dropped `next`, so the retry landed on the home page
 * instead of the run the operator had opened; and /api/auth/logout had no caller. Since identity
 * S4 the API signs people in (POST /v1/auth/login; rate limits, the progressive delay and the
 * soft lock are S1's, IDENTITY.md §4.10) and sets the session itself. The console keeps the
 * place and offers Sign out everywhere it shows who you are.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

import { middleware } from "../middleware";
import { safeNextClient } from "../app/(auth)/auth-parts";
import { codeOnly } from "./_helpers/source";

afterEach(() => vi.unstubAllEnvs());

describe("the place you were going", () => {
  it("travels to the sign-in page", async () => {
    vi.stubEnv("BOSS_CONSOLE_PUBLIC_ORIGIN", "https://boss.example");
    const r = await middleware(new NextRequest("https://boss.example/?area=code_tasks&run=crun_1", { headers: { host: "boss.example" } }));
    expect(new URL(r.headers.get("location")!).searchParams.get("next")).toBe("/?area=code_tasks&run=crun_1");
  });

  it("is kept through every step of sign-in, and never leaves the console", () => {
    const page = codeOnly("app/(auth)/login/page.tsx");
    expect(page).toContain("safeNext(params.next ?? null)");
    const flow = codeOnly("app/(auth)/login/sign-in.tsx");
    expect(flow).toContain("const target = safeNextClient(next);");
    // Done lands on the target; the one exception is §4.7's forced regeneration (fewer than 3
    // recovery codes left), which goes to /account to make new ones (d89a07d1). Enrolment keeps it.
    expect(flow).toContain('window.location.assign(step.must_regenerate_recovery_codes ? "/account" : target)');
    expect(flow).toContain("window.location.assign(afterEnrolStep(step, target))");
    expect(safeNextClient("/?area=code_tasks&run=crun_1")).toBe("/?area=code_tasks&run=crun_1");
    expect(safeNextClient("//evil.example")).toBe("/");
    expect(safeNextClient("https://evil.example")).toBe("/");
    expect(safeNextClient("/login?next=/x")).toBe("/");
  });
});

describe("a wrong password", () => {
  it("is one uniform sentence, whether or not the account exists (A3)", () => {
    const flow = codeOnly("app/(auth)/login/sign-in.tsx");
    expect(flow).toContain('invalid_credentials: "Wrong email or password"');
    expect(flow).not.toMatch(/no such (user|account)|unknown (user|email)/i);
  });

  it("the console no longer guesses or throttles passwords itself: the API does", () => {
    expect(codeOnly("middleware.ts")).not.toMatch(/password/i);
    const { existsSync } = require("node:fs") as typeof import("node:fs");
    expect(existsSync("app/api/auth/login/route.ts")).toBe(false);
  });
});

describe("sign out", () => {
  it("is under the avatar and in the System menu, and ends the API's session", () => {
    const menu = codeOnly("components/identity/account-menu.tsx");
    expect(menu).toContain('idPost("/v1/auth/logout")');
    expect(menu).toContain('window.location.assign("/login")');
    expect(menu).toContain('data-testid="sign-out"');
    const shell = codeOnly("app/ConsoleShell.tsx");
    expect(shell).toContain("<AccountMenu />");
    expect(shell).toContain('<button type="button" className="cap-btn" onClick={signOut}>Sign out</button>');
  });
});
