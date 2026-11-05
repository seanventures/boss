/**
 * AN SVG DRAWN THROUGH <img> HAS ITS NAMESPACE, AND A PASSKEY FAILURE SAYS WHY (round 3, boss-1).
 *
 * The TOTP QR came from the API without xmlns and showed as a broken image; every SVG data URI is
 * now built by lib/svg-data-uri.ts, which guarantees it. And WebAuthn on an IP address failed
 * with a bare "That didn't work": the console now judges the address first and names every
 * browser refusal (lib/identity/passkey-support.ts).
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { standaloneSvg, svgDataUri } from "../lib/svg-data-uri";
import {
  PASSKEY_NEEDS_DOMAIN_WORDS, PASSKEY_NEEDS_HTTPS_WORDS, PasskeyFailure, isIpHost, passkeyBlockWords, passkeyFailureWords,
} from "../lib/identity/passkey-support";
import { PasskeyCancelled } from "../lib/identity/webauthn";
import { sayAuth } from "../app/(auth)/auth-flow";

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    if (n === "node_modules" || n === ".next") return [];
    return statSync(p).isDirectory() ? files(p) : /\.tsx?$/.test(n) ? [p] : [];
  });
}

describe("svgDataUri", () => {
  it("adds the namespace when the root lacks it, and only then", () => {
    expect(standaloneSvg('<svg width="2"><g/></svg>')).toBe('<svg xmlns="http://www.w3.org/2000/svg" width="2"><g/></svg>');
    const ok = '<svg xmlns="http://www.w3.org/2000/svg" width="2"/>';
    expect(standaloneSvg(ok)).toBe(ok);
    expect(standaloneSvg('<?xml version="1.0"?>\n<svg class="segno"><g/></svg>')).toContain('<svg xmlns="http://www.w3.org/2000/svg" class="segno">');
  });
  it("is base64 of the standalone SVG", () => {
    const uri = svgDataUri("<svg><text>é</text></svg>");
    expect(uri.startsWith("data:image/svg+xml;base64,")).toBe(true);
    expect(Buffer.from(uri.split(",")[1], "base64").toString("utf8")).toBe('<svg xmlns="http://www.w3.org/2000/svg"><text>é</text></svg>');
  });
  it("is the only way the console builds an SVG data URI", () => {
    const cwd = process.cwd();
    const offenders = [...files(join(cwd, "app")), ...files(join(cwd, "components")), ...files(join(cwd, "lib"))]
      .filter((f) => !f.endsWith("lib/svg-data-uri.ts") && /data:image\/svg\+xml/.test(readFileSync(f, "utf8")));
    expect(offenders).toEqual([]);
  });
});

describe("where a passkey can be made", () => {
  it("not on an IP address, nor plain http off localhost", () => {
    expect(isIpHost("100.64.0.20")).toBe(true);
    expect(isIpHost("[fd7a:115c:a1e0::1]")).toBe(true);
    expect(isIpHost("boss.example.com")).toBe(false);
    expect(passkeyBlockWords("passkey_needs_domain", null)).toBe(PASSKEY_NEEDS_DOMAIN_WORDS);
    expect(passkeyBlockWords(null, { hostname: "100.64.0.20", protocol: "https:" })).toBe(PASSKEY_NEEDS_DOMAIN_WORDS);
    expect(passkeyBlockWords(null, { hostname: "boss.lan", protocol: "http:" })).toBe(PASSKEY_NEEDS_HTTPS_WORDS);
    expect(passkeyBlockWords(null, { hostname: "localhost", protocol: "http:" })).toBeNull();
    expect(passkeyBlockWords(null, { hostname: "boss.example.com", protocol: "https:" })).toBeNull();
    // Not bound yet is not a reason to hide them: the claim binds the origin.
    expect(passkeyBlockWords("public_origin_unset", { hostname: "boss.example.com", protocol: "https:" })).toBeNull();
  });

  it("an HTTPS or other secure context is not told the page is plain HTTP (BOSSAPP-662)", () => {
    // The API judged a forwarded http scheme. The page the operator has open is already secure.
    const https = passkeyBlockWords("passkey_needs_https", { hostname: "boss.example.com", protocol: "https:", secure: true });
    expect(https).not.toBe(PASSKEY_NEEDS_HTTPS_WORDS);
    expect(https ?? "").not.toMatch(/plain HTTP/);
    // isSecureContext, even when the protocol string the API saw was http.
    const trusted = passkeyBlockWords("passkey_needs_https", { hostname: "boss.example.com", protocol: "http:", secure: true });
    expect(trusted ?? "").not.toMatch(/plain HTTP/);
    // Loopback http is a secure context: an IP still needs a domain, never the plain-HTTP sentence.
    const loopback = passkeyBlockWords("passkey_needs_https", { hostname: "127.0.0.1", protocol: "http:", secure: true });
    expect(loopback).toBe(PASSKEY_NEEDS_DOMAIN_WORDS);
    expect(loopback).not.toMatch(/plain HTTP/);
    // A page that really is plain HTTP, and a location we cannot see, still say so.
    expect(passkeyBlockWords("passkey_needs_https", { hostname: "boss.lan", protocol: "http:" })).toBe(PASSKEY_NEEDS_HTTPS_WORDS);
    expect(passkeyBlockWords("passkey_needs_https", null)).toBe(PASSKEY_NEEDS_HTTPS_WORDS);
  });
});

describe("a passkey failure always says why", () => {
  it("names the browser's reason, and never the bare sentence", () => {
    expect(passkeyFailureWords(new PasskeyFailure("SecurityError"))).toMatch(/can't hold a passkey/);
    expect(passkeyFailureWords(new PasskeyCancelled())).toBe("Passkey cancelled or timed out");
    expect(passkeyFailureWords(new DOMException("x", "InvalidStateError"))).toBe("This device already has a passkey here");
    expect(passkeyFailureWords(new PasskeyFailure("SomethingNew"))).toBe("Passkey failed (SomethingNew)");
    for (const e of [new PasskeyFailure("SecurityError"), new Error("boom"), null, undefined, "x"]) {
      expect(sayAuth(e)).not.toBe("That didn't work");
    }
  });
  it("no passkey screen ends in the bare sentence", () => {
    for (const f of ["app/(auth)/auth-flow.ts", "app/(auth)/login/sign-in.tsx", "components/identity/step-up-sheet.tsx", "app/account/security.tsx"]) {
      expect(readFileSync(join(process.cwd(), f), "utf8"), f).not.toMatch(/["']That didn't work["']/);
    }
  });
});
