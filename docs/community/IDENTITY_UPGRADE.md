# Upgrade to named accounts

This release replaces the shared console password with named accounts: each person signs in as themselves, with a passkey or a password plus a second factor, and holds an access role (Owner, Admin, Operator, Viewer or Auditor). This page explains what happens to an existing install when you upgrade, what you do once, and when the old shared credentials stop working.

**Nothing locks you out at upgrade.** Every credential that works today keeps working after the upgrade. The old ones keep working until a date that starts counting only when you claim the install.

## What happens when you upgrade

The upgrade itself changes nothing you can see. On the first request after it starts, BOSS maps what exists today onto the new model:

| Today | After the upgrade |
| --- | --- |
| The shared console password (`BOSS_CONSOLE_PASSWORD`) | The credential that proves you are the operator when you claim the install. |
| The console's API key (`BOSS_API_KEY`) | A legacy principal, **Legacy console key**, with Owner-level access until the cutoff. |
| Other API keys (`BOSS_API_KEYS` with `BOSS_API_KEY_PRINCIPALS`) | Legacy keys, stored as hashes. Each one keeps exactly the access of the member it was bound to. |
| The worker's key | Unchanged. It keeps its access until the worker moves to per-job credentials. |
| Members such as `ceo`, `cto` and `cao` | Placeholder people, *pending*, holding exactly what that member could do. |
| Approver roles (`ceo`, `cao`) | Approval seats held by those placeholders. |
| Active break-glass grants | Time-boxed access that expires when the grant did. |
| SCIM users and groups | Pending people (with their email) and SCIM groups. Old SCIM bearer tokens keep working until you rotate them. |
| A network-trusted console (`BOSS_CONSOLE_AUTH=network`) | The **network bridge**: the console behaves exactly as today until you claim the install. |

Authorization runs in **shadow mode** by default. BOSS evaluates every request against the new roles and records what it *would* refuse, but refuses nothing. You can read that record in **Identity › Overview** or with `shadow-report` (see [Identity administration](IDENTITY_ADMIN.md)).

## Claim the install (once)

Claiming creates the first Owner: you. Open the console and choose **Claim this BOSS**, then:

1. **Password installs:** enter the shared console password as proof that you are the operator.
   **Network-trusted installs** (no password, reached over a private network such as a tailnet): the network trust that already makes you the operator is the proof. The claim screen shows the address it will bind to, for example `https://boss.your-tailnet.ts.net`. Confirm it.
2. Enter your name and email.
3. Register a passkey. On a password install you can use a password plus an authenticator app instead.

The claim works once. After it, the claim page answers *not found*.

When you claim, you can take over a legacy member such as `ceo`. Old records then show your name, for example **Param (was: ceo)**, and the `ceo` approval seat moves to you.

!!! note "No environment change, no log reading"
    On a network-trusted install the claim needs neither a setup link nor `BOSS_PUBLIC_URL`. The address you confirm becomes the passkey and CSRF origin. A `BOSS_PUBLIC_URL`, when set, takes precedence.

## The 30-day window

The cutoff counts from the moment you claim:

| Credential | During the 30 days | After |
| --- | --- | --- |
| Shared console password | Signs in as **Shared console password**, with the **Operator** role. | Refused. |
| Network-trusted access | Visitors act as **Network operator**, with the **Operator** role. | Refused. Visitors see the sign-in page. |
| `BOSS_API_KEY` | Unchanged (Owner-level). | Refused, unless an Owner keeps it explicitly (posture shows it red). |
| Other env API keys and the worker's key | Unchanged. | Unchanged, until you rotate each one into a service-account key. |
| OIDC bearer callers | Unchanged. | Unchanged, until the legacy paths are removed in a later release. |

The **Operator** role can start builds, run workflows, approve within its seat and deploy outside production. It cannot change settings, people or policy.

Everyone using a legacy credential sees a countdown banner from 14 days before the cutoff.

!!! warning "The console key waits for the console"
    If the console still sends `BOSS_API_KEY` at the cutoff and nobody has signed in with a named account in the last day, BOSS does not retire the key: doing so would lock the console out. The key then shows red in posture until an Owner signs in with their own account, after which the key is retired at the next check.

## During the window

- **Invite your team.** In **Identity › People**, invite each person and choose their role. A placeholder such as `cto` shows the role it maps to.
- **Move each automation to a service account.** Create a service account, give it a key with only the permissions it needs, and replace the env key in the client. Posture lists every legacy key with its use count and last use.
- **Point MCP clients at personal keys.** In **My account › API keys**, create a personal key and replace the shared key in the client configuration.
- **Close the network bridge early.** An Owner can end it at once with **Require sign-in** (Identity › Overview). The **Network operator** account is then disabled, and its history keeps its name.

## Extend or roll back

- **Extend a cutoff.** An Owner can move a legacy credential's cutoff, up to 90 days ahead. The extension needs step-up and is recorded in the security audit log. A credential that has already stopped working cannot be brought back.
- **Change the default window.** The policy key `legacy_cutoff_days` (0–90, default 30) moves every live cutoff to claim time plus the new number of days.
- **Turn authorization off.** `BOSS_AUTHZ_MODE=off` in `.env`, then restart, disables the new checks entirely. The identity API itself stays protected. Use this only to recover from a misconfigured rollout.

## If you are locked out

If the only Owner loses their passkey and second factor, an operator with shell access to the machine can recover them:

```bash
docker compose exec api python -m boss_engine.identity.cli recover-owner --email you@example.com
```

The command makes that person an active Owner, removes their authenticator app codes and recovery codes, ends their sessions, and prints a single-use link that expires after 15 minutes. It is recorded in the security audit log as **BOSS host CLI**, and every Owner sees a banner about it for 7 days. Nothing in the console or the API can run it.

## Next steps

- [Identity administration](IDENTITY_ADMIN.md): the audit log, posture, policy and the rollout to enforcement.
- [Sign in to Mission Control](CONSOLE_AUTH.md): reaching the console and the TLS requirements.
