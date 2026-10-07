/**
 * THE CLAIM USES THE PROOF AND THE ORIGIN THE BROWSER HOLDS (round 3, W1; J2, J14).
 *
 *   - A setup link (`#t=`) is honoured whenever the API lists setup_token in `claim.paths`, even
 *     where `claim.path` names the shared password (compose always sets one).
 *   - A network claim binds the API's origin, else the page's own — never an internal address.
 *   - Every server-side call carries the browser's host (X-Forwarded-Host/Proto), so
 *     /v1/auth/options answers about the browser's address, not `api:8000`.
 *   - A 401 that carries no code is "unauthenticated", and every console mode sends it to sign in.
 */
import { afterAll, describe, expect, it } from "vitest";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { claimOrigin, claimPathFor, claimProof } from "../lib/identity/claim-path";
import { refusalCode } from "../lib/identity/refusal-code";
import { forwardedHostHeaders, installIdentityResolver, withForwardedIdentity } from "../lib/identity/forward";
import type { AuthOptions } from "../lib/identity/types";

type Claim = NonNullable<AuthOptions["claim"]>;
const base: Claim = { path: "legacy_password", paths: ["setup_token", "legacy_password"], origin: null, expires_at: null, seats: [] };

describe("which proof", () => {
  it("a setup link wins on a shared-password install when the API accepts one", () => {
    expect(claimPathFor(base, "tok")).toBe("setup_token");
    expect(claimProof(claimPathFor(base, "tok"), { token: "tok", legacy: "", origin: null })).toEqual({ setup_token: "tok" });
  });
  it("without a link, the API's path stands", () => {
    expect(claimPathFor(base, "")).toBe("legacy_password");
    expect(claimPathFor({ ...base, path: "network_bridge", paths: ["setup_token", "network"] }, "")).toBe("network_bridge");
  });
  it("a link the API does not accept here is not used", () => {
    expect(claimPathFor({ ...base, paths: ["legacy_password"] }, "tok")).toBe("legacy_password");
    // An API that lists no paths accepts only the one it named.
    expect(claimPathFor({ ...base, paths: undefined }, "tok")).toBe("legacy_password");
    expect(claimPathFor({ ...base, path: "setup_token", paths: undefined }, "tok")).toBe("setup_token");
  });
});

describe("which origin a network claim binds", () => {
  const net: Claim = { ...base, path: "network_bridge", paths: ["setup_token", "network"] };
  it("the API's, else the page's own", () => {
    expect(claimOrigin({ ...net, origin: "https://boss.lan" }, "https://other")).toBe("https://boss.lan");
    expect(claimOrigin(net, "https://localhost:8443")).toBe("https://localhost:8443");
    expect(claimOrigin(net, null)).toBeNull();
  });
  it("is sent as confirm_origin, and never as an empty proof", () => {
    expect(claimProof("network_bridge", { token: "", legacy: "", origin: "https://boss.lan" })).toEqual({ confirm_origin: "https://boss.lan" });
    expect(claimProof("network_bridge", { token: "", legacy: "", origin: null })).toEqual({});
  });
  it("the claim page decides with these, not with claim.path / claim.origin", () => {
    const src = readFileSync(join(process.cwd(), "app/(auth)/claim/claim.tsx"), "utf8");
    expect(src).toMatch(/claimPathFor\(claim, token\)/);
    expect(src).toMatch(/claimOrigin\(claim, pageOrigin\)/);
    expect(src).toMatch(/window\.location\.origin/);
    expect(src).not.toMatch(/claim\.path\b|claim\.origin\b/);
  });
});

describe("server-side calls carry the browser's host", () => {
  afterAll(() => installIdentityResolver(async () => null));
  it("X-Forwarded-Host/Proto from the console origin the browser addressed", async () => {
    installIdentityResolver(async () => ({ cookie: "", csrf: null, origin: null, consoleOrigin: "https://localhost:8443" }));
    const h = await withForwardedIdentity("GET", { accept: "application/json" });
    expect(h.get("x-forwarded-host")).toBe("localhost:8443");
    expect(h.get("x-forwarded-proto")).toBe("https");
  });
  it("nothing when there is no request (a background render) or no origin", async () => {
    installIdentityResolver(async () => null);
    expect((await withForwardedIdentity("GET", undefined)).get("x-forwarded-host")).toBeNull();
    expect(forwardedHostHeaders("javascript:alert(1)")).toBeNull();
    expect(forwardedHostHeaders("https://u:p@x.example")).toBeNull();
    expect(forwardedHostHeaders(null)).toBeNull();
  });
  it("the resolver hands the console origin over", () => {
    const src = readFileSync(join(process.cwd(), "instrumentation.ts"), "utf8");
    expect(src).toMatch(/consoleOrigin: forward\.consoleOriginOf\(incoming\)/);
  });
});

describe("a 401 without a code is unauthenticated", () => {
  it("reads the API's transport refusal as a dead credential", () => {
    expect(refusalCode(401, { detail: "Valid BOSS API key or OIDC bearer token required." })).toBe("unauthenticated");
    expect(refusalCode(401, null)).toBe("unauthenticated");
  });
  it("keeps every real code", () => {
    expect(refusalCode(401, { error: "step_up_required" })).toBe("step_up_required");
    expect(refusalCode(401, { error: "invalid_credentials" })).toBe("invalid_credentials");
    expect(refusalCode(403, { error: "forbidden", permission: "user:read" })).toBe("forbidden");
    expect(refusalCode(403, { detail: "nope" })).toBe("nope");
    expect(refusalCode(429, {})).toBe("rate_limited");
  });
  it("browser and server read refusals with it", () => {
    for (const f of ["lib/identity/api.ts", "lib/identity/server.ts"]) {
      expect(readFileSync(join(process.cwd(), f), "utf8"), f).toMatch(/refusalCode\(res\.status/);
    }
  });
});

describe("every console mode sends an unauthenticated visitor to sign in", () => {
  function files(dir: string): string[] {
    return readdirSync(dir).flatMap((n) => {
      const p = join(dir, n);
      if (n === "node_modules" || n === ".next") return [];
      return statSync(p).isDirectory() ? files(p) : /\.tsx?$/.test(n) ? [p] : [];
    });
  }
  it("no network-mode exemption anywhere", () => {
    for (const f of [...files(join(process.cwd(), "app")), ...files(join(process.cwd(), "components")), ...files(join(process.cwd(), "lib"))]) {
      expect(readFileSync(f, "utf8"), f).not.toMatch(/"unauthenticated"\s*&&\s*!networkTrustedConsole\(\)/);
    }
  });
});
