/**
 * Stranger test r3 (rc10.28), the claim and sign-in words:
 *   - Reloading /claim during the second-factor step said "Already claimed / Sign in" to the
 *     person who had just claimed it, and the enrol screen's last button said "Continue" and
 *     landed on Projects. The signed-in Owner now resumes enrolment, and goes on to Setup with
 *     "Continue setup".
 *   - The authenticator prompt said "the 6-digit code for BOSS, BOSS".
 *   - "Lost every factor? An admin can reset it." on a one-Owner install, and the Forgot? sheet's
 *     plain `boss identity recover-owner` on a second install (plain `boss` runs the first one).
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { AFTER_CLAIM, claimResumeHref, enrolContinueLabel, firstOwnerNext } from "../lib/identity/claim-resume";
import { authenticatorLabel, ownBoss, recoverOwnerCommand, recoverOwnerStep, setupLinkCommand } from "../lib/identity/host-commands";
import type { MeResponse } from "../lib/identity/contract";

const WEB = join(__dirname, "..");
const read = (p: string) => readFileSync(join(WEB, p), "utf8");

type Me = Pick<MeResponse, "banners" | "access_roles">;
const enrolBanner = { id: "mfa_enrolment_required.x", kind: "mfa_enrolment_required", policy: "persistent" } as unknown as MeResponse["banners"][number];
const owner = (enrolling: boolean): Me => ({ access_roles: ["owner"], banners: enrolling ? [enrolBanner] : [] });

describe("a reload during the claim resumes the Owner", () => {
  it("sends the Owner who still needs a factor to enrol, coming back to Setup", () => {
    expect(claimResumeHref(owner(true))).toBe(`/enrol?next=${encodeURIComponent(AFTER_CLAIM)}`);
  });
  it("sends an enrolled Owner on to Setup", () => {
    expect(claimResumeHref(owner(false))).toBe(AFTER_CLAIM);
  });
  it("leaves anyone else, and nobody signed in, on the page", () => {
    expect(claimResumeHref(null)).toBeNull();
    expect(claimResumeHref({ access_roles: ["operator"], banners: [enrolBanner] })).toBeNull();
  });
  it("the claim page asks who is signed in before saying Already claimed", () => {
    const page = read("app/(auth)/claim/page.tsx");
    expect(page).toContain("claimResumeHref(");
    expect(page.indexOf("claimResumeHref(")).toBeLessThan(page.indexOf("<ClaimFlow"));
  });
});

describe("the enrol screen after a claim says Continue setup and goes to Setup", () => {
  it("labels the last button by where it goes", () => {
    expect(enrolContinueLabel(AFTER_CLAIM)).toBe("Continue setup");
    expect(enrolContinueLabel("/identity")).toBe("Continue");
  });
  it("an Owner's first factor goes on to Setup when Sign in asked only for /", () => {
    expect(firstOwnerNext(owner(true), "/")).toBe(AFTER_CLAIM);
    expect(firstOwnerNext(owner(true), undefined)).toBe(AFTER_CLAIM);
    expect(firstOwnerNext(owner(true), "/projects")).toBeNull();
    expect(firstOwnerNext(owner(false), "/")).toBeNull();
    expect(firstOwnerNext({ access_roles: ["admin"], banners: [enrolBanner] }, "/")).toBeNull();
  });
  it("the enrol screen and page use them", () => {
    expect(read("app/(auth)/enrol/enrol.tsx")).toContain("continueLabel={enrolContinueLabel(next)}");
    expect(read("app/(auth)/enrol/page.tsx")).toContain("firstOwnerNext(me.data, asked)");
  });
});

describe("sign-in words name this install", () => {
  it("names the org once in the authenticator prompt", () => {
    expect(authenticatorLabel("BOSS")).toBe("BOSS");
    expect(authenticatorLabel("")).toBe("BOSS");
    expect(authenticatorLabel("Acme")).toBe("BOSS, Acme");
    expect(read("app/(auth)/login/sign-in.tsx")).not.toContain("BOSS, {options.org_name}");
  });
  it("recover-owner uses this install's own command", () => {
    expect(recoverOwnerCommand(undefined)).toBe("boss identity recover-owner --email you@example.com");
    expect(recoverOwnerCommand("/home/dev/stranger-test/rc28/bin/boss"))
      .toBe("/home/dev/stranger-test/rc28/bin/boss identity recover-owner --email you@example.com");
    expect(recoverOwnerCommand("/tmp/$(id)/boss")).toBe("boss identity recover-owner --email you@example.com");
    expect(setupLinkCommand("/srv/b2/boss")).toBe("/srv/b2/boss identity setup-link");
    expect(ownBoss("boss")).toBe("boss");
  });
  it("says how the Owner recovers instead of only 'an admin can reset it'", () => {
    const signIn = read("app/(auth)/login/sign-in.tsx");
    expect(signIn).not.toContain("An admin can reset it.");
    expect(signIn).not.toContain("HOST_COMMANDS.recoverOwner");
    expect(signIn.match(/recoverOwnerStep\(options\.host_command\)/g)?.length).toBeGreaterThanOrEqual(2);
  });
  it("after the claim, with the path withheld, names the subcommand and 'this install's boss command'", () => {
    expect(recoverOwnerStep(null)).toEqual({ command: "identity recover-owner --email you@example.com", withOwnBoss: true });
    expect(recoverOwnerStep("boss")).toEqual({ command: "boss identity recover-owner --email you@example.com", withOwnBoss: false });
    expect(recoverOwnerStep("/srv/b2/boss").command).toBe("/srv/b2/boss identity recover-owner --email you@example.com");
    expect(read("app/(auth)/login/sign-in.tsx")).toContain("with this install\\u2019s boss command");
  });
});

describe("a passkey on the other loopback spelling says where it works", () => {
  it("names the bound origin when the page is another loopback spelling", async () => {
    const { passkeyBlockWords, passkeyOtherLoopbackWords } = await import("../lib/identity/passkey-support");
    const here = { hostname: "127.0.0.1", protocol: "http:", secure: true };
    expect(passkeyOtherLoopbackWords("http://localhost:3000", here)).toContain("http://localhost:3000");
    expect(passkeyBlockWords(null, here, "http://localhost:3000")).toContain("work at http://localhost:3000");
    // Same spelling, a domain, or nothing bound: not this case.
    expect(passkeyOtherLoopbackWords("http://localhost:3000", { hostname: "localhost" })).toBeNull();
    expect(passkeyOtherLoopbackWords("https://boss.example.com", here)).toBeNull();
    expect(passkeyOtherLoopbackWords(null, here)).toBeNull();
  });
  it("sign-in, enrol and My account pass the bound origin", () => {
    expect(read("lib/identity/sign-in-offers.ts")).toContain("options.public_origin");
    expect(read("app/(auth)/enrol/enrol.tsx")).toContain("passkeyPlace(), publicOrigin)");
    expect(read("app/account/security.tsx")).toContain("passkeyPlace(), publicOrigin)");
  });
});
