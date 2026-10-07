# Identity administration

This guide is for the Owners and Admins of a BOSS install. It covers the security audit log, identity posture, the identity policy, the move from shadow mode to enforcement, and the commands you run from the host shell. To upgrade an install that still uses the shared password, start with [Upgrade to named accounts](IDENTITY_UPGRADE.md).

## The security audit log

Every security-relevant change is recorded in the security audit log: sign-ins and failed sign-ins, sessions, MFA and passkeys, invitations, role and group changes, API keys, SSO and SCIM, agent delegations and tokens, policy changes, the claim, legacy credential use, and authorization denials.

Each event records who acted (the person, service account or agent, and the person an agent acted for), how they were authenticated, the IP address and user agent, what was changed, and the outcome. Nothing in the log is a secret (see [What is never logged](#what-is-never-logged)).

The log has three guarantees:

- **It is written with the change.** An event is written in the same database transaction as the change it describes. If the event cannot be written, the change does not happen.
- **It is append-only.** The database refuses to update or delete an event. The only deletion is the audited prune described below.
- **It is hash-chained.** Each event holds the SHA-256 of the previous one. A changed, removed or reordered event breaks the chain at that point, and verification names the first broken event.

There is no API that writes an event. Events come only from the code paths that make the change.

### Read, verify and export

In the console, open **Identity › Audit**. You can search free text (event, what it touched, a person's name, a grant, a token or a request id) and filter by person (an agent's events also show under the person it acted for), kind of actor, event type (`auth.*` matches a group), outcome, delegation and time.

Every API response carries an `X-Request-Id` header, and every event the request caused records the same id. Quote it when you report a problem, and search for it here.

| Action | Console | API | Who |
| --- | --- | --- | --- |
| Read the log | Identity › Audit | `GET /v1/identity/audit` | Owner, Admin, Auditor |
| Verify the chain | **Verify chain** | `POST /v1/identity/audit/verify` | Owner, Admin, Auditor (step-up) |
| Export | **Export** | `POST /v1/identity/audit/export` | Owner, Admin, Auditor (step-up) |
| Prune old events | **Prune** | `POST /v1/identity/audit/prune` | Owner (step-up) |

Verification and export are themselves recorded (`audit.verified`, `audit.exported`).

An export is a JSON bundle with every event in the range, oldest first, the result of verification, and the head of the chain (its last sequence number and hash). The bundle describes the hash, so you can re-verify it offline:

```text
hash = SHA-256(prev_hash || canonical_json(event))
canonical_json: the hashed fields, sorted keys, no whitespace, UTF-8, time as ISO-8601 UTC
```

Once a day, BOSS also writes the chain head into the activity log as an **anchor**. A chain cut short after an anchor no longer matches it.

### Retention and pruning

Events are kept for 400 days by default (`BOSS_IDENTITY_AUDIT_RETENTION_DAYS`, at least 30). Once a day BOSS prunes older events by itself, recorded as **BOSS** in the log. An Owner can prune sooner (**Prune** in Identity › Audit, step-up, or `boss identity audit prune --older-than-days N`), but never anything younger than 30 days.

A prune first writes an `audit.checkpoint` event holding the hash of the pruned range, then deletes the range in the same transaction, so the chain still verifies. A prune never deletes the record of the claim (`bootstrap.claimed`), the protection that stops the last Owner from being removed depends on it, or an earlier checkpoint.

### What is never logged

BOSS never writes these to its logs, error reports, HTTP access logs or the audit log:

- passwords, including one typed into the email field;
- authenticator codes and secrets, and recovery codes;
- session tokens, API keys (not even a prefix), run tokens and egress credentials;
- provider access and refresh tokens, OAuth codes and PKCE verifiers;
- SAML assertions and ID tokens;
- the vault master key and data keys.

One redaction filter enforces this. It is installed at the logging root of every BOSS process and removes credential headers (`Authorization`, `Cookie`, `X-Boss-CSRF`), every field named like a credential, every value shaped like a BOSS key or a JWT, the credential parameters of OAuth and SSO callback URLs, and the configured keys and passwords themselves. A failed sign-in for an unknown account stores the identifier only as an HMAC.

## The identity epoch

Any identity change (a person, a role, a binding, a group, a key, a grant, the policy) moves one number, the identity epoch, in the same transaction. The console reads it from `GET /v1/identity/me` (or `GET /v1/identity/epoch`) when it regains focus and every 30 seconds, and refreshes what you may do when it moves. `GET /v1/identity/epoch/stream` pushes it as server-sent events the moment a change commits.

## Posture

**Identity › Overview** shows the posture checks, each with a status and a link to its fix (`GET /v1/identity/posture`, or `boss identity posture`):

| Check | Red or amber when |
| --- | --- |
| MFA coverage | An Owner, Admin or Auditor has no passkey or authenticator app. |
| Stale API keys | A key has gone unused for 30 days or expires within 14. |
| Risky delegations | A delegation has no expiry or has been idle for 30 days. |
| Legacy credentials | The shared password, the shared API key, env API keys, classic GitHub tokens or the network bridge are still in use. Each shows its use count, last use and days left. |
| Orphans | A service account has no active owner, or an agent has no sponsor. |
| Audit chain | The chain was not verified in the last two days, or verification failed. |
| Public address | BOSS has no public address, so it sends no emailed links and offers no passkeys or SSO. |
| GitHub | Builds have no safe way to reach GitHub (see [GitHub for builds](#github-for-builds)). |

Legacy credentials are also listed on their own, with their cutoffs (`GET /v1/identity/legacy`).

## The identity policy

**Identity › Sign-in & SSO** edits the policy (`GET` and `PUT /v1/identity/policy`; changing it needs an Owner and step-up). Every change is recorded as `policy.changed` with the old and new values.

| Key | Default | Meaning |
| --- | --- | --- |
| `mfa_required_roles` | owner, admin, auditor | Roles whose holders must enrol MFA. |
| `session_idle_minutes` / `session_absolute_hours` | 720 / 168 | Session lifetimes. |
| `step_up_minutes` | 10 | How long a step-up stays fresh. |
| `password_min_length` | 12 | |
| `email_magic_link` / `email_code` | on | Email sign-in, effective only with SMTP. |
| `signup_enabled` / `signup_domains` | off / none | Self sign-up and its domain allow-list. |
| `public_origin` | unset | The console origin bound at claim (HTTPS only). |
| `key_default_days` / `key_max_days` | 90 / 365 | API key expiry. Owners get a reminder 14 and 3 days before a key expires (a banner, and an email with SMTP). |
| `key_auto_revoke_unused_days` | off | Revoke keys not used for this many days (counted from the last use, or from creation). Audited; the key's owner is emailed. |
| `delegation_default_days` / `delegation_max_days` | 30 / 90 | How long an agent may act for someone. |
| `legacy_cutoff_days` | 30 | Days after the claim at which the shared credentials stop working (0–30). It can only shorten the window; an Owner extends it once, by at most 30 days, per credential. |
| `network_bridge_enabled` | on | Off ends the network bridge at the claim. |

A value outside its range, or a key that does not exist, is refused with `422`. A save applies only the fields that changed, so an automatic setting stays automatic. Two fields are not switches: `smtp` reports whether email is configured, and `network_mode` can only be turned **off** (Require sign-in); network trust is never turned back on (`409`). `github_require_user_repo_access` is automatic until an Owner sets it (on exactly when more than one person is active); setting it to `null` makes it automatic again.

## From shadow mode to enforcement

Authorization is **enforced** by default. During an upgrade you can opt into **shadow mode** (`BOSS_AUTHZ_MODE=shadow`): every request is evaluated, and what would be refused is recorded but allowed. You then switch on enforcement one group of routes (a *router*) at a time, as each one shows it refuses nothing it should allow. While anything is not enforced, the *Access control* check in identity posture is red.

| Setting | Values | Effect |
| --- | --- | --- |
| `BOSS_AUTHZ_MODE` | `off`, `shadow`, `enforce` (default) | The mode for every router. |
| `BOSS_AUTHZ_ROUTER_MODES` | for example `setup=enforce,github=enforce` | Per-router overrides during the rollout. |
| `BOSS_AUTHZ_COVERAGE` | `warn`, `fail` | `fail` stops the API from starting while any route lacks a declared permission. It is the default under `enforce`. |

The identity routes themselves are always enforced, whatever the mode, unless the mode is `off`.

**The shadow report** (Identity › Overview, `GET /v1/identity/shadow-report`, or `shadow-report` from the shell) lists every router with:

- its current mode, its number of operations and any undeclared ones;
- what would have been refused in the last 7 days, by reason and by kind of caller, with the top routes;
- whether it is **ready to enforce**, and if not, why not.

A router is ready when all its routes are declared, shadow mode has run for 7 days, and every would-refusal in those 7 days is explained. To explain one, an Owner records why refusing it is correct, for example *"anonymous probes are meant to be refused"*. Explanations are audited (`POST /v1/identity/shadow-report/explain`).

The report ends with the exact `BOSS_AUTHZ_ROUTER_MODES` value that enforces every ready router. Set it in `.env` and restart the API. When every router is enforced, set `BOSS_AUTHZ_MODE=enforce` instead. Every change of mode is recorded as `authz.mode_changed`.

To step back, move a router to `shadow` in `BOSS_AUTHZ_ROUTER_MODES`, or set `BOSS_AUTHZ_MODE=off` for all of them.

Shadow refusals are counted, not logged one by one. The audit log gets one `authz.shadow_deny` event per new route, reason and kind of caller each day.

## GitHub for builds

Under `BOSS_AUTHZ_MODE=enforce` a build's sandbox holds no GitHub credential: boss-egress adds one to each git and API request and applies every rule (the run's branch only, no merges, no admin endpoints). **Setup › GitHub access** says which credential that is and sets it up. Posture check `github` shows the same answer.

| Way | What egress uses | How to set it up |
| --- | --- | --- |
| **GitHub App** (recommended) | A token per build, for that build's repositories and permissions only, revoked when the build ends. | **Create the App on GitHub**: BOSS posts an App manifest (contents and pull requests write; checks, statuses, metadata, issues and actions read; never administration or workflows). GitHub sends you back; the App's private key and client secret go into the vault. Then **Install on GitHub** and pick the repositories, or **Pick installation**. In **Repository**, **Use the GitHub App** lists the repositories it can reach; no token is pasted. |
| **As me** | The person's own GitHub connection (My account › Agents for me), only for runs whose grant says "act as me on GitHub". | Each person connects their own account. |
| **Fine-grained token** (less safe) | The token, from the vault, only for the repositories it was given. It is long-lived and acts as whoever made it. | Paste it in **Repository**; it is kept as the org's connection for the repositories you pick. |

**Moving `BOSS_GITHUB_TOKEN`.** An install that still sets `BOSS_GITHUB_TOKEN` shows **Move it into the vault** (API: `POST /v1/identity/github/token/migrate-env`). The token becomes the org's connection for the bound repositories; under enforce, the API and worker stop reading the variable at once and at every later start. Remove it from `.env` when convenient.

BOSS's own GitHub writes for a build (opening its pull request) use an App token for that one repository, bound to the build and revoked when it ends; outside a build such a token is revoked after 10 minutes.

## Passing environment variables to a build's agent

A build's agent gets a filtered environment: anything shaped like a secret (`*TOKEN*`, `*SECRET*`, `*PASSWORD*`, `*API_KEY*` …) is removed, and a fixed list of names can never pass. To let an extra, non-secret variable through (a test harness's stub URL, a feature flag the agent's tools read), name it in `BOSS_AGENT_ENV_ALLOW`, comma-separated, on the API and worker:

```bash
BOSS_AGENT_ENV_ALLOW=BOSS_E2E_MODEL_STUB_URL,BOSS_E2E_FAKES_URL
```

It cannot reopen the never-pass list, and previews strip these names again. It is for test and integration environments; do not use it to hand a build a credential (connect the credential instead, so egress holds it).

## A passkey that stops working ("counter regressed")

A device-bound passkey whose signature counter goes backwards looks like a cloned key, so BOSS refuses it, flags it in the person's passkey list and records `passkey.counter_regressed`. A synced passkey (iCloud, Google, 1Password) is flagged but still accepted. Restoring a device from a backup, or a test harness that re-creates a virtual authenticator without its counter, causes the same thing.

**The person** signs in with another factor (password with their authenticator app, or a recovery code), opens **My account › Security**, removes the flagged passkey and adds it again.

**When they have no other factor**, an Admin (or another Owner, for an Owner) opens **Identity › People ›** the person **› Reset MFA** (`user:reset_mfa`, step-up). That removes every second factor and recovery code and gives a one-time link; the person signs in and enrols again. If the person is the only Owner, run `recover-owner` from the host shell (below).

## Host-shell commands

These commands run on the machine where BOSS runs, against its database. No console page, API route or MCP tool can run them. Use the `boss` command:

```bash
boss identity <command>
```

It runs the identity CLI inside the api container as the `boss` user. Without the `boss` command, from the install directory:

```bash
docker compose -f scripts/install/community-compose.yml --env-file .env exec -u boss api \
  python -m boss_engine.identity.cli <command>
```

Always pass `-u boss`: the api container starts as root (to fence the build sandbox), and files written as root can break the `boss` user later.

| Command | What it does |
| --- | --- |
| `setup-link` | A new link to claim this BOSS, valid once for one hour. Only while the install has no Owner. |
| `host-key` | Issues a new key for the host tools (service account **Host tools**) and keeps it in `.env` as `BOSS_HOST_API_KEY`. The old one stops. With `boss identity` it is never printed. |
| `recover-owner --email E` | Makes a locked-out person an active Owner again, removes their authenticator app and recovery codes, ends their sessions, and prints a single-use link valid for 15 minutes. Recorded as `owner.recovered_by_host`. |
| `legacy status` | Lists every legacy credential with its principal, use count, last use and cutoff. |
| `legacy retire --path network_mode` | Ends a legacy credential now: `network_mode`, `shared_password` or `console_api_key`. |
| `audit verify` | Verifies the hash chain and records the result. |
| `audit export --out FILE` | Writes the export bundle to a file. |
| `audit prune [--older-than-days N]` | Prunes with a checkpoint (default: the retention setting). |
| `shadow-report [--json]` | Prints the per-router shadow report. |
| `posture` | Prints the posture checks. |
| `claim-status` | One line: `unclaimed`, or when the install was claimed and when the shared credentials stop. `boss doctor` prints it. |
| `mcp-key --project P [--permission X]` | Issues one project's MCP key. Prints it, so `boss identity` refuses it: use `make mcp-setup`, which keeps it in `.env`. |
| `egress-key [--revoke-others]` | Issues the egress service's key. Prints it; run it with the `docker compose … exec -u boss` form. |
| `seed-test-accounts --org O --owner-email E --out F [--domain D] [--i-know]` | Creates (or refreshes) one person per access role (Owner, Admin, Operator, Viewer, Auditor), the custom role `developer` with a person, and the service account `seed-ci` with a key, all as the Owner E. Passwords, TOTP secrets for roles that need MFA, and the key are written only to F, created with mode 0600; nothing secret is printed. Refused when the install looks like production (no `BOSS_ENVIRONMENT=dev/test/e2e/ci/staging` and a public origin that is not localhost or `.test`/`.local`) unless `--i-know`. Running it again gives the same accounts new credentials. Delete F when you are done. `boss identity` refuses it; use the `docker compose … exec -u boss` form. |
| `migrate` | Re-runs the mapping of legacy members, keys, grants and SCIM data. It runs by itself at startup, and running it again changes nothing that is already mapped. |

## Next steps

- [Upgrade to named accounts](IDENTITY_UPGRADE.md), for the claim and the 30-day window.
- [People and access](PEOPLE_AND_ACCESS.md) and [Sign-in setup](SIGN_IN_SETUP.md).
- [Sign in to Mission Control](CONSOLE_AUTH.md), for reaching the console and its TLS requirements.
