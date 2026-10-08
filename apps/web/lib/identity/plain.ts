/**
 * API-written sentences (an audit event's "what", a lineage line, a chain hop's rule) made
 * readable: permission keys become their names, event names become words, and raw ids
 * (usr_…, key_…, dlg_…) are dropped from the visible text. The original stays available for a
 * tooltip. Built on lib/identity/words.ts.
 */
import { isPermission } from "./permissions.gen";
import { eventWords, permissionName } from "./words";

// An id with its qualifiers: "rt_ab12:git:315ad114331e" is one id, not an id and ":git:315ad…".
const ID = /\b(?:usr|agt|dlg|key|sak|pat|sva|grp|idp|acr|ses|rol|bnd|evt|inv|tok|run|arl|sev|rt)_[A-Za-z0-9_]{2,}(?::[A-Za-z0-9_.-]+)*/g;
// A qualifier left behind by an id written without its prefix (":git:315ad114331e").
const FRAGMENT = /(^|\s)(?::[a-z]+)+:[0-9a-f]{7,}\b/g;
const EVENT = /\b(?:auth|egress|user|group|access_role|role_binding|api_key|service_account|session|delegation|passport|agent|sso|scim|policy|audit|access|mfa|passkey|login|connection|token|invitation|identity)\.[a-z_]+(?:\.[a-z_]+)?\b/g;
const KEY = /\b[a-z_]+:[a-z_]+\b/g;

export function plainText(text: string | null | undefined): string {
  if (!text) return "";
  return text
    // The engine's set notation, in words: "∩ ceiling(Forge)" reads "within Forge's limit".
    .replace(/ceiling\(([^)]+)\)/gi, "$1's limit")
    .replace(/\bceilings?\b/gi, (w) => (w[0] === "C" ? "Limit" : "limit") + (w.endsWith("s") ? "s" : ""))
    .replace(/∩/g, "within")
    .replace(/\bDPoP\b/g, "key-bound token")
    .replace(/\bmax depth\b/gi, "deepest allowed")
    .replace(/∪/g, "or")
    .replace(/\bmay_act\b/g, "may act for")
    .replace(/\bjti\b/gi, "token")
    .replace(KEY, (k) => (isPermission(k) ? permissionName(k) : k))
    .replace(EVENT, (e) => eventWords(e).toLowerCase())
    .replace(ID, "")
    .replace(FRAGMENT, "$1")
    // A bare API code (invalid_credentials) reads as words.
    .replace(/(^|[\s(·])([a-z]+(?:_[a-z]+)+)(?=$|[\s),·])/g, (_m, pre: string, code: string) => `${pre}${code.replace(/_/g, " ")}`)
    .replace(/\s*·\s*(·\s*)+/g, " · ")
    .replace(/^\s*·\s*|\s*·\s*$/g, "")
    .replace(/\(\s*\)/g, "")
    .replace(/\s{2,}/g, " ")
    .trim();
}
