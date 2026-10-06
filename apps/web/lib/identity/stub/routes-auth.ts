/**
 * Extra stub routes for the auth screens and My account. Called by stub/server.ts after its core
 * table, with its `on(method, pattern, needs, handler)`. Helpers (stubOk, stubErr, …) are
 * imported from ./server — safe, because they are only called when a request is handled.
 *
 * Seams these stand in for (S1): passkey registration options, renaming a passkey, turning the
 * authenticator off, ending every other session, linked identities, self sign-up verification,
 * and (S3) starting a GitHub connection.
 */
import type { StubOn } from "./server";
import { stubErr, stubOk } from "./server";
import type { LinkedIdentity } from "../types-auth";

export function registerAuthRoutes(on: StubOn): void {
  on("POST", "/v1/identity/me/passkeys/options", "self", ({ s, who }) => {
    const p = s.people.find((x) => x.id === who);
    return stubOk({
      stub: true, challenge: "c3R1Yi1yZWdpc3Rlcg", rp: { id: "boss.acme.dev", name: "BOSS" },
      user: { id: "dXNyX3N0dWI", name: p?.email ?? who, displayName: p?.display_name ?? who },
      pubKeyCredParams: [{ type: "public-key", alg: -8 }, { type: "public-key", alg: -7 }],
      timeout: 60_000, authenticatorSelection: { residentKey: "required", userVerification: "required" }, attestation: "none",
    });
  });

  on("PATCH", "/v1/identity/me/passkeys/([^/]+)", "self", ({ s, who, m, req }) => {
    const f = (s.factors[who] ?? []).find((x) => x.id === m[1]);
    if (!f) return stubErr(404, "not_found");
    const name = String((req.body as { name?: unknown } | null)?.name ?? "").trim();
    if (!name) return stubErr(422, "invalid_fields");
    f.name = name.slice(0, 60);
    return stubOk(f);
  });

  on("DELETE", "/v1/identity/me/mfa/totp", "self", ({ s, who }) => {
    const p = s.people.find((x) => x.id === who);
    const mfaRole = p?.bindings.some((b) => ["owner", "admin", "auditor"].includes(b.role_key));
    if (mfaRole && !p?.sign_in.passkeys) return stubErr(409, "last_factor");
    s.factors[who] = (s.factors[who] ?? []).filter((x) => x.kind !== "totp");
    if (p) p.sign_in.totp = false;
    return stubOk({ ok: true });
  });

  on("DELETE", "/v1/identity/me/sessions", "self", ({ s, who }) => {
    s.sessions[who] = (s.sessions[who] ?? []).filter((x) => x.current);
    return stubOk({ ok: true });
  });

  on("GET", "/v1/identity/me/identities", "self", ({ s, who, now }) => {
    const p = s.people.find((x) => x.id === who);
    const identities: LinkedIdentity[] = p?.sign_in.sso
      ? [{ id: "lnk_okta", provider: p.sign_in.sso, preset: "okta", label: "acme.okta.com", linked_at: new Date(now - 55 * 86_400_000).toISOString() }]
      : [];
    return stubOk({ identities });
  });

  on("DELETE", "/v1/identity/me/identities/([^/]+)", "self", () => stubOk({ ok: true }));

  on("POST", "/v1/identity/me/connections", "connection:use_own", ({ req }) => {
    const provider = String((req.body as { provider?: unknown } | null)?.provider ?? "github");
    return stubOk({ redirect: `/account/agents?connected=${encodeURIComponent(provider)}` });
  });

  on("POST", "/v1/auth/signup/verify", "public", ({ req }) => {
    const token = String((req.body as { token?: unknown } | null)?.token ?? "");
    return token ? stubOk({ next: "enrol", methods: ["passkey", "totp"], who: null }) : stubErr(401, "link_invalid");
  });
}
