/**
 * The provider form's model, pure: which kinds there are, what each asks for, and the exact body
 * POST /v1/identity/providers or PATCH …/{id} accepts (identity_sso.py ProviderBody /
 * ProviderPatch are allowlists: an unknown field is a 422, so nothing else is ever sent).
 * Tested in tests/identity-sso-providers.test.tsx.
 */
import type { Provider } from "../../../lib/identity/types";
import type { ClientAuth, ProviderDetail } from "../../../lib/identity/types-sso";

export type ProviderKind = "google" | "microsoft" | "okta" | "keycloak" | "github" | "saml" | "generic";

export const KINDS: { kind: ProviderKind; label: string; tip: string }[] = [
  { kind: "google", label: "Google", tip: "Google Workspace accounts" },
  { kind: "microsoft", label: "Microsoft", tip: "Microsoft Entra ID (Azure AD): needs your tenant id" },
  { kind: "okta", label: "Okta", tip: "Your Okta org URL, or an authorization server" },
  { kind: "keycloak", label: "Keycloak", tip: "A Keycloak realm's URL" },
  { kind: "github", label: "GitHub", tip: "GitHub accounts (an OAuth app)" },
  { kind: "saml", label: "SAML", tip: "Any SAML 2.0 identity provider" },
  { kind: "generic", label: "Other OIDC", tip: "Any OpenID Connect provider" },
];

export type ProviderForm = {
  kind: ProviderKind;
  name: string;
  issuer: string;
  tenant: string;
  client_id: string;
  /** Write-only: blank means "keep the stored one". */
  client_secret: string;
  client_auth: ClientAuth;
  scopes: string;
  domains: string;
  jit: boolean;
  trust_mfa: boolean;
  enforce_sso: boolean;
  groups_claim: string;
  saml_sso_url: string;
  /** A new signing certificate (PEM). Blank on edit keeps the stored ones. */
  saml_certificate: string;
  saml_sp_entity_id: string;
  /** Link an existing person with the same verified email in `domains` (off by default). */
  link_verified: boolean;
  /** SAML: accept sign-ins the IdP starts (a dashboard tile; off by default). */
  idp_initiated: boolean;
  /** SCIM may manage, and deprovision, people BOSS already has in `domains` (off by default). */
  scim_adopt: boolean;
};

export function kindOf(p: Pick<Provider, "protocol" | "preset">): ProviderKind {
  if (p.protocol === "saml") return "saml";
  if (p.protocol === "oauth2" || p.preset === "github") return "github";
  if (p.preset === "google" || p.preset === "microsoft" || p.preset === "okta" || p.preset === "keycloak") return p.preset;
  return "generic";
}

export function protocolOf(kind: ProviderKind): Provider["protocol"] {
  return kind === "saml" ? "saml" : kind === "github" ? "oauth2" : "oidc";
}

/** Does this kind ask for the issuer URL (the others derive it)? */
export function needsIssuer(kind: ProviderKind): boolean {
  return kind === "okta" || kind === "keycloak" || kind === "generic" || kind === "saml";
}

const DEFAULT_NAME: Record<ProviderKind, string> = {
  google: "Google", microsoft: "Microsoft", okta: "Okta", keycloak: "Keycloak", github: "GitHub", saml: "Company SSO", generic: "Company SSO",
};

/**
 * OKTA'S DEFAULTS (identity_sso.py _preset_defaults, sso_oidc.okta_default_scopes): the org
 * authorization server sends groups for the `groups` scope; a custom one (/oauth2/{id}) refuses an
 * unknown scope, so it asks for none and the admin adds a `groups` claim there instead.
 */
export function oktaScopes(issuer: string): string {
  return /^https:\/\/[^/]+\/oauth2\/[A-Za-z0-9]+\/?$/.test(issuer.trim()) ? "openid email profile" : "openid email profile groups";
}

export function emptyForm(kind: ProviderKind): ProviderForm {
  return {
    kind, name: DEFAULT_NAME[kind], issuer: "", tenant: "", client_id: "", client_secret: "", client_auth: "client_secret",
    scopes: kind === "okta" ? oktaScopes("") : protocolOf(kind) === "oidc" ? "openid email profile" : "", domains: "", jit: false, trust_mfa: false, enforce_sso: false,
    groups_claim: kind === "okta" ? "groups" : "", saml_sso_url: "", saml_certificate: "", saml_sp_entity_id: "",
    link_verified: false, idp_initiated: false, scim_adopt: false,
  };
}

/** The issuer as typed, and Okta's scopes following it while the admin has not changed them. */
export function withIssuer(f: ProviderForm, issuer: string): ProviderForm {
  if (f.kind !== "okta" || f.scopes !== oktaScopes(f.issuer)) return { ...f, issuer };
  return { ...f, issuer, scopes: oktaScopes(issuer) };
}

/** Okta's issuer, checked the way the API checks it ("" when fine). */
export function oktaIssuerProblem(issuer: string): string {
  const v = issuer.trim();
  if (!v) return "";
  if (/^https:\/\/[^/.]+-admin\./i.test(v)) return "Use your Okta URL without -admin";
  if (!/^https:\/\/[^/?#]+(\/oauth2\/[A-Za-z0-9]+)?\/?$/.test(v)) return "Use https://you.okta.com or …/oauth2/default";
  return "";
}

export function formOf(p: ProviderDetail): ProviderForm {
  return {
    kind: kindOf(p), name: p.name, issuer: p.issuer ?? "", tenant: "", client_id: p.client_id ?? "", client_secret: "",
    client_auth: p.client_auth ?? "client_secret", scopes: p.scopes.join(" "), domains: p.domains.join(", "),
    jit: p.jit_mode === "domain", trust_mfa: p.trust_idp_mfa, enforce_sso: p.enforce_sso, groups_claim: p.groups_claim ?? "",
    saml_sso_url: p.saml_sso_url ?? "", saml_certificate: "", saml_sp_entity_id: p.saml_sp_entity_id ?? "",
    link_verified: p.link_verified_domain_accounts === true, idp_initiated: p.allow_idp_initiated === true,
    scim_adopt: p.scim_adopt_existing_by_domain === true,
  };
}

const list = (text: string, sep: RegExp) => text.split(sep).map((x) => x.trim()).filter(Boolean);
export const domainList = (text: string) => list(text, /[\s,;]+/).map((d) => d.toLowerCase().replace(/^@/, ""));

/** What is still missing before Add can be pressed ("" when ready). */
export function missing(f: ProviderForm, editing = false): string {
  if (!f.name.trim()) return "Add a name";
  if (!editing && f.kind === "microsoft" && !f.tenant.trim()) return "Add your tenant id";
  if (!editing && needsIssuer(f.kind) && !/^https?:\/\/\S+$/.test(f.issuer.trim()) && f.kind !== "saml") return "Add the issuer URL";
  if (!editing && f.kind === "saml" && !f.issuer.trim()) return "Add the IdP entity ID";
  if (!editing && f.kind === "okta" && oktaIssuerProblem(f.issuer)) return oktaIssuerProblem(f.issuer);
  if (f.kind === "saml") {
    if (!/^https:\/\/\S+$/.test(f.saml_sso_url.trim())) return "Add the https sign-in URL";
    if (!editing && !f.saml_certificate.trim()) return "Paste the signing certificate";
  } else {
    if (!f.client_id.trim()) return "Add the client ID";
    if (!editing && f.client_auth === "client_secret" && !f.client_secret) return "Add the client secret";
  }
  if (f.enforce_sso && !domainList(f.domains).length) return "Requiring SSO needs a domain";
  if ((f.link_verified || f.scim_adopt) && !domainList(f.domains).length) return "Linking existing people needs a domain";
  return "";
}

/** The POST body for a new provider. */
/**
 * THE PROVIDER'S ID, RESERVED WHEN THE ADD SHEET OPENS. The redirect URL names it
 * (/v1/auth/sso/{id}/callback), and Google asks for that URL before it hands out the client id and
 * secret this form needs — so the id has to exist before Save. Same shape as the server's own
 * (ids.new_id: idp_ + 26 base32); the create route accepts it, and a taken one is 409.
 */
export function reserveProviderId(random: (n: number) => Uint8Array = (n) => crypto.getRandomValues(new Uint8Array(n))): string {
  const alphabet = "abcdefghijklmnopqrstuvwxyz234567";
  return "idp_" + [...random(26)].map((b) => alphabet[b % 32]).join("");
}

export function createBody(f: ProviderForm, reservedId?: string | null): Record<string, unknown> {
  const protocol = protocolOf(f.kind);
  const body: Record<string, unknown> = {
    ...(reservedId ? { id: reservedId } : {}),
    protocol, preset: f.kind === "saml" ? "generic" : f.kind, name: f.name.trim(),
    domains: domainList(f.domains), jit_mode: f.jit ? "domain" : "off", trust_idp_mfa: f.trust_mfa, enforce_sso: f.enforce_sso,
  };
  if (f.groups_claim.trim()) body.groups_claim = f.groups_claim.trim();
  // The opt-ins are sent only when on, so an older API never sees the fields.
  if (f.link_verified) body.link_verified_domain_accounts = true;
  if (f.scim_adopt) body.scim_adopt_existing_by_domain = true;
  if (f.idp_initiated && protocol === "saml") body.allow_idp_initiated = true;
  if (needsIssuer(f.kind)) body.issuer = f.issuer.trim();
  if (f.kind === "microsoft") body.tenant = f.tenant.trim();
  if (protocol === "saml") {
    body.saml_sso_url = f.saml_sso_url.trim();
    body.saml_certificates = [f.saml_certificate.trim()];
    if (f.saml_sp_entity_id.trim()) body.saml_sp_entity_id = f.saml_sp_entity_id.trim();
  } else {
    body.client_id = f.client_id.trim();
    if (f.client_secret) body.client_secret = f.client_secret;
    // Additive on FX-B's API; sent only when chosen, so an older API never sees the field.
    if (f.client_auth === "private_key_jwt") body.client_auth = "private_key_jwt";
    if (protocol === "oidc") body.scopes = list(f.scopes, /[\s,]+/);
  }
  return body;
}

/** The PATCH body: only what changed (an empty object means nothing to save). */
export function patchBody(p: ProviderDetail, f: ProviderForm): Record<string, unknown> {
  const was = formOf(p);
  const out: Record<string, unknown> = {};
  if (f.name.trim() !== was.name) out.name = f.name.trim();
  const d = domainList(f.domains);
  if (d.join() !== p.domains.join()) out.domains = d;
  if (f.jit !== was.jit) out.jit_mode = f.jit ? "domain" : "off";
  if (f.trust_mfa !== was.trust_mfa) out.trust_idp_mfa = f.trust_mfa;
  if (f.enforce_sso !== was.enforce_sso) out.enforce_sso = f.enforce_sso;
  if (f.groups_claim.trim() !== was.groups_claim) out.groups_claim = f.groups_claim.trim();
  if (f.link_verified !== was.link_verified) out.link_verified_domain_accounts = f.link_verified;
  if (f.scim_adopt !== was.scim_adopt) out.scim_adopt_existing_by_domain = f.scim_adopt;
  if (p.protocol === "saml" && f.idp_initiated !== was.idp_initiated) out.allow_idp_initiated = f.idp_initiated;
  if (p.protocol === "saml") {
    if (f.saml_sso_url.trim() !== was.saml_sso_url) out.saml_sso_url = f.saml_sso_url.trim();
    if (f.saml_certificate.trim()) out.saml_certificates = [f.saml_certificate.trim()];
  } else {
    if (f.client_id.trim() !== was.client_id) out.client_id = f.client_id.trim();
    if (f.client_secret) out.client_secret = f.client_secret;
    if (f.client_auth !== was.client_auth) out.client_auth = f.client_auth;
    const s = list(f.scopes, /[\s,]+/);
    if (p.protocol === "oidc" && s.join(" ") !== p.scopes.join(" ")) out.scopes = s;
  }
  return out;
}

/** A provider refusal in plain words (the API's codes, never shown raw). */
export function providerRefusal(code: string, detail: string | null): string {
  if (code === "issuer_in_use") return "Another provider already uses this issuer";
  if (code === "invalid_provider") {
    const d = (detail ?? "").toLowerCase();
    if (d.includes("tenant")) return "Add your Microsoft tenant id";
    if (d.includes("-admin")) return "Use your Okta URL without -admin";
    if (d.includes("okta")) return "Use https://you.okta.com or …/oauth2/default";
    if (d.includes("saml")) return "SAML needs an https sign-in URL and a certificate";
    if (d.includes("issuer")) return "Check the issuer URL";
    if (d.includes("group")) return "A mapped group no longer exists";
    if (d.includes("alg")) return "Choose signing algorithms BOSS allows";
    if (d.includes("https")) return "The sign-in URL must start with https://";
    return "Check the fields";
  }
  if (code === "public_origin_unset") return "Set the public URL first";
  return "";
}

/** POST …/test answers {ok:false, error:<sso code>}: what to check, in a few words. */
export function testFailureWords(code: string | null | undefined): string {
  switch (code) {
    case "discovery_invalid": case "discovery_refused": return "Can't read the issuer · check the URL";
    case "jwks_invalid": case "jwks_refused": return "Can't read its signing keys";
    case "issuer_mismatch": return "The issuer doesn't match";
    case "provider_misconfigured": return "Settings incomplete";
    case "alg_not_allowed": return "Unsupported signing algorithm";
    default: return "Test failed · check the settings";
  }
}

/** OIDC kinds whose app registration also takes a sign-in tile, sign-out and back-channel URL. */
const FULL_OIDC = new Set<string>(["okta", "keycloak", "generic"]);

/** Where the admin's IdP sends people back, and what it calls BOSS. Null without a public URL. */
export function idpUrls(origin: string | null | undefined, p: { id: string | null; protocol: Provider["protocol"]; saml_sp_entity_id?: string | null; saved?: boolean; kind?: string }):
  { label: string; value: string; tip: string }[] | null {
  if (!origin) return null;
  const o = origin.replace(/\/+$/, "");
  if (p.protocol === "saml") {
    return [
      { label: "Reply (ACS) URL", value: `${o}/saml/v2/acs`, tip: "Where your IdP posts the sign-in answer" },
      { label: "Entity ID", value: p.saml_sp_entity_id || `${o}/saml/v2/metadata`, tip: "How your IdP names BOSS (the audience)" },
      ...(p.id && p.saved !== false ? [{ label: "Metadata", value: `${o}/saml/v2/metadata?provider_id=${encodeURIComponent(p.id)}`, tip: "Import this in your IdP instead of typing" }] : []),
    ];
  }
  if (!p.id) return null;
  const base = `${o}/v1/auth/sso/${encodeURIComponent(p.id)}`;
  const urls = [{ label: "Redirect URL", value: `${base}/callback`, tip: "Paste into your IdP's allowed redirect URLs" }];
  if (p.protocol === "oidc" && p.kind && FULL_OIDC.has(p.kind)) {
    urls.push(
      { label: "Initiate login URI", value: `${base}/start`, tip: "Lets the IdP's dashboard tile start a sign-in" },
      { label: "Sign-out redirect URI", value: `${o}/login`, tip: "Where the IdP returns people after signing out" },
      { label: "Back-channel logout URI", value: `${base}/backchannel-logout`, tip: "The IdP ends BOSS sessions here when people sign out" },
    );
  }
  return urls;
}
