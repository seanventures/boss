/**
 * NAVIGATION NEVER FLASHES THE ORG PICKER (P0-B).
 *
 * "Select your organization", with "Opening…" on the BOSS card, flashed during plain navigation
 * between console pages: the page gated on `?org=` alone, so any request that lost it (one of
 * many URL builders, a page load of an area, a route page's rail) drew the picker and OrgGate's
 * stored-org effect bounced the browser back. The class is closed on the server: a URL without
 * an org means the org this session is in, and the picker is drawn only for a genuine choice.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { ORG_COOKIE, orgCookie, resolveConsoleOrg, withOrgRestored } from "../lib/console-org";

const boss = { org_id: "org_boss" };
const other = { org_id: "org_other" };

describe("resolveConsoleOrg", () => {
  it("a URL that lost ?org= opens the org the session is in, not the picker", () => {
    expect(resolveConsoleOrg({ requested: null, remembered: "org_boss", orgs: [other, boss] })).toBe(boss);
  });
  it("one organization is not a choice", () => {
    expect(resolveConsoleOrg({ requested: undefined, orgs: [boss] })).toBe(boss);
  });
  it("the URL's org wins over the remembered one", () => {
    expect(resolveConsoleOrg({ requested: "org_other", remembered: "org_boss", orgs: [boss, other] })).toBe(other);
  });
  it("the picker is still drawn for a genuine choice", () => {
    // asked to switch
    expect(resolveConsoleOrg({ pick: true, remembered: "org_boss", orgs: [boss] })).toBeUndefined();
    // nothing remembered, several orgs
    expect(resolveConsoleOrg({ orgs: [boss, other] })).toBeUndefined();
    // a remembered org that no longer exists
    expect(resolveConsoleOrg({ remembered: "org_gone", orgs: [boss, other] })).toBeUndefined();
    // a link naming an org that is not in the list is a real mismatch
    expect(resolveConsoleOrg({ requested: "org_gone", remembered: "org_boss", orgs: [boss, other] })).toBeUndefined();
  });
});

describe("the org survives in the address bar and the cookie", () => {
  it("restores ?org= keeping the rest of the URL", () => {
    expect(withOrgRestored("?area=infra&view=fleet", "org_boss")).toBe("?area=infra&view=fleet&org=org_boss");
    expect(withOrgRestored("?org=org_boss&area=infra", "org_boss")).toBeNull();
    expect(withOrgRestored("?org_id=org_boss", "org_boss")).toBeNull();
  });
  it("the cookie is site-wide and lax", () => {
    expect(orgCookie("org_boss")).toBe(`${ORG_COOKIE}=org_boss; Path=/; Max-Age=31536000; SameSite=Lax`);
  });
});

describe("wired in", () => {
  const page = readFileSync(join(process.cwd(), "app/page.tsx"), "utf8");
  const shell = readFileSync(join(process.cwd(), "app/ConsoleShell.tsx"), "utf8");
  const gate = readFileSync(join(process.cwd(), "app/org-gate.tsx"), "utf8");
  it("the page decides the org with resolveConsoleOrg from the remembered-org cookie", () => {
    expect(page).toContain("requestCookies.get(ORG_COOKIE)");
    expect(page).toMatch(/const selectedOrg = resolveConsoleOrg\(/);
    expect(page).not.toMatch(/const selectedOrg = requestedOrgId\s*\?/);
    // the remembered org reconnects on an API blip instead of showing an empty picker
    expect(page).toContain("if (heldOrgId && (!orgs.apiReachable");
  });
  it("the shell and the picker remember the org they open", () => {
    expect(shell).toContain("rememberOrg(orgId)");
    expect(shell).toContain("withOrgRestored(window.location.search, orgId)");
    expect(gate).toContain("rememberOrg(orgId)");
  });
});
