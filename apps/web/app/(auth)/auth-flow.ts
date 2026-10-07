/**
 * Steps the auth screens share once a session exists: learn its CSRF token (auth pages have no
 * IdentityProvider to do it), register a passkey on this device, and words for a refusal.
 */
import { ENROLMENT_GRANT_REQUIRED, ENROLMENT_GRANT_WORDS } from "../../lib/identity/enrolment";
import { IdentityError, idGet, idPost, setCsrfToken } from "../../lib/identity/api";
import { ME_PATH, type MeResponse } from "../../lib/identity/contract";
import { codesOf, type RecoveryCodes } from "../../lib/identity/types-auth";
import { createCredential, type RegistrationOptions, type Wrapped } from "../../lib/identity/webauthn";
import { PASSKEY_NEEDS_DOMAIN_WORDS, PASSKEY_NEEDS_HTTPS_WORDS, passkeyFailureWords } from "../../lib/identity/passkey-support";

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

/**
 * Register a passkey on this device for the signed-in person (IDENTITY.md §4.5). Returns the
 * recovery codes the API issues with a person's FIRST factor (shown once), or [] otherwise —
 * asking /me/recovery-codes again would silently replace the set just issued.
 */
export async function registerPasskey(name = deviceName()): Promise<string[]> {
  const options = await idPost<Wrapped<RegistrationOptions>>("/v1/identity/me/passkeys/options", {});
  const credential = await createCredential(options);
  return codesOf(await idPost<RecoveryCodes | null>("/v1/identity/me/passkeys", { name, credential }));
}

const WORDS: Record<string, string> = {
  invalid_credentials: "Wrong password",
  invalid_code: "That code didn't work",
  code_invalid: "That code didn't work",
  invalid_fields: "Check the fields",
  password_too_short: "That password is too short",
  password_too_long: "That password is too long",
  password_too_common: "That password is too common — pick another",
  reset_invalid: "This link no longer works",
  invitation_invalid: "This invitation no longer works",
  invalid_setup_token: "That setup token didn't work",
  origin_mismatch: "Open BOSS at its own address to claim it",
  credential_required: "Prove you run this BOSS first",
  email_in_use: "That email already has an account",
  password_required: "Choose a password",
  invalid_email: "Check the email address",
  no_second_factor: "Add a second factor first",
  busy: "Busy — try again in a moment",
  rate_limited: "Too many tries — wait a minute",
  link_invalid: "This link no longer works",
  not_found: "This link no longer works",
  identity_unavailable: "BOSS can't be reached",
  last_factor: "Add another factor first",
  mfa_enrolment_required: "Add a second factor first",
  // The API refused passkey options (engine webauthn.passkey_blocker / its PasskeyError codes).
  passkey_needs_domain: PASSKEY_NEEDS_DOMAIN_WORDS,
  passkey_needs_https: PASSKEY_NEEDS_HTTPS_WORDS,
  passkey_unavailable: "Passkeys aren't set up on this BOSS yet. Use an authenticator app",
  passkey_limit_reached: "You already have 10 passkeys. Remove one first",
  passkey_verification_failed: "The device's answer didn't check out. Try again",
  passkey_challenge_invalid: "The passkey request expired. Try again",
  passkey_unknown_credential: "BOSS doesn't know that passkey",
  passkey_user_mismatch: "That passkey belongs to someone else",
  passkey_aaguid_not_allowed: "This kind of authenticator isn't allowed here",
  passkey_counter_regressed: "That passkey looks copied, so it's blocked. Ask an admin",
  // Enrolment itself refused: this session may not add a factor (lib/identity/enrolment.ts).
  [ENROLMENT_GRANT_REQUIRED]: ENROLMENT_GRANT_WORDS,
};

/**
 * Why single sign-on came back without signing you in: identity_sso.py redirects to
 * `/signin?error=<code>` (S1) with one of these codes. Short, and says what to do.
 */
const SSO_WORDS: Record<string, string> = {
  not_invited: "No account for that email. Ask an admin to invite you.",
  account_disabled: "This account is disabled. Ask an admin.",
  already_linked: "That sign-in is already linked to another person.",
  link_invalid: "That sign-in link expired. Try again.",
  second_factor_required: "Your role needs a second factor. Sign in with your password first.",
  insecure_origin: "Open BOSS over its HTTPS address to use single sign-on.",
  state_invalid: "Single sign-on took too long or switched browsers. Try again.",
  nonce_mismatch: "Single sign-on took too long or switched browsers. Try again.",
  issuer_mismatch: "The identity provider answered for someone else. Ask an admin to check it.",
  token_failed: "The identity provider refused BOSS. Ask an admin to check it.",
  sso_required: "Your organisation signs in with single sign-on only.",
  [ENROLMENT_GRANT_REQUIRED]: ENROLMENT_GRANT_WORDS,
};

export function ssoErrorWords(code: string | null | undefined): string | null {
  if (!code) return null;
  const c = code.toLowerCase();
  if (SSO_WORDS[c]) return SSO_WORDS[c];
  if (c.startsWith("saml_")) return "The SAML sign-in was refused. Ask an admin to check the provider.";
  return "Single sign-on didn't finish. Try again, or use email.";
}

export function sayAuth(e: unknown): string {
  const passkey = passkeyFailureWords(e);
  if (passkey) return passkey;
  if (e instanceof IdentityError) {
    if (e.code === "invalid_code" && e.attemptsLeft !== null) return `That code didn't work · ${e.attemptsLeft} left`;
    return WORDS[e.code] ?? e.message;
  }
  // Never the bare sentence: whatever failed says its name.
  return e instanceof Error && e.message ? `That didn't work · ${e.message}` : "That didn't work · no answer";
}
