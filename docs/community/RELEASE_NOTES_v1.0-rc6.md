# BOSS Community 1.0.0-rc.6 (draft)

!!! warning "Draft"
    These notes describe a release that is not published yet. Details may change before `v1.0-rc6` is tagged.

Sixth Community release candidate. It follows `v1.0-rc5`. The headline is **named accounts**: every person signs in as themselves.

## Install or upgrade

```sh
curl -fsSL https://boss.sean.network/install.sh | bash
```

Already installed: `boss upgrade`. There is nothing else to run by hand. If the new release cannot start on your machine, `boss upgrade` puts the previous one back.

## Named accounts (Identity)

**What it is.** Each person has their own account and signs in with a passkey, or a password plus an authenticator app, or single sign-on. What they can see and do comes from an access role (Owner, Admin, Operator, Viewer, Auditor, or a custom role), at the whole install or one project.

- **People, groups and roles** in **Identity**: invite by email, groups, custom roles with an impact preview before you save, and **Effective access** that says why someone can do something.
- **Single sign-on**: OIDC and SAML (Keycloak, Okta, Microsoft Entra ID, Google), social sign-in (GitHub, Google, Microsoft, Apple), SCIM provisioning, and **Require it for domains**.
- **Email sign-in** with SMTP: one-time links and codes, password resets.
- **Service accounts and keys** for CI and scripts, and **personal API keys** for the CLI and MCP. Keys are shown once, expire, and can be limited to some permissions, projects and networks.
- **Agents act for a person**, never beyond what that person, the agent's Passport and the person's grant all allow. Revoking a grant, a Passport or a person halts the runs at once.
- **Builds hold no GitHub credential.** boss-egress adds a per-build GitHub App token (or your own, "as me") to each allowed request.
- **A security audit log** that is append-only, hash-chained, verifiable and exportable. Kept 400 days.
- **A build sandbox**: each build runs as its own user with a fenced network.

Guides: [People and access](PEOPLE_AND_ACCESS.md), [Sign-in setup](SIGN_IN_SETUP.md), [Agents and access](AGENTS_AND_ACCESS.md), [Build sandbox](SANDBOX.md), [Identity administration](IDENTITY_ADMIN.md).

## Upgrading an existing install

Full details: [Upgrade to named accounts](IDENTITY_UPGRADE.md).

1. `boss upgrade`.
2. Open the console and **claim** it: the password from `boss password` (or a one-hour link from `boss identity setup-link`) proves it is you. You become the first Owner.
3. Invite your team in **Identity › People**.
4. Back up the new `boss_community_identity` volume with your database: it holds the encryption key.

## What changes for existing users

- **The shared console password** claims the install. After the claim it still signs in, with no email, as **Operator**, for 30 days, with a countdown on every page. An Owner can extend it once by up to 30 days. Then it stops.
- **The shared API key** (`BOSS_API_KEY`) loses people-and-access changes at the claim and stops at the same date. Its responses carry `Deprecation` and `Sunset` headers until then.
- **The `boss` command, `boss doctor`, `make metatron` and `remote.sh`** now use their own key, `BOSS_HOST_API_KEY`, issued for you. `boss identity host-key` makes a new one.
- **MCP**: run `make mcp-setup` again. It gives your client a service account and a key for one project.
- **Network-trusted installs** (`BOSS_CONSOLE_AUTH=network`) work as before until the claim, then as Operator for at most 30 days.
- **Access checks are enforced.** Set `BOSS_AUTHZ_MODE=shadow` to try the roles first: BOSS then logs what it would refuse and refuses nothing.
- **`docker compose exec api` runs as root.** Add `-u boss`, or use `boss identity`.
- **SSO providers from rc5** are copied in, switched off. Add the client secret and turn them on.
- **New command:** `boss identity` (`setup-link`, `claim-status`, `recover-owner`, `host-key`, `legacy status`, `posture`, `audit`).

## Also in this release

- `boss upgrade` rolls back to the previous release when the new one cannot start (`--no-rollback` to keep it).
- A host that cannot fence the build sandbox (rootless Docker, gVisor, some WSL2 and VPS kernels) still starts: builds are held, and `boss doctor` says how to fix it.

## Known limits

- No CSV import or export of people. SCIM is the bulk path.
- No group auto-join, and no blocklist of single email addresses (domain lists only).
- No SMS codes and no CAPTCHA, by design: email and authenticator codes, passkeys, and a per-source backoff instead.
- One organisation per install, with projects inside it.
- No identity webhooks. Use the audit export.
- No periodic access reviews for agents yet. Posture flags stale keys and risky grants.
- The build sandbox's network fence on Docker Desktop for macOS is not yet verified.
- SAML and SCIM endpoints are served by the API: route `/saml/` and `/scim/` to it in your HTTPS proxy.
- Installer follow-ups: `BOSS_PUBLIC_URL` means the API address to the installer but the console address to sign-in (use `--console-origin`); the installer does not yet print the claim link at the end (use `boss identity setup-link`).
- Carried over from rc5: the paused capabilities listed in the [rc5 notes](RELEASE_NOTES_v1.0-rc5.md#known-issues).
