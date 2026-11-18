// @vitest-environment happy-dom
/**
 * The claim page names THIS install's command and tells the truth about the network.
 *
 * Stranger test r2 (2026-11-17), a second install on one machine opened over an SSH tunnel:
 *   - the page said `boss identity setup-link`, but plain `boss` there is the FIRST install, so
 *     running it would have printed a link for the other BOSS. The API now says the install's
 *     own command (`claim.host_command`, a second install's launcher path).
 *   - it said "You're not on the server's own network" to a browser on localhost (the tunnel).
 *
 * Breaks if the page ignores host_command, prints an unsafe one, or says "not on the server's
 * network" on a loopback page.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ClaimFlow } from "../app/(auth)/claim/claim";
import { onLoopback, setupLinkCommand } from "../lib/identity/host-commands";
import type { AuthOptions } from "../lib/identity/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => { act(() => root?.unmount()); host?.remove(); root = null; });

async function mount(node: React.ReactElement) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(node));
  for (let i = 0; i < 4; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  return host;
}

const options = (hostCommand?: string | null): AuthOptions => ({
  org_name: "BOSS", host: "localhost:3100", providers: [], smtp: false, signup: false, legacy_password: false,
  claim: { path: "setup_token", origin: null, expires_at: null, seats: [], host_command: hostCommand },
} as AuthOptions);

describe("setupLinkCommand", () => {
  it("is plain boss by default and the install's own launcher for a second install", () => {
    expect(setupLinkCommand(undefined)).toBe("boss identity setup-link");
    expect(setupLinkCommand("boss")).toBe("boss identity setup-link");
    expect(setupLinkCommand("/home/dev/stranger-test/bin2/boss")).toBe("/home/dev/stranger-test/bin2/boss identity setup-link");
    // Anything a shell would read differently is never printed.
    expect(setupLinkCommand("/tmp/x; rm -rf ~/boss")).toBe("boss identity setup-link");
    expect(setupLinkCommand("/tmp/$(id)/boss")).toBe("boss identity setup-link");
    expect(setupLinkCommand("/usr/bin/not-boss")).toBe("boss identity setup-link");
  });
});

describe("onLoopback", () => {
  it("is the server's own machine or a tunnel to it", () => {
    for (const h of ["localhost", "127.0.0.1", "127.1.2.3", "[::1]", "::1", "app.localhost"]) expect(onLoopback(h), h).toBe(true);
    for (const h of ["boss.example.com", "10.0.0.5", "203.0.113.9", "", null]) expect(onLoopback(h), String(h)).toBe(false);
  });
});

describe("the claim page without a link", () => {
  it("names the second install's command and says nothing about the network on localhost", async () => {
    window.history.replaceState(null, "", "/claim");
    expect(onLoopback(window.location.hostname)).toBe(true);
    const el = await mount(<ClaimFlow options={options("/home/dev/stranger-test/bin2/boss")} />);
    const box = el.querySelector("[data-testid='claim-no-token']")!;
    expect(box).not.toBeNull();
    expect(box.textContent).toContain("/home/dev/stranger-test/bin2/boss identity setup-link");
    expect(box.textContent).not.toMatch(/not on the server.s own network/);
    expect(el.querySelector("[data-testid='claim-why-link']")?.textContent).toBe("Claiming needs a one-hour link from the server.");
  });

  it("keeps plain boss on a default install", async () => {
    window.history.replaceState(null, "", "/claim");
    const el = await mount(<ClaimFlow options={options(null)} />);
    expect(el.querySelector("[data-testid='claim-no-token']")!.textContent).toContain("boss identity setup-link");
    expect(el.textContent).not.toContain("/boss identity");
  });
});
