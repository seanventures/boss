/**
 * WHAT A COMMUNITY USER MUST NEVER READ ON AN IDENTITY SCREEN: a permission key (user:invite —
 * the name "Invite people" is shown, the key only in a tooltip), an API error code
 * (identity_unavailable), an audit event name (access_role.updated), a raw id (usr_…, dlg_…) or
 * the jargon the audit flagged (∩, union, jti, JWKS, ceiling, id_token).
 *
 * Scans the RENDERED text, including screen-reader-only text and <option>s. Attribute values
 * (title, aria-*) are not scanned: a tooltip may carry the id. A subtree marked data-raw-ok is
 * skipped: the raw-JSON view of a stored event, a secret shown once.
 */
import { PERMISSIONS } from "../../lib/identity/permissions.gen";

const PERM = new RegExp(`(^|[^\\w:])(${[...PERMISSIONS].sort((a, b) => b.length - a.length).map((p) => p.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})(?![\\w:])`);
const CODES = [
  "identity_unavailable", "step_up_required", "step_up_cancelled", "csrf_failed", "not_found", "invalid_credentials",
  "rate_limited", "last_owner", "policy_refused", "invalid_policy", "confirmation_required", "consent_required",
  "invalid_provider", "issuer_in_use", "field_not_allowed", "name_required", "invalid_email", "email_in_use",
  "unknown_permission", "role_exists", "role_in_use", "group_exists", "revoked_is_terminal", "too_long",
  "no_second_factor", "mfa_enrolment_required", "enrolment_grant_required", "invalid_code", "code_invalid",
  "unauthenticated", "forbidden", "discovery_failed", "jwks_failed", "not_invited", "needs_reauth",
];
const CODE = new RegExp(`(^|[^\\w])(${CODES.join("|")})(?!\\w)`);
const EVENT = /(^|[^\w.@/])((user|group|access_role|role_binding|api_key|service_account|session|delegation|passport|agent|sso|scim|policy|audit|access|mfa|passkey|login|connection|token|invitation|identity)\.[a-z_]+(\.[a-z_]+)?)(?![\w@/])/;
const ID = /(^|[^\w/])((usr|agt|dlg|key|sak|pat|sva|grp|idp|acr|ses|rol|bnd|evt|inv|tok|run)_[A-Za-z0-9]{3,})/;
const JARGON = /∩|∪|\bunion\b|\bjti\b|\bJWKS\b|\bceiling\b|\bid_token\b/i;

export type RawLeak = { kind: "permission" | "code" | "event" | "id" | "jargon"; text: string };

function textNodes(root: Element): string[] {
  const out: string[] = [];
  const walk = (n: Node) => {
    if (n.nodeType === 3) { const t = (n.textContent ?? "").trim(); if (t) out.push(t); return; }
    if (n.nodeType !== 1) return;
    const el = n as Element;
    if (el.matches("script, style, [data-raw-ok]")) return;
    for (const c of el.childNodes) walk(c);
  };
  walk(root);
  return out;
}

export function rawLeaks(root: Element): RawLeak[] {
  const leaks: RawLeak[] = [];
  // Whole lines of a parent, so a key split across nodes is still caught.
  const lines = [...textNodes(root), ...[...root.querySelectorAll("*")].filter((el) => !el.closest("[data-raw-ok], script, style")).map((el) => textNodes(el).join(""))];
  for (const t of new Set(lines)) {
    let m: RegExpExecArray | null;
    if ((m = PERM.exec(t))) leaks.push({ kind: "permission", text: m[2] });
    if ((m = CODE.exec(t))) leaks.push({ kind: "code", text: m[2] });
    if ((m = EVENT.exec(t))) leaks.push({ kind: "event", text: m[2] });
    if ((m = ID.exec(t))) leaks.push({ kind: "id", text: m[2] });
    if ((m = JARGON.exec(t))) leaks.push({ kind: "jargon", text: m[0] });
  }
  const seen = new Set<string>();
  return leaks.filter((l) => { const k = `${l.kind}:${l.text}`; if (seen.has(k)) return false; seen.add(k); return true; });
}
