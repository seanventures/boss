/**
 * A DEAD SESSION SIGNS IN BEFORE THE ORG PICKER (round 2, R2-WEB).
 *
 * A revoked or expired cookie, or a network-mode console after Require sign-in, opened / and got
 * the org picker with an enabled "Onboard organization": the page asked for the org list before it
 * asked who was there. The API's 401 on /v1/identity/me now decides first, in every console mode.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { signInAgainHref } from "../lib/identity/enrolment";

describe("signInAgainHref", () => {
  it("a 401 goes to /login, keeping the place", () => {
    expect(signInAgainHref("unauthenticated", "/?area=skills&org=org_boss"))
      .toBe(`/login?next=${encodeURIComponent("/?area=skills&org=org_boss")}`);
  });
  it("anything else does not (a 403, an unreachable API, a live session)", () => {
    for (const p of [null, undefined, "forbidden", "identity_unavailable", "enrolment_required"]) {
      expect(signInAgainHref(p, "/")).toBeNull();
    }
  });
  it("never sends the browser off-site or back to the sign-in page", () => {
    expect(signInAgainHref("unauthenticated", "//evil.example")).toBe("/login?next=%2F");
    expect(signInAgainHref("unauthenticated", "https://evil.example")).toBe("/login?next=%2F");
    expect(signInAgainHref("unauthenticated", "/login?next=/")).toBe("/login?next=%2F");
  });
});

describe("the console page", () => {
  const src = readFileSync(join(process.cwd(), "app/page.tsx"), "utf8");
  it("decides sign-in before it fetches the org list or renders the picker", () => {
    const gate = src.indexOf("signInAgainHref(enrolment.problem");
    expect(gate).toBeGreaterThan(0);
    expect(gate).toBeLessThan(src.indexOf("await fetchOrgs()"));
    expect(gate).toBeLessThan(src.indexOf("<OrgGate"));
  });
  it("in every console mode: no network-mode exemption from the 401", () => {
    expect(src).not.toMatch(/problem === "unauthenticated" && !networkTrustedConsole\(\)/);
  });
});
