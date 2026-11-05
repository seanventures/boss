# PARITY-812 — password first, passkey on request

Operator report (boss-1, 2026-11-04): the claim let the first Owner create the account with a
passkey only; in any other browser the login page showed "Use your passkey — Waiting for your
device…", Chrome asked to insert a security key, and there was no password to fall back to.

## Capability parity (old → new)

| Capability | Before | After |
|---|---|---|
| Claim proof (setup link, shared password, network bridge + origin confirm) | yes | unchanged |
| Claim password | only where no passkey was possible | **always required** (UI + API `password_required`), show/hide eye, policy minimum unchanged |
| Claim second factor | passkey ceremony started on submit; authenticator only as fallback | "Protect your account": Passkey on this device (recommended) or Authenticator app; after a passkey, "Add an authenticator too?" (skippable); network-bridge path keeps passkey (D27) |
| Recovery codes after claim | yes | unchanged (codes from the first factor are kept while a second is added) |
| Sign-in layout (logo, providers, passkey button, "or use email", tabs, show/hide, Forgot, Create account) | yes | unchanged |
| Passkey ceremony on page load | not in code, but a passkey-only account had nothing else | never; only the button or a passkey picked from the email autofill |
| Passkey autofill (conditional mediation) | attribute only | real: `mediation: "conditional"` where the browser supports it; aborted before any other passkey request |
| Passkey sign-in cancelled/failed | waiting screen with an error | back to the form: "No passkey here? Use your password instead.", focus on email/password |
| Second step after password | passkey-only accounts got a waiting screen with no ceremony | code by default; passkey-only accounts get a "Use your passkey" button; switch either way; "Try another way" + recovery code |
| Account enumeration | factors only after a correct password | unchanged |
| Account › Security password card | always "Change password" | "Set password" (no current-password field) when there is none; "Change password" otherwise |
| Passkey-only accounts (claimed before) | no prompt | `no_password` banner "Add a password so you can sign in from any browser." → /account#acc-pw-h; dismissible for good; passkey and code sign-in keep working |
| Enrol (invite, sign-up, MFA-required sign-in) | factor then codes | titled "Protect your account"; a person with no password (emailed sign-up) chooses one after the factor (first password needs a fresh factor, API unchanged), or "Later, in My account" |
| Invite accept | password + factor | unchanged |
| `boss identity setup-link`, recover-owner | yes | untouched; covered by test_identity_setup_link_over_api and test_every_minted_link_lands_on_a_real_page (pass) |
| MFA required for Owners/Admins, password policy, rate limits | yes | unchanged |

## Tests changed because they encoded passkey-only claim

- `apps/api/tests/test_identity_authn_claim.py::test_a_passkey_first_claim_needs_no_password`
  → `test_a_claim_without_a_password_is_refused_and_spends_nothing` (the claim must now refuse it;
  the setup link stays usable).
- `apps/api/tests/test_identity_authn_claim.py::test_network_bridge_claim_binds_the_browsers_origin`
  — the successful network claim now sends a password.
- `apps/api/tests/test_identity_claim_to_signed_in_owner.py::test_shared_password_claim_enrols_an_authenticator_and_signs_the_owner_in`
  — dropped the `passkey-first` (no password) parameter.
- `apps/web/tests/identity-auth-and-account.test.tsx` (Claim this BOSS: setup link, shared-password
  setup link, network bridge) — type a password; expect "Claim and continue" instead of "Create a
  passkey and continue"; the passkey is created from "Protect your account", then "Skip for now".

## New tests

- `apps/web/tests/login-812-password-first-passkey-on-request.test.tsx` (11)
- `apps/api/tests/test_identity_812_never_passkey_only.py` (3)

## Not done / follow-ups

- Self sign-up still verifies by email first; its password is chosen in Enrol right after the
  factor (the API only lets a first password be set on a fresh factor). Asking it on the sign-up
  form itself needs an engine change to the pending sign-up record.
- UI change is invisible on a host until the web image is rebuilt.
