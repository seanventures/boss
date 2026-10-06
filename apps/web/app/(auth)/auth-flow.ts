/**
 * Steps the auth screens share once a session exists: learn its CSRF token (auth pages have no
 * IdentityProvider to do it), register a passkey on this device, and words for a refusal.
 */
import { IdentityError, idGet, idPost, setCsrfToken } from "../../lib/identity/api";
import { ME_PATH, type MeResponse } from "../../lib/identity/contract";
import { createCredential, PasskeyCancelled, type RegistrationOptions } from "../../lib/identity/webauthn";

/** Read /me once a session was just issued, so the writes that follow carry X-Boss-CSRF. */
export async function primeCsrf(): Promise<MeResponse> {
  const me = await idGet<MeResponse>(ME_PATH);
  setCsrfToken(me.csrf_token);
  return me;
}

/** A name for a new passkey that says which device holds it ("Mac · Chrome"). */
export function deviceName(): string {
  if (typeof navigator === "undefined") return "This device";
  const ua = navigator.userAgent;
  const os = /iPhone|iPad/.test(ua) ? "iPhone" : /Mac/.test(ua) ? "Mac" : /Android/.test(ua) ? "Android" : /Windows/.test(ua) ? "Windows" : /Linux/.test(ua) ? "Linux" : "This device";
  const browser = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : "";
  return browser ? `${os} · ${browser}` : os;
}

/** Register a passkey on this device for the signed-in person (IDENTITY.md §4.5). */
export async function registerPasskey(name = deviceName()): Promise<void> {
  const options = await idPost<RegistrationOptions>("/v1/identity/me/passkeys/options", {});
  const credential = await createCredential(options);
  await idPost("/v1/identity/me/passkeys", { name, credential });
}

const WORDS: Record<string, string> = {
  invalid_credentials: "Wrong password",
  invalid_code: "That code didn't work",
  invalid_fields: "Check the fields",
  password_too_short: "Use at least 12 characters",
  rate_limited: "Too many tries — wait a minute",
  link_invalid: "This link no longer works",
  not_found: "This link no longer works",
  identity_unavailable: "BOSS can't be reached",
  last_factor: "Add another factor first",
};

export function sayAuth(e: unknown): string {
  if (e instanceof PasskeyCancelled) return "Passkey cancelled";
  if (e instanceof IdentityError) {
    if (e.code === "invalid_code" && e.attemptsLeft !== null) return `That code didn't work · ${e.attemptsLeft} left`;
    return WORDS[e.code] ?? e.message;
  }
  if (e instanceof Error && e.message === "webauthn_unavailable") return "This browser has no passkeys";
  return "That didn't work";
}
