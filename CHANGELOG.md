# Changelog

All notable changes to BOSS Community. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Release notes with more detail live in `docs/community/RELEASE_NOTES_*.md`.

## v1.0-rc6.2

First-run setup without help (GitHub links and verification, deploy needs only a machine, automatic build database, step-up everywhere, claim discoverability, README quick start), opt-in CI workflow changes with Wait for CI, app traffic metrics, Architecture approval, and the Captain backoff. See docs/community/RELEASE_NOTES_v1.0-rc6.2.md.

## [v1.0-rc6.1]

Hotfix on v1.0-rc6. Notes: [docs/community/RELEASE_NOTES_v1.0-rc6.1.md](docs/community/RELEASE_NOTES_v1.0-rc6.1.md).

### Fixed

- **The console is fast again.** A page asks only for the feeds it shows (Factory 38 → 17 API calls, Overview 34 → 12), each signed-in request spends 2 identity statements instead of 4, and the Factory and Command Center send smaller payloads (283 → 134 KB, 1.16 MB → 642 KB). The console reuses an answer for up to 5 s only after the same page load proved the session live at the same identity epoch; revocation still takes effect on the next request.
- **The Infra Deploy button** returned a server error on every app target. Registering and bootstrapping a deployment target from setup had the same fault.

### Changed

- The engine's Python package version is `1.0.0rc6.post1` (PEP 440 has no `1.0.0-rc.6.1`); everything else is `1.0.0-rc.6.1`.

## [v1.0-rc6]

Notes: [docs/community/RELEASE_NOTES_v1.0-rc6.md](docs/community/RELEASE_NOTES_v1.0-rc6.md).

### Added

- **Named accounts (Identity).** Every person signs in as themselves: passkeys, password with an authenticator app, recovery codes, email links and codes (with SMTP), OIDC and SAML single sign-on, social sign-in, SCIM provisioning and self sign-up by domain. Access roles (Owner, Admin, Operator, Viewer, Auditor and custom roles), groups, project-level access, service accounts, personal API keys and effective-access explanations. See [People and access](docs/community/PEOPLE_AND_ACCESS.md) and [Sign-in setup](docs/community/SIGN_IN_SETUP.md).
- **Claiming an install.** The first visit to `/claim` creates the first Owner. `boss identity setup-link` makes a one-hour claim link.
- **Agents act for a person.** Agent Passports with a ceiling and a sponsor, delegation grants with consent, run tokens, and a revocation cascade that halts runs at once. See [Agents and access](docs/community/AGENTS_AND_ACCESS.md).
- **boss-egress.** Builds hold no GitHub or provider credential; egress adds a per-build GitHub App token (or the person's own) and enforces the rules.
- **Build sandbox.** Each build runs as its own user with a fenced network. See [Build sandbox](docs/community/SANDBOX.md).
- **Security audit log.** Append-only, hash-chained, verifiable and exportable; 400-day retention with automatic, checkpointed pruning.
- **`boss identity`** host commands: `setup-link`, `claim-status`, `recover-owner`, `host-key`, `legacy`, `posture`, `audit`, `migrate`.
- **Host tools' key** (`BOSS_HOST_API_KEY`, service account "Host tools") used by `boss`, `boss doctor`, `make metatron` and `remote.sh`.
- **API key hygiene.** Expiry reminders 14 and 3 days ahead, optional revocation of keys unused for N days.
- `boss upgrade --no-rollback`.

### Changed

- The shared console password claims the install, then signs in as Operator for 30 days after the claim (one Owner extension of up to 30 days), then stops. Every page shows the countdown.
- The shared `BOSS_API_KEY` loses people-and-access changes at the claim and stops at the same date. Responses on any legacy credential carry `Deprecation` and `Sunset` headers.
- `make mcp-setup` creates a project service account and a key pinned to that project, instead of a member bound in `BOSS_API_KEYS`.
- Access checks are enforced by default (`BOSS_AUTHZ_MODE=enforce`).
- `docker compose exec api` runs as root; pass `-u boss`.
- `boss upgrade` puts the previous release back when the new one cannot start.
- rc5 SSO providers are copied into Identity › Sign-in & SSO, switched off.
- A boss key works in `X-Boss-Api-Key` as well as `Authorization: Bearer`.
- `GET /v1/auth/session` also returns the signed-in principal, org, MFA time, CSRF token, expiry and a key's pinned projects.

### Fixed

- A wrong password no longer fails the sign-in with a server error.

## [v1.0-rc5]

Model router, governed host deployment, the redesigned console and Improve BOSS. See [docs/community/RELEASE_NOTES_v1.0-rc5.md](docs/community/RELEASE_NOTES_v1.0-rc5.md).

## Earlier

`v1.0-rc1` to `v1.0-rc4`: see the GitHub releases page.
