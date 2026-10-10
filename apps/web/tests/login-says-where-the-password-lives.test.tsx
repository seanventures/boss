// @vitest-environment happy-dom
/**
 * The page that asks for the shared console password says where it lives.
 *
 * The installer prints the command once, at the end of a long install, and a person who missed
 * it reached a password box and nothing else. Since identity S4 the shared password is no longer
 * a sign-in — it is the proof that you are the operator on the "Claim this BOSS" screen
 * (IDENTITY.md §4.11, D25) — so that is where the hint lives now. It names the command, never
 * the value, and appears only on the legacy-password claim.
 */
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { AuthOptions } from "../lib/identity/types";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams() }));

async function render(path: NonNullable<AuthOptions["claim"]>["path"]) {
  const { ClaimFlow } = await import("../app/(auth)/claim/claim");
  const options: AuthOptions = {
    org_name: "Acme BOSS", host: "boss.acme.dev", providers: [], smtp: false, signup: false, legacy_password: path === "legacy_password",
    claim: { path, origin: path === "network_bridge" ? "https://goldeneye.example.ts.net" : null, expires_at: null, seats: ["ceo"] },
  };
  return renderToStaticMarkup(<ClaimFlow options={options} />);
}

describe("the legacy-password claim", () => {
  it("tells you how to find the password on the server, and over SSH", async () => {
    const html = await render("legacy_password");
    expect(html).toContain('data-testid="console-password-hint"');
    expect(html).toContain("boss password");
    expect(html).toContain("ssh -t user@your-server");
  });

  it("never renders a password value", async () => {
    const html = await render("legacy_password");
    expect(html).not.toMatch(/BOSS_CONSOLE_PASSWORD=|value="[^"]{24,}"/);
  });

  it("shows the hint only where a password is asked for", async () => {
    expect(await render("network_bridge")).not.toContain('data-testid="console-password-hint"');
  });
});
