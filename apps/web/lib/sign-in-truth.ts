/**
 * WHAT SIGNING IN TO BOSS ACTUALLY USES, said the same way everywhere it is said.
 *
 * The console walk on boss-1 (2026-10-02) found three answers to one question. The Identity
 * area was subtitled "Who can sign in — OIDC, SAML, SCIM", but console sign-in is a single
 * installer-generated password (app/login, middleware.ts) and never consults a configured
 * provider. The v2 Settings screen said the opposite: "This deployment authenticates with an
 * API key. There is no SAML or SCIM to configure" — while v1 offered SAML and SCIM forms.
 *
 * Since BOSS Identity (2026-10) everyone signs in as themselves; the installer password is a
 * legacy path the Overview counts down. These sentences are the one answer. The v2 Settings reason comes from the engine
 * (console_v2_platform.SIGN_IN_TRUTH), and tests/identity-says-what-sign-in-uses.test.ts fails
 * if the two drift apart.
 */
export const CONSOLE_SIGN_IN_TRUTH =
  "Everyone signs in as themselves: with a password, a passkey, or a provider added under " +
  "Identity › Sign-in & SSO, plus a second factor where their access role needs one.";

export const API_AUTH_TRUTH =
  "Calls to the BOSS API authenticate with an API key, or with an OIDC bearer token when the " +
  "deployment sets BOSS_OIDC_ENABLED.";
