/**
 * Stub routes for Identity › Sign-in: providers (add, edit, delete), group mappings and SCIM keys.
 * Dev-only double of apps/api/app/routers/identity_sso.py (and FX-B's GET /providers/{id});
 * registered by stub/server.ts. The core table answers GET list, PATCH and test; PATCH stores
 * `group_mappings` as the dict the API takes, and the detail here turns it into the list the
 * API answers.
 */
import * as F from "./fixtures";
import type { StubOn } from "./server";
import { stubAudit, stubErr, stubNotFound, stubOk, stubState } from "./server";

type Key = { id: string; name: string; status: "active" | "revoked"; created_at: string; expires_at: string | null; last_used_at: string | null };
const KEYS = new WeakMap<object, Map<string, Key[]>>();
function keysOf(provider: string): Key[] {
  const s = stubState();
  if (!KEYS.has(s)) KEYS.set(s, new Map());
  const m = KEYS.get(s)!;
  if (!m.has(provider)) m.set(provider, []);
  return m.get(provider)!;
}
const rid = (n = 8) => Math.random().toString(36).slice(2, 2 + n);

export function registerSsoRoutes(on: StubOn): void {
  on("POST", "/v1/identity/providers", "identity_provider:manage", ({ s, req, who, now }) => {
    const b = (req.body ?? {}) as Record<string, unknown>;
    const protocol = String(b.protocol ?? "");
    if (!["oidc", "saml", "oauth2"].includes(protocol) || !String(b.name ?? "").trim()) return stubErr(422, "invalid_provider", { detail: "name and protocol are required" });
    const preset = String(b.preset ?? "generic");
    const issuer = preset === "google" ? "https://accounts.google.com" : preset === "github" ? "https://github.com"
      : preset === "microsoft" ? `https://login.microsoftonline.com/${String(b.tenant ?? "")}/v2.0` : String(b.issuer ?? "");
    if (!issuer || (preset === "microsoft" && !b.tenant)) return stubErr(422, "invalid_provider", { detail: preset === "microsoft" ? "the Microsoft preset needs your tenant id" : "issuer is required" });
    if (s.providers.some((p) => p.issuer?.toLowerCase() === issuer.toLowerCase())) return stubErr(409, "issuer_in_use");
    // The id the Add sheet reserved (so the redirect URL was shown first), like the real route.
    if (b.id !== undefined && !/^idp_[a-z2-7]{26}$/.test(String(b.id))) return stubErr(422, "invalid_provider", { detail: "bad id" });
    if (b.id && s.providers.some((p) => p.id === b.id)) return stubErr(409, "provider_id_taken");
    const p = {
      id: b.id ? String(b.id) : `idp_${rid()}`, name: String(b.name).trim(), protocol: protocol as "oidc", preset: preset as "generic", issuer,
      client_id: protocol === "saml" ? null : String(b.client_id ?? "") || null, saml_sso_url: protocol === "saml" ? String(b.saml_sso_url ?? "") : null,
      has_client_secret: Boolean(b.client_secret), scopes: Array.isArray(b.scopes) ? b.scopes.map(String) : [], allowed_algs: ["RS256", "ES256", "EdDSA"],
      saml_sp_entity_id: (b.saml_sp_entity_id as string) ?? null, saml_certificate_count: Array.isArray(b.saml_certificates) ? b.saml_certificates.length : 0,
      domains: Array.isArray(b.domains) ? b.domains.map(String) : [], jit_mode: b.jit_mode === "domain" ? "domain" as const : "off" as const,
      trust_idp_mfa: Boolean(b.trust_idp_mfa), enforce_sso: Boolean(b.enforce_sso), groups_claim: (b.groups_claim as string) ?? null, status: "active" as const,
    };
    s.providers.push(p);
    stubAudit(s, now, { event_type: "sso.provider.created", what: p.name, headline: "A sign-in provider was added" }, who);
    return stubOk(p, 201);
  });

  on("GET", "/v1/identity/providers/([^/]+)", "identity_provider:read", ({ s, m }) => {
    const p = s.providers.find((x) => x.id === m[1]);
    if (!p) return stubNotFound();
    const raw = (p as unknown as { group_mappings?: Record<string, string> }).group_mappings ?? {};
    const groups = F.groups(s.t0, s.people);
    return stubOk({
      ...p, group_mappings: Object.entries(raw).map(([external_group, group_id]) => ({ external_group, group_id, group_name: groups.find((g) => g.id === group_id)?.name ?? null })),
      scim_keys: keysOf(p.id), client_auth: (p as unknown as { client_auth?: string }).client_auth ?? "client_secret",
    });
  });

  on("DELETE", "/v1/identity/providers/([^/]+)", "identity_provider:manage", ({ s, m, who, now }) => {
    const p = s.providers.find((x) => x.id === m[1]);
    if (!p) return stubNotFound();
    p.status = "disabled";
    for (const k of keysOf(p.id)) k.status = "revoked";
    stubAudit(s, now, { event_type: "sso.provider.deleted", what: p.name, headline: "A sign-in provider was removed" }, who);
    return stubOk({ deleted: true });
  });

  on("POST", "/v1/identity/providers/([^/]+)/scim-keys", "identity_provider:manage", ({ s, m, req, now }) => {
    const p = s.providers.find((x) => x.id === m[1]);
    if (!p) return stubNotFound();
    const b = (req.body ?? {}) as { name?: unknown; expires_in_days?: unknown };
    const days = typeof b.expires_in_days === "number" ? b.expires_in_days : null;
    const k: Key = { id: `key_${rid(6)}`, name: String(b.name ?? "SCIM"), status: "active", created_at: new Date(now).toISOString(),
      expires_at: days ? new Date(now + days * 86_400_000).toISOString() : null, last_used_at: null };
    keysOf(p.id).unshift(k);
    return stubOk({ id: k.id, key: `boss_scim_${rid(12)}${rid(12)}`, principal_id: `sys_scim_${p.id.split("_").slice(1).join("_")}`, shown_once: true, expires_at: k.expires_at }, 201);
  });

  on("DELETE", "/v1/identity/providers/([^/]+)/scim-keys/([^/]+)", "identity_provider:manage", ({ m }) => {
    const k = keysOf(m[1]).find((x) => x.id === m[2]);
    if (!k) return stubNotFound();
    k.status = "revoked";
    return stubOk({ revoked: true });
  });
}
