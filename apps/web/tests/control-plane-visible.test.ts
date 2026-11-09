import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * The console says WHICH control plane it is reading.
 *
 * Two dev servers, identical chrome, completely different worlds: one pointed at a laptop
 * database showed a single project while the other showed forty, and nothing on either screen
 * explained the difference. The reasonable conclusion — the one that was actually reached — is
 * "we are losing data almost every time".
 *
 * For a customer with staging and production open in two tabs, the same ambiguity is worse than
 * confusing: it is a change made confidently against the wrong environment.
 */
const SHELL = codeOnly("app/ConsoleShell.tsx");
const PAGE = codeOnly("app/page.tsx");
const CSS = codeOnly("app/globals.css");

describe("the control plane is named", () => {
  it("renders the host beside the online indicator", () => {
    // "Control plane online" answers whether it is reachable, never which one it is.
    expect(SHELL).toContain('data-testid="control-plane-host"');
    expect(SHELL).toContain("planeLabel(status.controlPlane)");
  });

  it("takes the URL from the SERVER, not the browser", () => {
    // Every fetch on this page is server-side, and the build-time NEXT_PUBLIC value can
    // disagree with what is actually being called — which is exactly the case that misleads.
    expect(PAGE).toContain("controlPlane: API_URL");
    expect(PAGE).toContain('import { API_URL, PROJECT_ID } from "../lib/client"');
  });

  it("shortens a URL to the part that distinguishes it", () => {
    expect(SHELL).toContain("function planeLabel");
    expect(SHELL).toContain("new URL(url).host");
  });

  it("falls back to the raw value rather than showing nothing", () => {
    // An unparseable URL is still evidence about which backend is configured.
    const fn = SHELL.slice(SHELL.indexOf("function planeLabel"), SHELL.indexOf("function isLocalPlane"));
    expect(fn).toContain("catch");
    expect(fn).toContain("return url;");
  });
});

describe("a local control plane is called out", () => {
  it("marks localhost, 127.0.0.1, 0.0.0.0 and ::1", () => {
    const fn = SHELL.slice(SHELL.indexOf("function isLocalPlane"));
    for (const host of ["localhost", "127", "0\\.0\\.0\\.0", "::1"]) {
      expect(fn).toContain(host);
    }
  });

  it("says the word 'local', not only a colour", () => {
    expect(SHELL).toContain("`local, ${host}`");
  });

  it("styles it as a warning, since it wears production's chrome", () => {
    const rule = CSS.slice(CSS.indexOf(".bx-control-host {"), CSS.indexOf("/* ── Activity log"));
    expect(rule).toContain("[data-local]");
    expect(rule).toContain("--amber");
  });

  it("stays quiet for a normal control plane", () => {
    // Reassurance, not an alert: an indicator that shouts on every page becomes furniture.
    const rule = CSS.slice(CSS.indexOf(".bx-control-host {"), CSS.indexOf(".bx-control-host[data-local]"));
    expect(rule).toContain("--faint");
  });
});

describe("an internal control plane is not shown", () => {
  it("hides a compose-internal name like api:8000, which tells a person nothing", async () => {
    const { isInternalPlane } = await import("../app/ConsoleShell");
    expect(isInternalPlane("http://api:8000")).toBe(true);
    expect(isInternalPlane("http://localhost:8000")).toBe(false);
    expect(isInternalPlane("https://boss.acme.dev")).toBe(false);
    expect(isInternalPlane("http://10.0.0.5:8000")).toBe(false);
    expect(SHELL).toContain("!isInternalPlane(status.controlPlane)");
  });
});
