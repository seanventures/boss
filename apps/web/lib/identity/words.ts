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

/** "access_role.updated" → "Access role updated"; "sso.provider.created" → "Sign-in provider created". */
const EVENT_NOUNS: Record<string, string> = {
  sso: "Sign-in", authz: "Access", auth: "Sign-in", authn: "Sign-in", scim: "Directory sync", mfa: "Second factor", api_key: "API key", access_role: "Access role",
  role_binding: "Access", jit: "Just-in-time", idp: "Sign-in provider", provider: "provider", totp: "authenticator app",
  webauthn: "passkey", oidc: "single sign-on", saml: "single sign-on",
};
export function eventWords(type: string): string {
  const parts = type.split(".").filter(Boolean).map((p) => EVENT_NOUNS[p] ?? p.replace(/_/g, " "));
  return humanise(parts.join(" ").replace(/\s+/g, " "));
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
