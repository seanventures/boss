/**
 * CAN THIS BOSS HOLD A PASSKEY, AND WHY A PASSKEY FAILED (round 3, boss-1 at https://100.64.0.20).
 *
 * WebAuthn's RP ID must be a domain name on a secure context. Reached by an IP address, every
 * browser refuses before Touch ID even runs, and the claim's "Create your passkey" ended in a bare
 * "That didn't work". The API now says so (`/v1/auth/options` passkey_reason, from
 * engine/boss_engine/identity/webauthn.py passkey_blocker) and never hands out options for such an
 * origin; the console offers the authenticator app instead, with one plain line, and every WebAuthn
 * failure that still happens is named from its DOMException.
 */

export const PASSKEY_NEEDS_DOMAIN_WORDS = "Passkeys need a domain name. This BOSS is reached by an IP address.";
export const PASSKEY_NEEDS_HTTPS_WORDS = "Passkeys need HTTPS. This BOSS is reached over plain HTTP.";

/** An IPv4 or IPv6 literal (as URL.hostname gives it: v6 in brackets). */
export function isIpHost(hostname: string | null | undefined): boolean {
  const h = (hostname ?? "").trim().toLowerCase().replace(/^\[|\]$/g, "");
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(h)) return true;
  return h.includes(":") && /^[0-9a-f:.]+$/.test(h);
}

/** The page a passkey would be created on. `secure` is `window.isSecureContext` when the caller has it. */
export type PasskeyPlace = { hostname?: string; protocol?: string; secure?: boolean } | null | undefined;

/** https, an explicit secure context, or localhost — not "plain HTTP". */
function placeIsSecure(where: PasskeyPlace): boolean {
  if (!where) return false;
  if (where.secure === true) return true;
  if (where.protocol === "https:") return true;
  const host = (where.hostname ?? "").trim().toLowerCase();
  return host === "localhost" || host.endsWith(".localhost");
}

/**
 * Why passkeys are impossible here, in words — or null when they can be offered. An IP is always
 * a domain problem. A page the browser already treats as a secure context is never "plain HTTP",
 * even when the API judged a forwarded http scheme (`passkey_needs_https`). An unknown place
 * (no `where`) still trusts that API reason.
 */
export function passkeyBlockWords(reason: string | null | undefined, where: PasskeyPlace): string | null {
  if (reason === "passkey_needs_domain") return PASSKEY_NEEDS_DOMAIN_WORDS;
  const hostname = where?.hostname ?? "";
  if (hostname && isIpHost(hostname)) return PASSKEY_NEEDS_DOMAIN_WORDS;
  if (!placeIsSecure(where)) {
    if (reason === "passkey_needs_https") return PASSKEY_NEEDS_HTTPS_WORDS;
    if (hostname && where?.protocol && where.protocol !== "https:") return PASSKEY_NEEDS_HTTPS_WORDS;
  }
  return null;
}

/** This page, including the browser's secure-context bit. Null during a server render. */
export function passkeyPlace(): { hostname: string; protocol: string; secure: boolean } | null {
  if (typeof window === "undefined") return null;
  return { hostname: window.location.hostname, protocol: window.location.protocol, secure: window.isSecureContext };
}

/** The browser's own reasons a WebAuthn call failed (DOMException.name), as one short sentence. */
const DOM_WORDS: Record<string, string> = {
  NotAllowedError: "Passkey cancelled or timed out",
  AbortError: "Passkey cancelled",
  SecurityError: "This address can't hold a passkey. Passkeys need a domain name over HTTPS",
  InvalidStateError: "This device already has a passkey here",
  NotSupportedError: "This device can't make this kind of passkey",
  ConstraintError: "This device can't verify it's you (set a screen lock or PIN)",
  NotReadableError: "The device's passkey store couldn't be read. Try again",
  UnknownError: "The device's passkey store failed. Try again",
  TypeError: "The browser refused the passkey request",
  webauthn_unavailable: "This browser has no passkeys",
};

/** A WebAuthn failure, carried with the browser's reason. */
export class PasskeyFailure extends Error {
  readonly reason: string;
  constructor(reason: string, detail?: string) {
    super(detail ? `${reason}: ${detail}` : reason);
    this.name = "PasskeyFailure";
    this.reason = reason;
  }
}

/** Words for a passkey failure, or null when `e` is not one. Never a bare "didn't work". */
export function passkeyFailureWords(e: unknown): string | null {
  if (e instanceof PasskeyFailure) return DOM_WORDS[e.reason] ?? `Passkey failed (${e.reason})`;
  if (typeof DOMException !== "undefined" && e instanceof DOMException) return DOM_WORDS[e.name] ?? `Passkey failed (${e.name})`;
  return null;
}
