/**
 * Sign-in & SSO, as the real API serves it (S1 providers, S5 policy). Pure, tested
 * (tests/identity-admin-contract.test.ts).
 */
import type { Provider, SecurityPolicy } from "./types";

const PRESETS: Provider["preset"][] = ["google", "github", "microsoft", "apple", "okta", "entra", "keycloak", "generic"];

/**
 * A provider row as GET /v1/identity/providers serves it. Tolerates the older console shape the
 * dev-only identity stub still answers with (`kind`, `jit: "domains"`, preset "custom",
 * status "draft"/"error"), so a dev build renders either.
 */
export function providerView(raw: unknown): Provider {
  const r = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const str = (v: unknown) => (typeof v === "string" && v ? v : null);
  const protocol = (str(r.protocol) ?? str(r.kind) ?? "oidc") as Provider["protocol"];
  const preset = PRESETS.includes(r.preset as Provider["preset"]) ? r.preset as Provider["preset"] : "generic";
  const jit = str(r.jit_mode) ?? (r.jit === "domains" ? "domain" : str(r.jit));
  return {
    id: String(r.id ?? ""),
    name: String(r.name ?? r.id ?? ""),
    protocol: ["oidc", "saml", "oauth2"].includes(protocol) ? protocol : "oidc",
    preset,
    issuer: str(r.issuer),
    client_id: str(r.client_id),
    saml_sso_url: str(r.saml_sso_url),
    has_client_secret: Boolean(r.has_client_secret),
    scopes: Array.isArray(r.scopes) ? r.scopes.map(String) : [],
    allowed_algs: Array.isArray(r.allowed_algs) ? r.allowed_algs.map(String) : [],
    saml_sp_entity_id: str(r.saml_sp_entity_id),
    saml_certificate_count: typeof r.saml_certificate_count === "number" ? r.saml_certificate_count : 0,
    domains: Array.isArray(r.domains) ? r.domains.map(String) : [],
    jit_mode: jit === "domain" ? "domain" : "off",
    trust_idp_mfa: Boolean(r.trust_idp_mfa),
    enforce_sso: Boolean(r.enforce_sso),
    groups_claim: str(r.groups_claim),
    status: r.status === "active" ? "active" : "disabled",
    link_verified_domain_accounts: r.link_verified_domain_accounts === true,
    allow_idp_initiated: r.allow_idp_initiated === true,
    scim_adopt_existing_by_domain: r.scim_adopt_existing_by_domain === true,
  };
}

/** Keys the API computes or owns: never sent back unless the person acted on them. */
const NEVER_ROUND_TRIPPED = new Set(["smtp", "github_require_user_repo_access_auto", "network_mode"]);

/**
 * The PUT body for a policy save: ONLY the keys the person changed since the read. Sending the
 * whole object back would re-apply derived values — a stale `github_require_user_repo_access`
 * silently pins it, a stale `smtp` is a 422, a stale `network_mode.active:true` is a 409.
 */
export function policyChanges(saved: SecurityPolicy, edited: SecurityPolicy): Partial<SecurityPolicy> {
  const out: Record<string, unknown> = {};
  for (const key of Object.keys(edited)) {
    if (NEVER_ROUND_TRIPPED.has(key)) continue;
    if (JSON.stringify(edited[key]) !== JSON.stringify(saved[key])) out[key] = edited[key];
  }
  return out as Partial<SecurityPolicy>;
}

/** "Require sign-in": ends the network bridge, and nothing else in the same request. */
export const REQUIRE_SIGN_IN: Partial<SecurityPolicy> = { network_mode: { active: false, ends_at: null } };

/** Back to automatic (on once more than one person): PUT null. */
export const GITHUB_ACCESS_AUTOMATIC = { github_require_user_repo_access: null } as const;
