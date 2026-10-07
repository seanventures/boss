/**
 * THE IDENTITY STUB API — a stand-in for streams S1, S2, S3 and S5 while they build.
 *
 * It answers the routes of IDENTITY.md §10 (plus the seams lib/identity/types.ts names) with the
 * fixture org, in process, with the real error contract: 401 unauthenticated, 401
 * step_up_required, 403 {"error":"forbidden","permission":…}, 403 csrf_failed, 404, 409. It
 * decides with the generated catalogue, exactly as the PDP will, so the console's mirroring is
 * exercised against refusals, not just happy paths.
 *
 * NEVER IN PRODUCTION BY ACCIDENT: it answers only when BOSS_IDENTITY_STUB is set to the
 * deliberate value in STUB_ENABLE_VALUE (lib/identity/stub/enabled.ts), and every page then
 * carries a "Stub data" banner. tests/identity-stub-is-never-shipped.test.ts checks that no
 * Dockerfile, compose file or installer sets it.
 *
 * Retired route by route as S1–S5 land: delete a handler here when its real route exists.
 */
import { PERMISSION_META, isPermission, type Permission } from "../permissions.gen";
import type { MePermissionsResponse, MeResponse } from "../contract";
import { CATALOGUE_FINGERPRINT, PERMISSIONS } from "../permissions.gen";
import type {
  AccessRole, ApiKey, AuditEvent, AuthOptions, Binding, CascadePreview, ChainHop, Delegation, DelegationDetail,
  EffectiveAccess, EffectiveEntry, IssuedSecret, Person, Posture, RolePreview, ScopeLabel, ServiceAccount,
} from "../types";
import * as F from "./fixtures";
import { registerPeopleRoutes } from "./routes-people";
import { registerRoleRoutes } from "./routes-roles";
import { registerAgentRoutes } from "./routes-agents";
import { registerAuthRoutes } from "./routes-auth";
import { registerSsoRoutes } from "./routes-sso";
import { bindGroup, groupsOf, registerGroupRoutes, unbindGroup } from "./routes-groups";
import { registerAccountRoutes } from "./routes-account";

export type StubRequest = {
  method: string;
  path: string;
  query: URLSearchParams;
  body: unknown;
  cookies: Record<string, string>;
  headers: Headers;
};
export type StubResponse = { status: number; body: unknown; setCookies?: string[] };

export const SESSION_COOKIE = "__Host-boss_session";
export const PREAUTH_COOKIE = "__Host-boss_preauth";
/** Dev only: which first-run claim the stub pretends is pending ("setup_token" | "legacy_password" | "network_bridge"). */
export const STUB_CLAIM_COOKIE = "boss_stub_claim";

type Sess = { principal: string; csrf: string; freshUntil: number; methods: string[]; created: number };
type Pre = { email: string; principal: string | null; kind: "pwd" | "email_code" | "magic" };

type State = {
  t0: number;
  people: Person[];
  roles: AccessRole[];
  sas: ServiceAccount[];
  personal: Record<string, ApiKey[]>;
  delegations: Delegation[];
  passports: ReturnType<typeof F.passports>;
  audit: AuditEvent[];
  sessions: ReturnType<typeof F.sessions>;
  factors: ReturnType<typeof F.factors>;
  providers: ReturnType<typeof F.providers>;
  policy: ReturnType<typeof F.policy>;
  live: Map<string, Sess>;
  pre: Map<string, Pre>;
  claimed: boolean;
  epoch: number;
  verifiedAt: number;
  seq: number;
};

function fresh(now: number): State {
  const people = F.people(now);
  return {
    t0: now, people, roles: F.roles(now), sas: F.serviceAccounts(now), personal: F.personalKeys(now),
    delegations: F.delegations(now), passports: F.passports(now), audit: F.auditEvents(now), sessions: F.sessions(now),
    factors: F.factors(now), providers: F.providers(now), policy: F.policy(), live: new Map(), pre: new Map(),
    claimed: true, epoch: 41, verifiedAt: now - 4 * 60_000, seq: 48_211,
  };
}

export function stubState(): State {
  const g = globalThis as { __bossIdentityStub?: State };
  if (!g.__bossIdentityStub) g.__bossIdentityStub = fresh(Date.now());
  return g.__bossIdentityStub;
}

/** Tests start from the fixture again. */
export function resetStub(now = Date.now()): void {
  (globalThis as { __bossIdentityStub?: State }).__bossIdentityStub = fresh(now);
}

// ---------------------------------------------------------------------------------------------
// decisions, as the PDP makes them

function rolePerms(s: State, key: string): Permission[] {
  return F.permissionsOfRole(key, s.roles);
}

function bindingsOf(s: State, principal: string): Binding[] {
  const person = s.people.find((p) => p.id === principal);
  if (person) return person.bindings;
  return s.sas.find((x) => x.id === principal)?.bindings ?? [];
}

/** Effective permissions at a scope (A3: an org-scoped key takes effect only from an org binding). */
export function effective(s: State, principal: string, scope: ScopeLabel): Map<Permission, Binding[]> {
  const out = new Map<Permission, Binding[]>();
  for (const b of bindingsOf(s, principal)) {
    if (b.scope !== "org" && b.scope !== scope) continue;
    for (const p of rolePerms(s, b.role_key)) {
      if (b.scope !== "org" && PERMISSION_META[p].scope === "org") continue;
      out.set(p, [...(out.get(p) ?? []), b]);
    }
  }
  return out;
}

function holds(s: State, principal: string, p: Permission, scope: ScopeLabel = "org"): boolean {
  return effective(s, principal, scope).has(p);
}

// ---------------------------------------------------------------------------------------------

const ok = (body: unknown, status = 200, setCookies?: string[]): StubResponse => ({ status, body, setCookies });
const err = (status: number, error: string, extra: Record<string, unknown> = {}): StubResponse => ({ status, body: { error, ...extra } });
const notFound = () => err(404, "not_found");

function token(prefix: string): string {
  const a = new Uint8Array(24);
  globalThis.crypto.getRandomValues(a);
  return prefix + Array.from(a, (b) => b.toString(36).padStart(2, "0")).join("").slice(0, 32);
}

function cookie(name: string, value: string, maxAge: number): string {
  return `${name}=${value}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${maxAge}`;
}

function startSession(s: State, principal: string, methods: string[], now: number): StubResponse["setCookies"] {
  const t = token("bss_");
  s.live.set(t, { principal, csrf: token("csrf_"), freshUntil: methods.some((m) => m !== "pwd") ? now + 600_000 : 0, methods, created: now });
  return [cookie(SESSION_COOKIE, t, 7 * 86_400), cookie(PREAUTH_COOKIE, "", 0)];
}

function personByEmail(s: State, email: string): Person | undefined {
  return s.people.find((p) => p.email.toLowerCase() === email.trim().toLowerCase());
}

function body<T extends Record<string, unknown>>(req: StubRequest): Partial<T> {
  return (req.body && typeof req.body === "object" ? req.body : {}) as Partial<T>;
}

function scopeOf(q: URLSearchParams): ScopeLabel {
  const p = q.get("project_id");
  return p ? (`project:${p}` as ScopeLabel) : "org";
}

function audit(s: State, now: number, e: Partial<AuditEvent> & Pick<AuditEvent, "event_type" | "what" | "headline">, actor: string): void {
  const who = s.people.find((p) => p.id === actor);
  s.seq += 1;
  s.audit.unshift({
    id: `sev_${s.seq}`, seq: s.seq, at: new Date(now).toISOString(), outcome: "ok", source: "console",
    actor: { id: actor, kind: "user", name: who?.display_name ?? actor, hue: who?.hue ?? 7 }, on_behalf_of: null, chain: [],
    grant_id: null, token_jti: null, run_id: null, why: [], raw: { event_type: e.event_type, actor_principal_id: actor },
    prev_hash: F.hashLike(s.seq - 1), hash: F.hashLike(s.seq), ...e,
  });
  s.epoch += 1;
}

// ---------------------------------------------------------------------------------------------

type Ctx = { s: State; req: StubRequest; now: number; m: RegExpMatchArray; who: string; sess: Sess };
type Handler = (c: Ctx) => StubResponse;
type Route = {
  method: string;
  pattern: RegExp;
  /** A catalogue key, "self" (any signed-in person) or "public". */
  needs: Permission | "self" | "public";
  run: Handler;
};

const R: Route[] = [];
const on = (method: string, pattern: string, needs: Route["needs"], run: Handler) =>
  R.push({ method, pattern: new RegExp(`^${pattern}$`), needs, run });

/** For the per-area stub files (stub/routes-*.ts): register one more route. */
export const stubRoute = on;
export type StubOn = typeof on;
export type { Ctx as StubCtx };
export { ok as stubOk, err as stubErr, notFound as stubNotFound, effective as stubEffective, audit as stubAudit, cascade as stubCascade };

// ---- sign-in (public) ------------------------------------------------------------------------

on("GET", "/v1/auth/options", "public", ({ s, req }) => {
  const path: string | undefined = req.cookies[STUB_CLAIM_COOKIE];
  const claim: AuthOptions["claim"] = path && ["setup_token", "legacy_password", "network_bridge"].includes(path)
    ? { path: path as "setup_token", origin: path === "network_bridge" ? `https://goldeneye.tail4b2c.ts.net` : null,
        expires_at: path === "setup_token" ? new Date(Date.now() + 52 * 60_000).toISOString() : null,
        seats: ["ceo", "cto", "cao", "vp_agents", "release_manager"] }
    : null;
  const bare = req.cookies.boss_stub_bare === "1";
  const options: AuthOptions = {
    org_name: F.ORG.name, host: F.HOST,
    providers: bare ? [] : s.providers.map((p) => ({ id: p.id, name: p.name, preset: p.preset })),
    smtp: bare ? false : s.policy.smtp, signup: !bare && (s.policy.signup.enabled || req.cookies.boss_stub_signup === "1"),
    legacy_password: false, claim,
  };
  return ok({ ...options, password_min_length: s.policy.password_min_length });
});

on("POST", "/v1/auth/login", "public", ({ s, req, now }) => {
  const { email = "", password = "" } = body<{ email: string; password: string }>(req);
  const person = personByEmail(s, String(email));
  if (!person || !password || password === "wrong" || person.status !== "active") return err(401, "invalid_credentials");
  const methods = [...(person.sign_in.passkeys ? ["passkey" as const] : []), ...(person.sign_in.totp ? ["totp" as const] : [])];
  const who = { display_name: person.display_name, email: person.email, hue: person.hue };
  if (methods.length) {
    const t = token("pre_");
    s.pre.set(t, { email: person.email, principal: person.id, kind: "pwd" });
    return ok({ next: "mfa", methods: [...methods, "recovery"], who }, 200, [cookie(PREAUTH_COOKIE, t, 600)]);
  }
  return ok({ next: "done", methods: [], who }, 200, startSession(s, person.id, ["pwd"], now));
});

function preauth(s: State, req: StubRequest): Pre | undefined {
  return s.pre.get(req.cookies[PREAUTH_COOKIE] ?? "");
}

on("POST", "/v1/auth/mfa/totp", "public", ({ s, req, now }) => {
  const pre = preauth(s, req);
  if (!pre?.principal) return err(401, "unauthenticated");
  const { code = "" } = body<{ code: string }>(req);
  if (!/^\d{6}$/.test(String(code)) || code === "000000") return err(401, "invalid_code", { attempts_left: 4 });
  return ok({ next: "done", methods: [], who: null }, 200, startSession(s, pre.principal, ["pwd", "totp"], now));
});

on("POST", "/v1/auth/mfa/recovery", "public", ({ s, req, now }) => {
  const pre = preauth(s, req);
  if (!pre?.principal) return err(401, "unauthenticated");
  const { code = "" } = body<{ code: string }>(req);
  if (String(code).replace(/[\s-]/g, "").length !== 12) return err(401, "invalid_code", { attempts_left: 4 });
  return ok({ next: "done", methods: [], who: null, codes_left: 7 }, 200, startSession(s, pre.principal, ["pwd", "recovery_code"], now));
});

on("POST", "/v1/auth/webauthn/options", "public", ({ req }) => {
  const { purpose = "login" } = body<{ purpose: string }>(req);
  return ok({ stub: true, purpose, challenge: "c3R1Yi1jaGFsbGVuZ2U", rpId: F.HOST, timeout: 60_000, userVerification: "required", allowCredentials: [] });
});

on("POST", "/v1/auth/webauthn/verify", "public", ({ s, req, now }) => {
  const { purpose = "login" } = body<{ purpose: string }>(req);
  const live = s.live.get(req.cookies[SESSION_COOKIE] ?? "");
  if (purpose === "step_up") {
    if (!live) return err(401, "unauthenticated");
    live.freshUntil = now + 600_000;
    return ok({ next: "done", methods: [], who: null, step_up_fresh_until: new Date(live.freshUntil).toISOString() });
  }
  const pre = preauth(s, req);
  return ok({ next: "done", methods: [], who: null }, 200, startSession(s, pre?.principal ?? "usr_param", ["passkey"], now));
});

for (const kind of ["magic-link", "email-code"] as const) {
  on("POST", `/v1/auth/${kind}`, "public", ({ s, req }) => {
    if (!s.policy.smtp) return notFound();
    const { email = "" } = body<{ email: string }>(req);
    const t = token("pre_");
    s.pre.set(t, { email: String(email), principal: personByEmail(s, String(email))?.id ?? null, kind: kind === "email-code" ? "email_code" : "magic" });
    return ok({ sent: true }, 202, [cookie(PREAUTH_COOKIE, t, 600)]);
  });
}

on("POST", "/v1/auth/email-code/verify", "public", ({ s, req, now }) => {
  const pre = preauth(s, req);
  const { code = "" } = body<{ code: string }>(req);
  if (!pre || !pre.principal || !/^\d{6}$/.test(String(code)) || code === "000000") return err(401, "invalid_code", { attempts_left: 4 });
  return ok({ next: "done", methods: [], who: null }, 200, startSession(s, pre.principal, ["email_code"], now));
});

on("POST", "/v1/auth/magic-link/verify", "public", ({ s, req, now }) => {
  const pre = preauth(s, req);
  const { token: t = "" } = body<{ token: string }>(req);
  if (!pre || !pre.principal || !t) return err(401, "link_invalid");
  return ok({ next: "done", methods: [], who: null }, 200, startSession(s, pre.principal, ["magic_link"], now));
});

on("POST", "/v1/auth/signup", "public", ({ s, req }) =>
  (s.policy.signup.enabled || req.cookies.boss_stub_signup === "1") ? ok({ sent: true }, 202) : notFound());
on("POST", "/v1/auth/password/forgot", "public", () => ok({ sent: true }, 202));
on("POST", "/v1/auth/password/reset", "public", ({ req }) => {
  const { new_password = "" } = body<{ new_password: string }>(req);
  return String(new_password).length < 12 ? err(422, "password_too_short") : ok({ next: "mfa", methods: ["passkey", "totp"], who: null });
});

on("POST", "/v1/auth/setup/claim", "public", ({ s, req, now }) => {
  const claim = req.cookies[STUB_CLAIM_COOKIE];
  if (!claim) return notFound();
  const { name = "", email = "" } = body<{ name: string; email: string }>(req);
  if (!String(name).trim() || !String(email).includes("@")) return err(422, "invalid_fields");
  const param = s.people.find((p) => p.id === "usr_param")!;
  return ok({ next: "enrol", methods: ["passkey", "totp"], who: { display_name: param.display_name, email: param.email, hue: param.hue } },
    200, [...(startSession(s, "usr_param", ["setup"], now) ?? []), `${STUB_CLAIM_COOKIE}=; Path=/; Max-Age=0`]);
});

// As S1 answers them: the token in the BODY, never the URL.
const inviteToken = (req: StubRequest) => String((req.body as { token?: unknown } | null)?.token ?? "");
on("POST", "/v1/auth/invitations/preview", "public", ({ req }) => (!inviteToken(req) || inviteToken(req) === "expired" ? notFound() : ok({
  org: F.ORG.name, inviter: "Priya Raman", email: "ana@acme.dev", roles: ["Operator · acme-web"], expires_at: new Date(Date.now() + 5 * 86_400_000).toISOString(),
  sso: [{ id: "idp_okta", name: "Acme SSO", preset: "okta" }],
})));
on("POST", "/v1/auth/invitations/accept", "public", ({ s, now, req }) => {
  if (!inviteToken(req) || inviteToken(req) === "expired") return err(400, "invitation_invalid");
  const ana = s.people.find((p) => p.id === "usr_ana");
  if (ana) ana.status = "active";
  return ok({ next: "enrol", methods: ["passkey", "totp"], who: null }, 200, startSession(s, "usr_ana", ["pwd"], now));
});

on("POST", "/v1/auth/step-up", "self", ({ sess, req, now }) => {
  const { code = "" } = body<{ code: string; method: string }>(req);
  if (!/^\d{6}$/.test(String(code)) || code === "000000") return err(401, "invalid_code", { attempts_left: 4 });
  sess.freshUntil = now + 600_000;
  return ok({ step_up_fresh_until: new Date(sess.freshUntil).toISOString() });
});

on("POST", "/v1/auth/logout", "self", ({ s, req }) => {
  s.live.delete(req.cookies[SESSION_COOKIE] ?? "");
  return ok({ ok: true }, 200, [cookie(SESSION_COOKIE, "", 0)]);
});

// ---- me --------------------------------------------------------------------------------------

function meOf(s: State, who: string, sess: Sess): MeResponse {
  const p = s.people.find((x) => x.id === who)!;
  return {
    principal_id: p.id, kind: "user", display_name: p.display_name, email: p.email, org: F.ORG, status: p.status,
    access_roles: p.bindings.filter((b) => b.scope === "org").map((b) => b.role_key),
    sign_in: { password: p.sign_in.password, totp: p.sign_in.totp, passkeys: p.sign_in.passkeys, recovery_codes_remaining: 8, sso_links: p.sign_in.sso ? [p.sign_in.sso] : [] },
    mfa_required: p.bindings.some((b) => ["owner", "admin", "auditor"].includes(b.role_key)),
    mfa_at: sess.freshUntil ? new Date(sess.freshUntil - 600_000).toISOString() : null,
    session_id: `ses_${sess.created}`, session_expires_at: new Date(sess.created + 7 * 86_400_000).toISOString(),
    csrf_token: sess.csrf, epoch: s.epoch, network_bound: false, banners: [],
    // The fixture's colour is the person's chosen one, so the menu matches every list.
    avatar_seed: String(((p.hue % 8) + 8) % 8),
  };
}

on("GET", "/v1/identity/me", "self", ({ s, who, sess }) => ok(meOf(s, who, sess)));
on("GET", "/v1/identity/me/permissions", "self", ({ s, req, who, sess, now }) => {
  const scope = scopeOf(req.query);
  const out: MePermissionsResponse = {
    epoch: s.epoch, scope, permissions: [...effective(s, who, scope).keys()].sort(),
    step_up_fresh_until: sess.freshUntil > now ? new Date(sess.freshUntil).toISOString() : null,
    catalogue_fingerprint: CATALOGUE_FINGERPRINT,
  };
  return ok(out);
});
on("GET", "/v1/identity/me/sessions", "self", ({ s, who }) => ok({ sessions: s.sessions[who] ?? [] }));
on("DELETE", "/v1/identity/me/sessions/([^/]+)", "self", ({ s, who, m }) => {
  s.sessions[who] = (s.sessions[who] ?? []).filter((x) => x.id !== m[1] || x.current);
  return ok({ ok: true });
});
on("GET", "/v1/identity/me/factors", "self", ({ s, who }) => ok({ factors: s.factors[who] ?? [] }));
on("POST", "/v1/identity/me/mfa/totp", "self", () => ok({ otpauth_uri: `otpauth://totp/BOSS:${F.HOST}?secret=JBSWY3DPEHPK3PXP&issuer=BOSS`, secret: "JBSW Y3DP EHPK 3PXP" }));
on("POST", "/v1/identity/me/mfa/totp/confirm", "self", ({ req }) => {
  const { code = "" } = body<{ code: string }>(req);
  return /^\d{6}$/.test(String(code)) && code !== "000000" ? ok({ ok: true }) : err(401, "invalid_code", { attempts_left: 4 });
});
on("POST", "/v1/identity/me/passkeys", "self", ({ s, who, req, now }) => {
  const { name = "This device" } = body<{ name: string }>(req);
  s.factors[who] = [{ id: token("pk_"), kind: "passkey", name: String(name), detail: "Passkey", last_used_at: new Date(now).toISOString(), note: "synced" }, ...(s.factors[who] ?? [])];
  return ok({ ok: true }, 201);
});
on("DELETE", "/v1/identity/me/passkeys/([^/]+)", "self", ({ s, who, m }) => {
  s.factors[who] = (s.factors[who] ?? []).filter((f) => f.id !== m[1]);
  return ok({ ok: true });
});
on("POST", "/v1/identity/me/recovery-codes", "self", () => ok({
  codes: ["7KQ2-MX4P-9TRA", "B3ZV-8WNE-QL2D", "H6YC-2PFK-UJ7S", "N9DE-4RXB-ZK8M", "Q2LW-7HTV-CY5F", "T8MS-3GQN-WD6P", "V5JR-9KCE-BX2H", "X4PA-6ZLU-MF9Q", "Z7GN-2SWD-RT3K", "E1HB-5VQY-PL8C"],
}));
on("POST", "/v1/identity/me/password", "self", ({ req }) => {
  const { new: next = "" } = body<{ current: string; new: string }>(req);
  return String(next).length < 12 ? err(422, "password_too_short") : ok({ ok: true });
});
on("GET", "/v1/identity/me/api-keys", "api_key:manage_own", ({ s, who }) => ok({ keys: s.personal[who] ?? [] }));
on("POST", "/v1/identity/me/api-keys", "api_key:manage_own", ({ s, who, req, now }) => {
  const b = body<{ name: string; scopes: string[]; projects: string[] | null; expires_days: number }>(req);
  const scopes = (b.scopes ?? []).filter(isPermission);
  const mine = effective(s, who, "org");
  const over = scopes.find((p) => !mine.has(p));
  if (over) return err(403, "forbidden", { permission: over });
  const p = s.people.find((x) => x.id === who)!;
  const key: ApiKey = { id: token("key_").slice(0, 10), name: String(b.name || "New key"), owner: { id: who, name: p.display_name, kind: "user" }, scopes,
    projects: b.projects ?? null, networks: null, created_at: new Date(now).toISOString(), last_used_at: null, last_used_ip: null, last_used_client: null,
    expires_at: new Date(now + (Number(b.expires_days) || 90) * 86_400_000).toISOString(), grace_until: null, rotated_from: null, status: "active" };
  s.personal[who] = [key, ...(s.personal[who] ?? [])];
  const issued: IssuedSecret = { key, secret: `boss_pat_${token("").slice(0, 43)}x${token("").slice(0, 5)}`, replaces: null };
  audit(s, now, { event_type: "api_key.created", what: key.id, headline: `${p.display_name} created a key` }, who);
  return ok(issued, 201);
});
on("POST", "/v1/identity/me/api-keys/([^/]+)/revoke", "api_key:manage_own", ({ s, who, m }) => {
  const k = (s.personal[who] ?? []).find((x) => x.id === m[1]);
  if (!k) return notFound();
  k.status = "revoked";
  return ok({ ok: true });
});
on("GET", "/v1/identity/me/delegations", "self", ({ s, who }) => ok({ delegations: s.delegations.filter((d) => d.user.id === who) }));
on("POST", "/v1/identity/me/delegations/preview", "self", ({ s, req, who }) => {
  const b = body<{ agents: string[]; permissions: string[] }>(req);
  const mine = effective(s, who, "org");
  const asked = (b.permissions ?? []).filter(isPermission);
  return ok({
    granted: asked.filter((p) => mine.has(p) && PERMISSION_META[p].delegable),
    dropped: asked.filter((p) => !mine.has(p) || !PERMISSION_META[p].delegable),
  });
});
on("POST", "/v1/identity/me/delegations", "delegation:grant", ({ s, req, who, now }) => {
  const b = body<{ agents: string[]; permissions: string[]; projects: string[]; days: number | null; purpose: string; kind: string }>(req);
  const p = s.people.find((x) => x.id === who)!;
  const mine = effective(s, who, "org");
  const perms = (b.permissions ?? []).filter(isPermission).filter((x) => mine.has(x) && PERMISSION_META[x].delegable);
  const d: Delegation = {
    id: `dlg_${token("").slice(0, 6).toUpperCase()}`, kind: b.kind === "task" ? "task" : "standing", user: { id: who, name: p.display_name.split(" ")[0], hue: p.hue },
    agents: (b.agents ?? []).map((a) => ({ id: a, name: s.passports.find((x) => x.id === a)?.name ?? a })), label: String(b.purpose || "acting for me"),
    permissions: perms, projects: b.projects ?? [], github: null, subdelegation: { allowed: false, to: [] }, max_depth: 3,
    created_at: new Date(now).toISOString(), expires_at: b.kind === "task" ? null : new Date(now + (Number(b.days) || 30) * 86_400_000).toISOString(),
    purpose: b.purpose ?? null, status: "active", last_used_at: null, last_used_run: null,
  };
  s.delegations.unshift(d);
  audit(s, now, { event_type: "delegation.created", what: d.id, headline: `${p.display_name} let agents act for them`, grant_id: d.id }, who);
  return ok(d, 201);
});
on("POST", "/v1/identity/me/delegations/([^/]+)/revoke", "self", ({ s, who, m, now, req }) => {
  const d = s.delegations.find((x) => x.id === m[1] && x.user.id === who);
  if (!d) return notFound();
  if (body<{ dry_run: boolean }>(req).dry_run) return ok(cascade(d));
  d.status = "revoked";
  audit(s, now, { event_type: "delegation.revoked", what: d.id, headline: "A grant was revoked", grant_id: d.id }, who);
  return ok(cascade(d));
});
on("GET", "/v1/identity/me/connections", "self", ({ now }) => ok({ connections: [
  { id: "con_gh1", provider: "GitHub", account: "sam-okafor", scopes: ["repo"], status: "active", linked_at: new Date(now - 20 * 86_400_000).toISOString() },
] }));

// ---- people ----------------------------------------------------------------------------------

on("GET", "/v1/identity/users", "user:read", ({ s, req }) => {
  const q = (req.query.get("q") ?? "").toLowerCase();
  const role = req.query.get("role");
  const status = req.query.get("status");
  const users = s.people.filter((p) => (!q || `${p.display_name} ${p.email}`.toLowerCase().includes(q))
    && (!role || p.bindings.some((b) => b.role_key === role)) && (!status || p.status === status));
  return ok({ users });
});
on("POST", "/v1/identity/users", "user:invite", ({ s, req, who, now }) => {
  const b = body<{ emails: string[]; roles: string[]; groups: string[] }>(req);
  const mine = effective(s, who, "org");
  for (const r of b.roles ?? []) {
    const over = rolePerms(s, r).find((p) => !mine.has(p));
    if (over) return err(403, "forbidden", { permission: over });
  }
  const invited = (b.emails ?? []).map((email) => ({ email, link: s.policy.smtp ? null : `https://${F.HOST}/invite/inv_${token("").slice(0, 12)}` }));
  audit(s, now, { event_type: "user.invited", what: invited.map((i) => i.email).join(", "), headline: "People were invited" }, who);
  return ok({ invited }, 201);
});
on("GET", "/v1/identity/users/([^/]+)", "user:read", ({ s, m }) => {
  const p = s.people.find((x) => x.id === m[1]);
  return p ? ok(p) : notFound();
});
on("GET", "/v1/identity/users/([^/]+)/factors", "user:read", ({ s, m }) => ok({ factors: s.factors[m[1]] ?? [] }));
for (const [action, perm] of [["disable", "user:disable"], ["enable", "user:disable"], ["reset-mfa", "user:reset_mfa"], ["revoke-sessions", "user:revoke_sessions"], ["reset-link", "user:update"]] as const) {
  on("POST", `/v1/identity/users/([^/]+)/${action}`, perm, ({ s, m, who, now }) => {
    const p = s.people.find((x) => x.id === m[1]);
    if (!p) return notFound();
    if (p.id === who && action === "disable") return err(409, "cannot_disable_self");
    if (action === "disable" && p.bindings.some((b) => b.role_key === "owner")
      && s.people.filter((x) => x.status === "active" && x.bindings.some((b) => b.role_key === "owner")).length <= 1) return err(409, "last_owner");
    if (action === "reset-mfa" && p.bindings.some((b) => b.role_key === "owner") && !holds(s, who, "owner:assign")) return err(403, "forbidden", { permission: "owner:assign" });
    if (action === "disable") p.status = "disabled";
    if (action === "enable") p.status = "active";
    if (action === "revoke-sessions") s.sessions[p.id] = [];
    audit(s, now, { event_type: `user.${action.replace("-", "_")}`, what: p.display_name, headline: `${p.display_name}: ${action.replace("-", " ")}` }, who);
    return ok(action === "reset-link" ? { link: `https://${F.HOST}/reset#t=${token("")}`, expires_in: 1800 } : { ok: true, cascade: action === "disable" ? cascade(null) : undefined });
  });
}
on("GET", "/v1/identity/sessions", "session:read", ({ s, req }) => ok({ sessions: s.sessions[req.query.get("principal_id") ?? ""] ?? [] }));
on("DELETE", "/v1/identity/sessions/([^/]+)", "session:revoke", ({ s, m }) => {
  for (const k of Object.keys(s.sessions)) s.sessions[k] = s.sessions[k].filter((x) => x.id !== m[1]);
  return ok({ ok: true });
});

on("GET", "/v1/identity/principals/([^/]+)/effective", "user:read", ({ s, req, m }) => {
  const scope = scopeOf(req.query);
  const eff = effective(s, m[1], scope);
  const entries: EffectiveEntry[] = PERMISSIONS.filter((p) => !PERMISSION_META[p].systemOnly).map((p) => {
    const via = eff.get(p) ?? [];
    return {
      permission: p, held: via.length > 0,
      why: via.map((b) => ({ role: b.role_name, role_key: b.role_key, scope: b.scope, via_group: b.via_group?.name ?? null })),
      held_by: via.length ? null : (PERMISSION_META[p].roles.includes("admin") ? "Admin at org" : PERMISSION_META[p].roles.includes("owner") ? "Owner only" : null),
    };
  });
  const byProject: Record<string, number> = {};
  for (const b of bindingsOf(s, m[1])) if (b.scope.startsWith("project:")) {
    const pid = b.scope.slice(8);
    byProject[pid] = effective(s, m[1], b.scope).size;
  }
  const out: EffectiveAccess = { principal_id: m[1], scope, held: eff.size, total: entries.length, by_project: byProject, entries };
  return ok(out);
});

on("GET", "/v1/identity/grantors", "self", ({ s, req }) => {
  const p = req.query.get("permission");
  if (!p || !isPermission(p)) return err(422, "unknown_permission");
  const grantors = s.people.filter((x) => x.status === "active" && holds(s, x.id, "role_binding:assign", "org") && holds(s, x.id, p))
    .map((x) => ({ id: x.id, name: x.display_name.split(" ")[0], role: x.bindings.find((b) => b.scope === "org")?.role_name ?? "", hue: x.hue }));
  return ok({ grantors });
});
on("POST", "/v1/identity/access-requests", "self", ({ req }) => {
  const { permission = "" } = body<{ permission: string }>(req);
  return isPermission(String(permission)) ? ok({ decision_id: `dec_${token("").slice(0, 8)}` }, 201) : err(422, "unknown_permission");
});

// ---- groups, roles, bindings -----------------------------------------------------------------

on("GET", "/v1/identity/groups", "group:read", ({ s }) => ok({ groups: groupsOf(s) }));
on("GET", "/v1/identity/groups/([^/]+)", "group:read", ({ s, m }) => {
  const g = groupsOf(s).find((x) => x.id === m[1]);
  return g ? ok(g) : notFound();
});
on("POST", "/v1/identity/groups/([^/]+)/members", "group:manage_members", ({ s, m, req, who, now }) => {
  const g = F.groups(s.t0, s.people).find((x) => x.id === m[1]);
  if (!g) return notFound();
  if (g.source !== "manual") return err(409, "idp_group_read_only");
  const { principal_id = "" } = body<{ principal_id: string }>(req);
  const p = s.people.find((x) => x.id === principal_id);
  if (!p) return notFound();
  for (const b of g.bindings) {
    const over = rolePerms(s, b.role_key).find((x) => !holds(s, who, x));
    if (over) return err(403, "forbidden", { permission: over });
  }
  if (!p.groups.some((x) => x.id === g.id)) p.groups.push({ id: g.id, name: g.name, source: g.source, provider: g.provider });
  audit(s, now, { event_type: "group.member_added", what: `${p.display_name} → ${g.name}`, headline: "A group gained a member" }, who);
  return ok({ ok: true });
});
on("DELETE", "/v1/identity/groups/([^/]+)/members/([^/]+)", "group:manage_members", ({ s, m }) => {
  const p = s.people.find((x) => x.id === m[2]);
  if (p) p.groups = p.groups.filter((g) => g.id !== m[1]);
  return ok({ ok: true });
});

on("GET", "/v1/identity/access-roles", "access_role:read", ({ s }) => ok({ roles: s.roles }));
on("GET", "/v1/identity/access-roles/([^/]+)", "access_role:read", ({ s, m }) => {
  const r = s.roles.find((x) => x.id === m[1] || x.key === m[1]);
  return r ? ok(r) : notFound();
});
on("GET", "/v1/identity/access-roles/([^/]+)/versions", "access_role:read", ({ s }) => ok({ versions: F.roleVersions(s.t0) }));
on("POST", "/v1/identity/access-roles/([^/]+)/preview", "access_role:read", ({ s, m, req }) => {
  const r = s.roles.find((x) => x.id === m[1]);
  if (!r) return notFound();
  const next = (body<{ permissions: string[] }>(req).permissions ?? []).filter(isPermission);
  return ok(preview(s, r, next));
});
on("PATCH", "/v1/identity/access-roles/([^/]+)", "access_role:manage", ({ s, m, req, who, now }) => {
  const r = s.roles.find((x) => x.id === m[1]);
  if (!r) return notFound();
  if (r.builtin) return err(409, "builtin_read_only");
  const b = body<{ permissions: string[]; name: string; description: string; confirm: string }>(req);
  const next = (b.permissions ?? r.permissions).filter(isPermission);
  const changed = [...next.filter((p) => !r.permissions.includes(p)), ...r.permissions.filter((p) => !next.includes(p))];
  const over = changed.find((p) => !holds(s, who, p));
  if (over) return err(403, "forbidden", { permission: over });
  const critical = next.filter((p) => !r.permissions.includes(p) && PERMISSION_META[p].critical);
  if (critical.length && b.confirm !== r.name) return err(422, "confirm_required", { permissions: critical });
  const before = r.permissions.length;
  r.permissions = next; r.version += 1;
  if (b.name) r.name = String(b.name);
  if (b.description) r.description = String(b.description);
  audit(s, now, { event_type: "access_role.updated", what: `${r.name} v${r.version - 1}→v${r.version} · ${next.length - before >= 0 ? "+" : ""}${next.length - before}`, headline: `${r.name} changed` }, who);
  return ok(r);
});
on("POST", "/v1/identity/access-roles/([^/]+)/clone", "access_role:manage", ({ s, m, who, now }) => {
  const r = s.roles.find((x) => x.id === m[1]);
  if (!r) return notFound();
  const over = r.permissions.find((p) => !holds(s, who, p));
  if (over) return err(403, "forbidden", { permission: over });
  const c: AccessRole = { ...r, id: `arl_${token("").slice(0, 8)}`, key: `${r.key}_copy`, name: `${r.name} copy`, builtin: false, holders: 0, version: 1,
    cloned_from: { id: r.id, name: r.name, version: r.version, at: new Date(now).toISOString() }, hue: 1 };
  s.roles.push(c);
  return ok(c, 201);
});
on("POST", "/v1/identity/role-bindings", "role_binding:assign", ({ s, req, who, now }) => {
  const b = body<{ subject_id: string; subject_type: string; role_key: string; role_id: string; scope: string; expires_at: string | null }>(req);
  // The API binds by role_id (BindingCreate); older console code sent role_key.
  if (!b.role_key && b.role_id) b.role_key = s.roles.find((r) => r.id === b.role_id)?.key ?? "";
  if (b.subject_type === "group") {
    const over = rolePerms(s, String(b.role_key)).find((p) => !holds(s, who, p));
    if (over) return err(403, "forbidden", { permission: over });
    const out = bindGroup(s, String(b.subject_id), String(b.role_key), "org", s.people.find((x) => x.id === who)?.display_name.split(" ")[0] ?? "BOSS", now, b.expires_at ?? null);
    return out ? ok(out, 201) : notFound();
  }
  const svc = s.sas.find((x) => x.id === b.subject_id);
  if (svc) {
    const role = s.roles.find((r) => r.key === b.role_key);
    if (!role) return notFound();
    const over = rolePerms(s, role.key).find((p) => !holds(s, who, p));
    if (over) return err(403, "forbidden", { permission: over });
    const binding = { id: `rb_${token("").slice(0, 6)}`, role_id: role.id, role_key: role.key, role_name: role.name, scope: (b.scope || "org") as ScopeLabel,
      subject_id: svc.id, via_group: null, granted_by: s.people.find((x) => x.id === who)?.display_name.split(" ")[0] ?? null, granted_at: new Date(now).toISOString(), expires_at: b.expires_at ?? null };
    svc.bindings.push(binding);
    svc.effective = new Set(svc.bindings.flatMap((x) => rolePerms(s, x.role_key))).size;
    return ok(binding, 201);
  }
  if (b.subject_id === who) return err(409, "cannot_bind_self");
  if (b.role_key === "owner" && !holds(s, who, "owner:assign")) return err(403, "forbidden", { permission: "owner:assign" });
  const over = rolePerms(s, String(b.role_key)).find((p) => !holds(s, who, p));
  if (over) return err(403, "forbidden", { permission: over });
  const p = s.people.find((x) => x.id === b.subject_id);
  const role = s.roles.find((r) => r.key === b.role_key);
  if (!p || !role) return notFound();
  p.bindings.push({ id: `rb_${token("").slice(0, 6)}`, role_id: role.id, role_key: role.key, role_name: role.name, scope: (b.scope || "org") as ScopeLabel,
    subject_id: p.id, via_group: null, granted_by: s.people.find((x) => x.id === who)?.display_name.split(" ")[0] ?? null, granted_at: new Date(now).toISOString(), expires_at: b.expires_at ?? null });
  audit(s, now, { event_type: "role_binding.added", what: `${role.name} → ${p.display_name}`, headline: "An access role was granted" }, who);
  return ok({ ok: true }, 201);
});
on("DELETE", "/v1/identity/role-bindings/([^/]+)", "role_binding:assign", ({ s, m, who, now }) => {
  if (unbindGroup(s, m[1])) return ok({ ok: true });
  for (const svc of s.sas) {
    if (!svc.bindings.some((x) => x.id === m[1])) continue;
    svc.bindings = svc.bindings.filter((x) => x.id !== m[1]);
    svc.effective = new Set(svc.bindings.flatMap((x) => rolePerms(s, x.role_key))).size;
    return ok({ ok: true });
  }
  for (const p of s.people) {
    const b = p.bindings.find((x) => x.id === m[1]);
    if (!b) continue;
    if (b.via_group) return err(409, "binding_is_inherited");
    if (p.id === who) return err(409, "cannot_bind_self");
    const over = rolePerms(s, b.role_key).find((x) => !holds(s, who, x));
    if (over) return err(403, "forbidden", { permission: over });
    p.bindings = p.bindings.filter((x) => x.id !== b.id);
    audit(s, now, { event_type: "role_binding.removed", what: `${b.role_name} ✕ ${p.display_name}`, headline: "An access role was removed" }, who);
    return ok({ ok: true });
  }
  return notFound();
});

// ---- service accounts and keys ---------------------------------------------------------------

on("GET", "/v1/identity/service-accounts", "service_account:read", ({ s }) => ok({ service_accounts: s.sas }));
on("GET", "/v1/identity/service-accounts/([^/]+)", "service_account:read", ({ s, m }) => {
  const sa = s.sas.find((x) => x.id === m[1]);
  return sa ? ok(sa) : notFound();
});
on("POST", "/v1/identity/service-accounts", "service_account:manage", ({ s, req, who, now }) => {
  const b = body<{ name: string; description: string; role_key: string; owner_principal_ids?: string[] }>(req);
  const over = b.role_key ? rolePerms(s, String(b.role_key)).find((p) => !holds(s, who, p)) : undefined;
  if (over) return err(403, "forbidden", { permission: over });
  const me = s.people.find((x) => x.id === who)!;
  const ownerIds = Array.isArray(b.owner_principal_ids) && b.owner_principal_ids.length ? b.owner_principal_ids : [who];
  const owners = ownerIds.flatMap((id) => { const p = s.people.find((x) => x.id === id); return p ? [{ id: p.id, name: p.display_name.split(" ")[0], hue: p.hue }] : []; });
  if (!owners.length) return err(422, "owner_required");
  const sa: ServiceAccount = { id: `sva_${token("").slice(0, 8)}`, name: String(b.name || "new-account"), description: String(b.description || ""), kind: "service",
    owners, bindings: [], keys: [], status: "active", created_by: me.display_name.split(" ")[0],
    created_at: new Date(now).toISOString(), last_used: null, effective: rolePerms(s, String(b.role_key || "viewer")).length, orphaned: false };
  s.sas.unshift(sa);
  return ok(sa, 201);
});
on("POST", "/v1/identity/service-accounts/([^/]+)/keys", "service_account_key:manage", ({ s, req, m, who, now }) => {
  const sa = s.sas.find((x) => x.id === m[1]);
  if (!sa) return notFound();
  const held = sa.bindings.flatMap((b) => rolePerms(s, b.role_key));
  const over = held.find((p) => !holds(s, who, p));
  if (over) return err(403, "forbidden", { permission: over });
  const b = body<{ name: string; scopes: string[]; projects: string[] | null; networks: string | null; expires_days: number | null; replaces: string | null; overlap_hours: number }>(req);
  const scopes = (b.scopes ?? []).filter(isPermission);
  const outside = scopes.find((p) => !held.includes(p));
  if (outside) return err(403, "forbidden", { permission: outside });
  const key: ApiKey = { id: `key_${token("").slice(0, 6)}`, name: String(b.name || "New key"), owner: { id: sa.id, name: sa.name, kind: "service_account" }, scopes,
    projects: b.projects ?? null, networks: b.networks ?? null, created_at: new Date(now).toISOString(), last_used_at: null, last_used_ip: null, last_used_client: null,
    expires_at: b.expires_days ? new Date(now + b.expires_days * 86_400_000).toISOString() : null, grace_until: null, rotated_from: b.replaces ?? null, status: "active" };
  let replaces: IssuedSecret["replaces"] = null;
  if (b.replaces) {
    const old = sa.keys.find((k) => k.id === b.replaces);
    if (old) {
      const hours = Number(b.overlap_hours) || 0;
      old.status = hours ? "grace" : "revoked";
      old.grace_until = hours ? new Date(now + hours * 3_600_000).toISOString() : null;
      replaces = { id: old.id, stops_at: old.grace_until };
    }
  }
  sa.keys.unshift(key);
  audit(s, now, { event_type: b.replaces ? "api_key.rotated" : "api_key.created", what: `${key.id} · ${sa.name}`, headline: "A key was created" }, who);
  const issued: IssuedSecret = { key, secret: `boss_sak_${token("").slice(0, 32)}${token("").slice(0, 11)}${token("").slice(0, 6)}`, replaces };
  return ok(issued, 201);
});
on("POST", "/v1/identity/service-accounts/([^/]+)/keys/([^/]+)/revoke", "service_account_key:manage", ({ s, m, who, now }) => {
  const k = s.sas.find((x) => x.id === m[1])?.keys.find((x) => x.id === m[2]);
  if (!k) return notFound();
  k.status = "revoked"; k.grace_until = null;
  audit(s, now, { event_type: "api_key.revoked", what: k.id, headline: "A key was revoked" }, who);
  return ok({ ok: true });
});
on("POST", "/v1/identity/service-accounts/([^/]+)/disable", "service_account:manage", ({ s, m }) => {
  const sa = s.sas.find((x) => x.id === m[1]);
  if (!sa) return notFound();
  sa.status = sa.status === "disabled" ? "active" : "disabled";
  return ok(sa);
});
on("GET", "/v1/identity/api-keys", "api_key:read", ({ s }) => ok({ keys: [...Object.values(s.personal).flat(), ...s.sas.flatMap((x) => x.keys)] }));

// ---- agents and delegations ------------------------------------------------------------------

on("GET", "/v1/identity/agents", "agent_passport:read", ({ s }) => ok({ agents: s.passports }));
on("GET", "/v1/identity/agents/([^/]+)/passport", "agent_passport:read", ({ s, m }) => {
  const a = s.passports.find((x) => x.id === m[1]);
  return a ? ok(a) : notFound();
});
on("PATCH", "/v1/identity/agents/([^/]+)/passport", "agent_passport:manage", ({ s, m, req, who, now }) => {
  const a = s.passports.find((x) => x.id === m[1]);
  if (!a) return notFound();
  const next = (body<{ ceiling: string[] }>(req).ceiling ?? a.ceiling).filter(isPermission);
  const bad = next.find((p) => !PERMISSION_META[p].delegable);
  if (bad) return err(422, "not_delegable", { permission: bad });
  const over = next.find((p) => !holds(s, who, p));
  if (over) return err(403, "forbidden", { permission: over });
  const removed = a.ceiling.filter((p) => !next.includes(p));
  const added = next.filter((p) => !a.ceiling.includes(p));
  a.ceiling = next;
  a.lineage.push({ at: new Date(now).toISOString(), event: "ceiling_changed", detail: [...added.map((p) => `+${p}`), ...removed.map((p) => `−${p}`)].join(" ") });
  audit(s, now, { event_type: "agent.passport.ceiling_changed", what: a.name, headline: `${a.name}'s ceiling changed` }, who);
  return ok(a);
});
for (const [action, perm] of [["suspend", "agent_passport:suspend"], ["resume", "agent_passport:suspend"], ["revoke", "agent_passport:revoke"]] as const) {
  on("POST", `/v1/identity/agents/([^/]+)/${action}`, perm, ({ s, m, who, now }) => {
    const a = s.passports.find((x) => x.id === m[1]);
    if (!a) return notFound();
    if (a.status === "revoked") return err(409, "revoked_is_terminal");
    a.status = action === "resume" ? "active" : action === "suspend" ? "suspended" : "revoked";
    audit(s, now, { event_type: `agent.passport.${action}`, what: a.name, headline: `${a.name}: ${action}` }, who);
    return ok(a);
  });
}
on("GET", "/v1/identity/delegations", "delegation:read_all", ({ s }) => ok({ delegations: s.delegations }));
// As the API: the grantor or a reader of every grant; anyone else reads 404.
on("GET", "/v1/identity/delegations/([^/]+)", "self", ({ s, m, who }) => {
  const d = s.delegations.find((x) => x.id === m[1]);
  return d && (d.user.id === who || holds(s, who, "delegation:read_all")) ? ok(detail(s, d)) : notFound();
});
on("POST", "/v1/identity/delegations/([^/]+)/revoke", "delegation:revoke_any", ({ s, m, req, who, now }) => {
  const d = s.delegations.find((x) => x.id === m[1]);
  if (!d) return notFound();
  if (d.status === "revoked") return err(409, "revoked_is_terminal");
  if (body<{ dry_run: boolean }>(req).dry_run) return ok(cascade(d));
  d.status = "revoked";
  audit(s, now, { event_type: "delegation.revoked", what: `${d.id} · halted ${cascade(d).halts} runs`, headline: "A grant was revoked", grant_id: d.id }, who);
  return ok(cascade(d));
});

// ---- providers, policy, posture, audit -------------------------------------------------------

on("GET", "/v1/identity/providers", "identity_provider:read", ({ s }) => ok({ providers: s.providers }));
on("POST", "/v1/identity/providers/([^/]+)/test", "identity_provider:manage", ({ s, m }) => {
  const p = s.providers.find((x) => x.id === m[1]);
  return p ? ok({ ok: true, issuer: p.issuer, checks: ["discovery", "jwks", "redirect_uri"] }) : notFound();
});
on("PATCH", "/v1/identity/providers/([^/]+)", "identity_provider:manage", ({ s, m, req }) => {
  const p = s.providers.find((x) => x.id === m[1]);
  if (!p) return notFound();
  Object.assign(p, body<Record<string, unknown>>(req));
  return ok(p);
});
on("GET", "/v1/identity/policy", "security_policy:read", ({ s }) => ok(s.policy));
on("PUT", "/v1/identity/policy", "security_policy:manage", ({ s, req, who, now }) => {
  Object.assign(s.policy, body<Record<string, unknown>>(req));
  audit(s, now, { event_type: "policy.changed", what: "security policy", headline: "The security policy changed" }, who);
  return ok(s.policy);
});
on("GET", "/v1/identity/posture", "user:read", ({ s, now }) => ok(posture(s, now)));
on("GET", "/v1/identity/audit", "audit:read", ({ s, req }) => {
  const q = (req.query.get("q") ?? "").toLowerCase();
  const outcome = req.query.get("outcome");
  const actor = req.query.get("actor");
  const grant = req.query.get("grant");
  const events = s.audit.filter((e) => (!q || `${e.event_type} ${e.what} ${e.actor.name} ${e.grant_id ?? ""} ${e.token_jti ?? ""}`.toLowerCase().includes(q))
    && (!outcome || outcome.split(",").includes(e.outcome)) && (!grant || e.grant_id === grant)
    && (!actor || e.actor.id === actor || e.on_behalf_of?.id === actor));
  return ok({ events, cursor: null, chain: { verified_at: new Date(s.verifiedAt).toISOString(), seq: s.seq, ok: true, first_break: null } });
});
on("GET", "/v1/identity/audit/([^/]+)", "audit:read", ({ s, m }) => {
  const e = s.audit.find((x) => x.id === m[1]);
  return e ? ok(e) : notFound();
});
on("POST", "/v1/identity/audit/verify", "audit:export", ({ s, now }) => {
  s.verifiedAt = now;
  return ok({ ok: true, seq: s.seq, verified_at: new Date(now).toISOString(), first_break: null });
});
on("POST", "/v1/identity/audit/export", "audit:export", ({ s }) => ok({ url: null, events: s.audit.length, anchor: F.hashLike(s.seq) }));
on("GET", "/v1/identity/catalogue", "self", () => ok({ fingerprint: CATALOGUE_FINGERPRINT, permissions: PERMISSIONS.map((p) => PERMISSION_META[p]) }));

// ---------------------------------------------------------------------------------------------

function preview(s: State, r: AccessRole, next: Permission[]): RolePreview {
  const added = next.filter((p) => !r.permissions.includes(p));
  const removed = r.permissions.filter((p) => !next.includes(p));
  const holders = s.people.filter((p) => p.bindings.some((b) => b.role_key === r.key) || p.groups.some((g) => g.id === "grp_release" && r.key === "release_captain"));
  const viaRelease = holders.filter((p) => p.groups.some((g) => g.id === "grp_release"));
  return {
    diff: { added, removed },
    affected: {
      people: holders.map((p) => ({ id: p.id, name: p.display_name.split(" ")[0], hue: p.hue, via: p.groups.some((g) => g.id === "grp_release") ? "Release" : null })),
      direct: holders.length - viaRelease.length, via_groups: viaRelease.length ? [{ group: "Release", count: viaRelease.length }] : [],
      service_accounts: s.sas.filter((x) => x.bindings.some((b) => b.role_key === r.key)).length,
      agents: [
        { id: "agt_harbor", name: "Harbor", grants: 2, change: null },
        { id: "agt_pilot", name: "Pilot", grants: 1, change: null },
        { id: "agt_forge", name: "Forge", grants: 2, change: removed.filter((p) => PERMISSION_META[p].delegable).map((p) => `−${p}`).join(" ") || null },
      ],
      grants: 5,
    },
    pages: { gained: added.some((p) => p.startsWith("deployment:")) ? ["Infra › Promote"] : [], lost: [] },
    routes: { gained: added.length * 3, lost: removed.length * 3 },
    tools: { gained: Math.min(2, added.length), lost: Math.min(1, removed.length) },
    warnings: added.filter((p) => PERMISSION_META[p].critical).map((p) => ({ permission: p, text: PERMISSION_META[p].delegable ? "critical" : "critical and never delegable" })),
  };
}

function cascade(d: Delegation | null): CascadePreview {
  if (!d || d.status !== "active") {
    return { halts: 0, items: [{ kind: "runs", label: "Halts running agents", detail: "none running", count: 0 }] };
  }
  const agents = d.agents.map((a) => a.name).join(", ");
  return {
    halts: d.agents.length,
    items: [
      { kind: "runs", label: "Halts running agents", detail: `${agents} on ${d.projects[0] ?? "this grant"}`, count: d.agents.length },
      { kind: "tokens", label: "Revokes run tokens", detail: "rt_7P, rt_7R, rt_7T and 4 refreshes", count: 7 },
      ...(d.github ? [{ kind: "github" as const, label: "Revokes the GitHub token", detail: `installation token for ${d.github.repos[0]}`, count: 1 }] : []),
      ...(d.subdelegation.allowed ? [{ kind: "grants" as const, label: "Revokes the child grant", detail: "dlg_01JB7R · Forge → Vera", count: 1 }] : []),
      { kind: "schedules", label: "Pauses a schedule", detail: "\"Nightly dependency bumps\"", count: 1 },
    ],
  };
}

function detail(s: State, d: Delegation): DelegationDetail {
  const userEff = [...effective(s, d.user.id, d.projects[0] ? (`project:${d.projects[0]}` as ScopeLabel) : "org").keys()];
  const granted = d.permissions;
  const nonDeleg = userEff.filter((p) => !PERMISSION_META[p].delegable && ["github:merge", "deployment:production", "decision:approve"].includes(p));
  const hops: ChainHop[] = [
    { kind: "person", id: d.user.id, name: s.people.find((p) => p.id === d.user.id)?.display_name ?? d.user.name, subtitle: "Operator · Admin in acme-web",
      stage: "SIGNED IN · PASSKEY", count: userEff.length, count_label: `${d.user.name.toUpperCase()} HOLDS`, effective: [...granted, ...nonDeleg],
      dropped: [], external: [{ label: "acme/web · write", dropped: false }, { label: "acme/infra · read", dropped: false }],
      rule: "passkey session · step-up 10:41", live: true, hue: d.user.hue },
    { kind: "grant", id: d.id, name: `Grant ${d.id.toLowerCase()}`, subtitle: `${d.user.name} chose · 30 days · ${d.projects.join(", ")}`,
      stage: "∩ SAM · NON-DELEGABLE DROPPED", count: granted.length, count_label: "GRANTED", effective: granted, dropped: nonDeleg,
      external: [{ label: "contents: write", dropped: false }, { label: "pull_requests: write", dropped: false }, { label: "acme/infra", dropped: true }],
      rule: `∩ ${d.user.name} live · may_act ${d.agents.map((a) => a.name).join(", ")}`, live: true, hue: 0 },
  ];
  let cur = granted;
  d.agents.forEach((a, i) => {
    const pass = s.passports.find((p) => p.id === a.id);
    const ceiling = pass?.ceiling ?? cur;
    const keep = cur.filter((p) => ceiling.includes(p)).slice(0, Math.max(3, cur.length - 1 - i * 2));
    const dropped = cur.filter((p) => !keep.includes(p));
    hops.push({ kind: "agent", id: a.id, name: a.name, subtitle: `${pass?.title ?? "Agent"} · run_${8811 + i}`,
      stage: i === 0 ? "ACT · DEPTH 1 · PLANS" : `ACT.ACT · DEPTH ${i + 1} · ${i === 1 ? "BUILDS" : "REVIEWS"}`,
      count: keep.length, count_label: `${a.name.toUpperCase()} MAY`, effective: keep, dropped,
      external: i === 2 ? [{ label: "contents: read", dropped: false }, { label: "pull_requests: write", dropped: false }, { label: "contents: write", dropped: true }]
        : [{ label: i === 1 ? "contents: write → boss/*" : "contents: read", dropped: false }, { label: "pull_requests: write", dropped: false }],
      rule: `∩ ceiling(${a.name}) · rt_7${"PRT"[i] ?? "X"}${i === 1 ? " · DPoP" : i === 2 ? " · max depth" : ""}`, live: true, hue: 0 });
    cur = keep;
  });
  const t = (h: number, m: number, sec: number) => { const x = new Date(); x.setHours(h, m, sec, 0); return x.toISOString(); };
  return {
    ...d, run: { id: "run_8812", title: "Interview Scorecard build" }, chain: hops,
    tokens: [
      { at: t(10, 41, 3), text: "Minted for Atlas · rt_7P · aud boss:api", agent: "Atlas", jti: "rt_7P", outcome: "live" },
      { at: t(10, 41, 40), text: "Exchanged → Forge · rt_7R · 6 scopes", agent: "Forge", jti: "rt_7R", outcome: "live" },
      { at: t(10, 52, 12), text: "Exchanged → Vera · rt_7T · 3 scopes", agent: "Vera", jti: "rt_7T", outcome: "live" },
      { at: t(10, 52, 30), text: "Vera asked to start Rook at depth 4", agent: "Vera", jti: null, outcome: "refused" },
    ],
    egress: [
      { method: "PUSH", path: "acme/web refs/heads/boss/8812-scorecard-auth", status: 200, note: null },
      { method: "POST", path: "/repos/acme/web/pulls → PR #212", status: 201, note: null },
      { method: "PUT", path: "/repos/acme/web/pulls/212/merge", status: 403, note: "human-only" },
      { method: "PUSH", path: "acme/web refs/heads/main", status: 403, note: "boss/* only" },
    ],
    checked: `${d.user.name} can write ${d.github?.repos[0] ?? "the repo"} on GitHub · 4 min ago`,
  };
}

function posture(s: State, now: number): Posture {
  const active = s.people.filter((p) => p.status === "active");
  const mfa = active.filter((p) => p.sign_in.passkeys || p.sign_in.totp || p.sign_in.idp_mfa).length;
  const noMfa = active.filter((p) => !(p.sign_in.passkeys || p.sign_in.totp || p.sign_in.idp_mfa));
  return {
    score: 86, headline: "Posture: good, 3 things to fix",
    summary: [`MFA on ${mfa} of ${active.length} people`, "2 legacy paths still in use", `Audit chain verified ${Math.max(1, Math.round((now - s.verifiedAt) / 60_000))} min ago · seq ${s.seq.toLocaleString("en-US")}`],
    checks: [
      { id: "mfa", label: "MFA coverage", status: noMfa.length ? "warn" : "ok", value: String(mfa), unit: `of ${active.length}`,
        detail: noMfa.length ? `${noMfa[0].display_name} has no second factor.` : "Everyone has a second factor.",
        fix: noMfa.length ? { label: `Remind ${noMfa[0].display_name.split(" ")[0]}`, href: `/identity/people/${noMfa[0].id}?tab=security` } : null },
      { id: "keys", label: "Stale keys", status: "warn", value: "2", unit: null, detail: "key_x9q2ab unused 41 days. key_7f3k2q expires in 6 days.",
        fix: { label: "Review keys", href: "/identity/service-accounts?sa=sva_ci_deployer" } },
      { id: "delegations", label: "Risky delegations", status: "bad", value: "1", unit: null, detail: "Param → Forge, Rook, Sentry: until revoked, 3 repos.",
        fix: { label: "Set an expiry", href: "/identity/agents/delegations/dlg_01JE2P" } },
      { id: "legacy", label: "Legacy paths", status: "bad", value: "2", unit: null, detail: "Shared console password: 2 sign-ins this week. BOSS_API_KEY used by 1 MCP client.",
        fix: { label: "Retire in 23 days", href: "/identity/sign-in#legacy" } },
      { id: "owners", label: "Owners & sponsors", status: "ok", value: "0", unit: "issues", detail: "2 Owners with passkeys. Every agent has a sponsor.", fix: null },
      { id: "audit", label: "Audit chain", status: "ok", value: "✓", unit: null, detail: `${s.seq.toLocaleString("en-US")} events, unbroken. Last anchor 00:00 UTC.`,
        fix: { label: "Verify now", href: "/identity/audit?verify=1" } },
    ],
    counts: {
      people: s.people.filter((p) => p.status !== "pending").length, active: active.length, invited: s.people.filter((p) => p.status === "pending").length,
      disabled: s.people.filter((p) => p.status === "disabled").length, agents: 34, suspended_agents: s.passports.filter((p) => p.status === "suspended").length,
      groups: 4, roles_builtin: 5, roles_custom: s.roles.filter((r) => !r.builtin).length, service_accounts: s.sas.length,
      active_grants: s.delegations.filter((d) => d.status === "active").length + 5, runs_acting: 3, tokens_out: 0,
    },
    acting_now: s.delegations.filter((d) => d.status === "active").slice(0, 4).map((d) => ({
      user: d.user, agents: d.agents, project: d.projects[0] ?? "", label: d.kind === "task" ? "this build only" : `${d.label.split(" on ")[0]} · ${Math.max(1, Math.round(((d.expires_at ? Date.parse(d.expires_at) : now) - now) / 86_400_000))}d`,
    })),
    audit: { verified_at: new Date(s.verifiedAt).toISOString(), seq: s.seq, ok: true },
    legacy: [{ kind: "legacy_password", label: "Shared console password", until: new Date(now + 23 * 86_400_000).toISOString() }, { kind: "legacy_api_key", label: "BOSS_API_KEY", until: null }],
  };
}

// ---------------------------------------------------------------------------------------------

const WRITE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/** Answer one request the way the API will. */
export function stubHandle(req: StubRequest, now = Date.now()): StubResponse {
  const s = stubState();
  const path = req.path.replace(/\/+$/, "");
  const matches = R.filter((r) => r.pattern.test(path));
  if (!matches.length) return notFound();
  const route = matches.find((r) => r.method === req.method);
  if (!route) return err(405, "method_not_allowed");
  const m = path.match(route.pattern)!;
  if (route.needs === "public") {
    return route.run({ s, req, now, m, who: "", sess: { principal: "", csrf: "", freshUntil: 0, methods: [], created: now } });
  }
  const tokenValue = req.cookies[SESSION_COOKIE];
  const sess = tokenValue ? s.live.get(tokenValue) : undefined;
  if (!sess) return err(401, "unauthenticated");
  if (WRITE.has(req.method) && req.headers.get("x-boss-csrf") !== sess.csrf) return err(403, "csrf_failed");
  const who = sess.principal;
  if (route.needs !== "self") {
    const scope = scopeOf(req.query);
    if (!holds(s, who, route.needs, scope) && !holds(s, who, route.needs, "org")) {
      return err(403, "forbidden", { permission: route.needs });
    }
    if (WRITE.has(req.method) && PERMISSION_META[route.needs].stepUp && sess.freshUntil <= now) {
      return err(401, "step_up_required", { permission: route.needs });
    }
  }
  return route.run({ s, req, now, m, who, sess });
}

/** Every route the stub answers, for the seam test and the report. */
export function stubRoutes(): { method: string; path: string; needs: string }[] {
  return R.map((r) => ({ method: r.method, path: r.pattern.source.replace(/^\^|\$$/g, ""), needs: r.needs }));
}

// Per-area routes, registered after the core table (a file per area, so streams of work on
// different screens never edit the same stub file).
registerPeopleRoutes(on);
registerRoleRoutes(on);
registerAgentRoutes(on);
registerAuthRoutes(on);
registerSsoRoutes(on);
registerGroupRoutes(on);
registerAccountRoutes(on);
