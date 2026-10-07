/**
 * THE IDENTITY SCREENS' WORDS FOR MACHINE VALUES, one definition each.
 *
 * A community user reads names, never ids: a permission is "Invite people" (its key, user:invite,
 * goes only in a tooltip), an audit event is "Access role updated" (not access_role.updated), and
 * a refusal is a short sentence (not forbidden or identity_unavailable). Every identity screen
 * renders these through here; tests/identity-shows-no-raw-codes.test.tsx fails when one leaks.
 */
import { PERMISSION_META, type Permission } from "./permissions.gen";

type Meta = (typeof PERMISSION_META)[Permission];
const META = PERMISSION_META as Record<string, Meta | undefined>;

/** "user:invite" → "Invite people". An unknown key is humanised, never shown raw. */
export function permissionName(p: string): string {
  const meta = META[p];
  if (meta) return meta.title;
  return humanise(p.replace(/:/g, " "));
}

/** The tooltip that carries the id: "user:invite · Invite by email…" (≤ 14 words). */
export function permissionTip(p: string): string {
  return p;
}

/** Who to ask for a permission the viewer lacks: an Admin, or an Owner when only Owners hold it. */
export function askWho(p: string): string {
  const roles = META[p]?.roles ?? [];
  return roles.includes("admin") ? "ask an Admin" : "ask an Owner";
}

/** The disabled control's reason: Needs “Invite people” · ask an Admin. */
export function needsWords(p: string): string {
  return `Needs “${permissionName(p)}” · ${askWho(p)}`;
}

/** "access_role.updated" → "Access role updated"; "auth.login.failed" → "Sign-in failed". */
const EVENT_NOUNS: Record<string, string> = {
  auth: "", authn: "", authz: "access", sso: "single sign-on", scim: "directory sync", mfa: "second factor",
  api_key: "API key", access_role: "access role", role_binding: "access", idp: "sign-in provider",
  provider: "provider", totp: "authenticator app", webauthn: "passkey", oidc: "single sign-on", saml: "single sign-on",
  login: "sign-in", step_up: "identity confirmed", egress: "outbound call", vault: "", jit: "just-in-time",
  passport: "Passport", ceiling_changed: "access limit changed", shadow_deny: "would be denied", new_device: "new device",
  used_from_new_ip: "used from a new address", email_code: "email code", magic_link: "email link", pr_opened: "PR opened",
};
export function eventWords(type: string): string {
  const parts = type.split(".").filter(Boolean).map((p) => (p in EVENT_NOUNS ? EVENT_NOUNS[p] : p.replace(/_/g, " "))).filter(Boolean);
  const s = parts.join(" ").replace(/\s+/g, " ").trim();
  return s ? s[0].toUpperCase() + s.slice(1) : humanise(type);
}

/** "identity_unavailable" → "Identity unavailable". For a code nobody mapped; prefer a sentence. */
export function humanise(code: string): string {
  const s = code.replace(/[_.:-]+/g, " ").trim().toLowerCase();
  return s ? s[0].toUpperCase() + s.slice(1) : "";
}

/** Why a server read failed, as a heading and one plain sentence (ErrorState's words). */
export function loadFailureWords(what: string, status: number, code: string, permission?: string | null): { title: string; body: string } {
  if (status === 403 && permission) return { title: `You can't see ${what}`, body: `This needs “${permissionName(permission)}”. ${cap(askWho(permission))}.` };
  if (status === 403) return { title: `You can't see ${what}`, body: "Your access does not include this. Ask an Admin." };
  if (status === 401) return { title: "Your session ended", body: "Sign in again to continue." };
  if (status === 404) return { title: `${cap(what)} not found`, body: "It may have been removed, or the link is out of date." };
  if (status === 0 || status === 503 || code === "identity_unavailable") {
    return { title: "Identity is unreachable", body: "BOSS could not reach its identity service. Try again in a moment." };
  }
  if (status === 429) return { title: "Too many requests", body: "Wait a few seconds, then try again." };
  return { title: `Couldn't load ${what}`, body: "The server answered with an error. Try again; if it keeps failing, check the API logs." };
}

function cap(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

/** An approval seat's key as a title: "release_manager" → "Release manager", "ceo" → "CEO". */
const SEATS: Record<string, string> = { ceo: "CEO", cto: "CTO", cao: "CAO", cfo: "CFO", vp_agents: "VP of Agents", release_manager: "Release manager" };
export function seatName(key: string): string {
  return SEATS[key] ?? humanise(key);
}

/** How a session signed in, in words: "pwd" → "password", "sso:okta" → "Okta SSO". */
const METHODS: Record<string, string> = {
  pwd: "password", password: "password", totp: "authenticator app", passkey: "passkey", webauthn: "passkey", hwk: "security key",
  recovery: "recovery code", email_code: "email code", magic_link: "email link", otp: "one-time code", mfa: "second factor",
  network: "trusted network", setup_token: "setup link", legacy_password: "shared password",
};
export function methodWords(m: string): string {
  // `sso:<provider id>`: an id is not a name, so it reads "SSO" unless it is a plain word.
  if (m.startsWith("sso:")) { const n = m.slice(4); return /^[a-z]+$/.test(n) ? `${n[0].toUpperCase()}${n.slice(1)} SSO` : "SSO"; }
  return METHODS[m] ?? humanise(m).toLowerCase();
}

/** "1 key", "3 keys". */
export function keyCount(n: number): string {
  return `${n} ${n === 1 ? "key" : "keys"}`;
}

/**
 * A change in a count, as the impact preview shows it: "+3", "−2", "+3 −2", or "no change". A
 * zero side is never printed: "+0 −0" read as a broken preview (round 2, R2-WEB). Every delta on
 * an identity screen is drawn by <Delta> (components/identity/kit.tsx), which uses this.
 */
export function deltaParts(gained: number, lost: number): { gained: string | null; lost: string | null; none: boolean } {
  const g = Math.max(0, Math.trunc(gained || 0));
  const l = Math.max(0, Math.trunc(lost || 0));
  return { gained: g ? `+${g}` : null, lost: l ? `−${l}` : null, none: !g && !l };
}

/** An audit outcome in words ("ok" → "done"); an unknown one is humanised, never shown raw. */
const OUTCOMES: Record<string, string> = { ok: "done", denied: "refused", failed: "failed", notice: "notice" };
export function outcomeWords(outcome: string): string {
  return OUTCOMES[outcome] ?? humanise(outcome).toLowerCase();
}
