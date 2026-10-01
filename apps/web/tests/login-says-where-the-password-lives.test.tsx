/**
 * The sign-in page says where the console password lives.
 *
 * The installer prints the command once, at the end of a long install, and a person who missed
 * it reached a page with a password box and nothing else — "what is this console password?" was
 * the first question on a fresh v1.0-rc1 install. The page names the command, never the value.
 */
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("../lib/console-session", () => ({
  consoleAuthConfig: () => ({ password: "never-rendered", secret: "s" }),
  safeNext: (next: string | null) => next ?? "/",
}));

// Next compiles the page with the automatic JSX runtime; vitest here uses the classic one, which
// expects React in scope. Supplied for the test rather than imported into a page that needs none.
(globalThis as unknown as { React: typeof React }).React = React;

async function render(error?: string) {
  const { default: ConsoleLogin } = await import("../app/login/page");
  return renderToStaticMarkup(await ConsoleLogin({ searchParams: Promise.resolve({ error }) }));
}

describe("the console sign-in page", () => {
  it("tells you how to find the password on the server, and over SSH", async () => {
    const html = await render();
    expect(html).toContain('data-testid="console-password-hint"');
    expect(html).toContain("python3 scripts/install/console_password.py show");
    expect(html).toContain("ssh -t user@your-server");
    expect(html).toContain("https://boss.sean.network/docs/CONSOLE_AUTH/");
  });

  it("never renders the password itself", async () => {
    expect(await render()).not.toContain("never-rendered");
  });

  it("keeps the hint when the password was wrong — that is when it is needed most", async () => {
    const html = await render("invalid");
    expect(html).toContain("Incorrect password.");
    expect(html).toContain("console_password.py show");
  });
});
