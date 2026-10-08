# BOSS Community 1.0.0-rc.6

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
- **Host credentials stay with their provider.** A provider key or token the install holds (in `.env`, an org-scoped secret, the org's GitHub connection) is sent only to that provider's own endpoints, plus those the operator configures. Every outbound request is checked, and a refusal names the credential and the host. If you use **GitHub Enterprise**, a **self-hosted Plane or Jira**, or an **OpenAI-compatible gateway** with a key from `.env`, set its URL on the host (`BOSS_GITHUB_API_URL`, `PLANE_BASE_URL`, `JIRA_BASE_URL`, `OPENAI_BASE_URL` …) or list it in `BOSS_CREDENTIAL_HOSTS`. An org-scoped secret can list its own hosts in `metadata.allowed_hosts`. See [Host credentials](IDENTITY_ADMIN.md#host-credentials-what-a-reference-may-name-and-where-it-may-be-sent).
- **Naming a host credential is an org act.** Creating an `env:` reference or an org- or workspace-scoped reference needs `integration:manage` for the organisation. An `env:` reference may name only the provider credentials BOSS knows, plus what the operator lists in `BOSS_SECRET_REF_ENV_ALLOW`. It can never name BOSS's own secrets.
- **Job sinks from before rc6 are held.** An event subscription that queues a job queues nothing until someone who holds that job kind's permission re-saves or re-enables it. See [Job sinks set up before rc6](IDENTITY_ADMIN.md#job-sinks-set-up-before-rc6).
- **PRD auto-approve is off under enforcement.** Unless you set `BOSS_MEETING_PRD_AUTO_APPROVE`, a meeting's PRD waits for a person's approval on an enforced install, and is approved automatically only under `shadow` or `off`. Set it to `true` to keep the old behaviour.
- **Webhook secrets** can no longer name a provider's credential (`env:BOSS_GITHUB_TOKEN`). Give each webhook a secret of its own.
- **New command:** `boss identity` (`setup-link`, `claim-status`, `recover-owner`, `host-key`, `legacy status`, `posture`, `audit`).

## Also in this release

- `boss upgrade` rolls back to the previous release when the new one cannot start (`--no-rollback` to keep it).
- A host that cannot fence the build sandbox (rootless Docker, gVisor, some WSL2 and VPS kernels) still starts: builds are held, and `boss doctor` says how to fix it.

## Also fixed in this candidate

- Google sign-in can claim an invitation for a Gmail address (gmail.com, googlemail.com) or for the Workspace domain Google vouches for, without listing that domain on the provider. It never creates an account by itself.
- BOSS's own read-only repository reader keeps its Passport: people can suspend or revoke it, but not change its limits.
- Sessions and key last-used show tailnet addresses (100.64.0.0/10 and Tailscale IPv6) again.
- Sign-in through Google (and any identity provider) works on hosts with no IPv6 route: outbound calls try each checked address, IPv4 first.
- The Add provider sheet shows the redirect URL before you save, with a short setup guide for Google, GitHub, Microsoft, Okta, OIDC and SAML.
- A host credential (GitHub, cloud and model keys) is only ever sent to its own provider's servers, on every outbound path; naming one in a reference needs an org-level permission.
- Identity notices can be closed (or acknowledged, for security notices), stay closed on every device, and fold into one card.
- Every "New build" button opens the New project screen; planning with an agent is an option there.

## Known limits

- No group auto-join, and no blocklist of single email addresses (domain lists only).
- No SMS codes and no CAPTCHA, by design: email and authenticator codes, passkeys, and a per-source backoff instead.
- One organisation per install, with projects inside it.
- No identity webhooks. Use the audit export.
- No periodic access reviews for agents yet. Posture flags stale keys and risky grants.
- The build sandbox's network fence on Docker Desktop for macOS is not yet verified.
- SAML and SCIM endpoints are served by the API: route `/saml/` and `/scim/` to it in your HTTPS proxy.
- Installer follow-ups: `BOSS_PUBLIC_URL` means the API address to the installer but the console address to sign-in (use `--console-origin`); the installer does not yet print the claim link at the end (use `boss identity setup-link`).
- The host-credential check does not inspect separate processes (git, the Claude and Codex command-line agents, a build's own code), streamed or compressed request bodies, or bodies over 4 MB. Those get credentials only through egress and the filtered agent environment.
- Carried over from rc5: the paused capabilities listed in the [rc5 notes](RELEASE_NOTES_v1.0-rc5.md#known-issues).
