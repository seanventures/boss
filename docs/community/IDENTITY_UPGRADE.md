# Upgrade to named accounts

This release replaces the one shared console password with named accounts. Each person signs in as themselves, with a passkey or a password plus an authenticator app, and holds an access role: Owner, Admin, Operator, Viewer or Auditor.

This page explains what happens when you upgrade an existing install, the one thing you do (claim it), and when the old shared credentials stop.

## In short

1. Run `boss upgrade`. Nothing else to run by hand.
2. Open the console. It says **This BOSS has no Owner yet. Claim it.** Claim it: you become the first Owner.
3. Invite your team in **Identity › People**.
4. Your teammates can keep signing in with the shared password for 30 days, as **Operator**. Every page shows how many days are left.
5. The `boss` command, `boss doctor`, `make metatron` and `remote.sh` keep working: they now use their own key.

## What the upgrade does by itself

On first start the new release sets up everything it needs. You do not edit `.env`.

| What | What happens |
| --- | --- |
| Database | New tables are added. Nothing is removed. |
| Your members (`ceo`, `cto`, …) | Become *pending* people, holding exactly what that member could do. Approver seats move with them. |
| Active break-glass grants | Become access that expires when the grant did. |
| SCIM users and groups | Become pending people (with their email) and SCIM groups. Old SCIM bearer tokens keep working until you rotate them. |
| SSO providers you set up in rc5 | Copied to **Identity › Sign-in & SSO**, switched **off**. Add the client secret and turn each one on. |
| Encryption key | Created on the new `boss_community_identity` volume. **Back it up with the database** (see below). |
| Egress key, server key | Created automatically. |
| Host tools' key | `boss upgrade` issues `BOSS_HOST_API_KEY` into `.env` (see [Host tools](#host-tools-cli-and-scripts)). |

If the new release cannot start on your machine, `boss upgrade` puts the previous release back and starts it again. Your data stays. Pass `--no-rollback` to leave it as it is.

!!! note "Build sandbox"
    Builds now run as their own user with a fenced network. If your host cannot install that fence (rootless Docker, gVisor, some WSL2 and VPS kernels), BOSS still starts but holds builds until it is fixed. `boss doctor` says so. See [Build sandbox](SANDBOX.md).

## Claim the install (once)

Claiming creates the first Owner: you. Open `/claim` on your console address (for example `http://127.0.0.1:3000/claim`), or click **Claim it** on the sign-in page.

1. **Prove you run this BOSS.** One of:
    - the shared console password: `boss password` shows it;
    - a setup link: `boss identity setup-link` makes one that works once, for one hour (the api log prints one too);
    - on a network-trusted install (`BOSS_CONSOLE_AUTH=network`): confirm the `https://` address the claim screen shows. It becomes your passkey and sign-in address.
2. Enter your name and email.
3. Add a passkey, or a password plus an authenticator app.

You can also take over a legacy member such as `ceo`. Old records then show your name, for example **Sam (was: ceo)**, and the `ceo` approval seat moves to you.

The claim works once. `boss doctor` and `boss identity claim-status` say whether the install is claimed, and when the shared credentials stop.

**Before the claim**, the shared password only claims. Signing in with it says *This BOSS has no Owner yet. Claim it first.*

!!! note "Safari and plain http://localhost"
    Sign-in cookies are always `Secure`. Chrome, Edge and Firefox accept them on `http://localhost`; Safari does not. On a laptop install use another browser, or put the console behind HTTPS. An install reached from another machine must use HTTPS anyway (see [Sign in](CONSOLE_AUTH.md)).

## What changes for everyone else

### Teammates who use the shared password

After the claim, the shared password still signs in, for **30 days from the claim**:

- On the sign-in page, choose **Use the shared password**. No email is needed.
- They are signed in as **Shared console password**, with the **Operator** role: they can run builds and workflows, approve decisions and merge. They cannot change people, access or settings.
- Every page shows a banner: *You're signed in with the shared password. It stops working in N days. Ask an Owner to invite you.*
- A session never lasts past the end date.

Invite each of them in **Identity › People** before the end date. After it, the shared password is refused: *The shared password no longer works. Ask an Owner to invite you.*

### The shared API key (`BOSS_API_KEY`)

| When | What it can do |
| --- | --- |
| Before the claim | Everything an Owner can. |
| From the claim | Everything except changing people and access (people, groups, roles, keys, service accounts, agent delegations, sign-in providers, the sign-in policy). |
| After the end date | Nothing. It is refused. |

Every response to a request made with a shared or legacy credential carries two headers, so scripts can see the end coming:

```text
Deprecation: true
Sunset: Thu, 05 Nov 2026 12:00:00 GMT
```

This applies to the shared API key, other keys in `BOSS_API_KEYS`, network-trusted access and shared-password sessions.

### Other API keys (`BOSS_API_KEYS`)

- A key bound to a **person** member (for example `cto`) keeps that member's access and stops at the same end date. Replace it with a personal key (**My account › API keys**) or a service-account key.
- The **worker's** key (`BOSS_WORKER_API_KEY`) keeps working. It has no end date.

### Host tools (CLI and scripts)

The `boss` command, `boss doctor`, `make metatron`, `remote.sh` and the model and subscription helpers no longer use the shared key. They use their own key, `BOSS_HOST_API_KEY` in `.env`:

- It belongs to the service account **Host tools**, with the role **Host tools**: everything the shared key could do except changing people and access. It has no end date.
- `boss` issues it for you when it is missing or refused, and after `boss upgrade`.
- `boss identity host-key` issues a new one. The old one stops at once.
- Owners see it in **Identity › Service accounts**. They can narrow the role or revoke the key; the next `boss` command issues a new key.

### MCP clients

Run `make mcp-setup` again. It gives your agent client its own key:

- a service account **MCP (your-project)**, with access to that one project only;
- a key pinned to that project, kept in `.env` as `BOSS_MCP_API_KEY`.

An rc5 MCP key is replaced and unbound (the API restarts once to drop it). `make mcp` then starts the MCP server with only the new key. See [MCP](MCP.md).

A person can also use a personal key (**My account › API keys**) in a remote MCP or API client.

### Network-trusted installs (`BOSS_CONSOLE_AUTH=network`)

Before the claim, the console works as it did. After the claim, visitors act as **Network operator**, with the **Operator** role, for at most 30 days. A banner shows the end date. After it, visitors see the sign-in page.

## Timeline

| Credential | Before the claim | 30 days from the claim | After |
| --- | --- | --- | --- |
| Shared console password | Claims the install only | Signs in as Operator (no email) | Refused |
| `BOSS_API_KEY` | Owner | Owner minus people and access | Refused |
| Person keys in `BOSS_API_KEYS` | As the member | As the member | Refused |
| Worker key | Worker | Worker | Worker |
| Network-trusted access | As today | Operator | Sign-in page |
| `BOSS_HOST_API_KEY` | Host tools | Host tools | Host tools |

## Extend or end early

- **Extend once.** An Owner can move the end date once, by at most 30 days, so at most 60 days after the claim. It needs step-up and is recorded in the audit log (`POST /v1/identity/legacy/{path}/extend`). A credential that has already stopped cannot be brought back.
- **Shorten the window.** The policy setting `legacy_cutoff_days` (0–30, default 30) sets the window for every shared credential. It can only make it shorter.
- **End now.** **Require sign-in** in **Identity › Sign-in & SSO** ends network-trusted access at once. From the host: `boss identity legacy retire --path shared_password` (or `console_api_key`, `network_mode`).
- **See what's still in use.** `boss identity legacy status`, or **Identity › Overview**: each shared credential with its use count, last use and days left.

## Back up the encryption key

Named accounts keep some secrets encrypted: authenticator-app secrets and connected accounts. Their key is **not** in the database. It is the file `/var/lib/boss/identity/master.key` on the `boss_community_identity` volume.

Back up that volume with the database. Without it, people re-enrol their authenticator apps and reconnect GitHub. With a different key, BOSS refuses to start the vault rather than guess.

To bring your own key, set `BOSS_MASTER_KEY_FILE` (or `BOSS_MASTER_KEY`: 32 bytes, base64) and `BOSS_MASTER_KEY_AUTOCREATE=0`.

## Trial mode

Access checks are **enforced** by default. To try the new roles first, set `BOSS_AUTHZ_MODE=shadow` in `.env` and restart: BOSS records what it *would* refuse but refuses nothing. Posture shows this in red. See [Identity administration](IDENTITY_ADMIN.md#from-shadow-mode-to-enforcement).

## Troubleshooting

| You see | Do this |
| --- | --- |
| *This BOSS has no Owner yet* | Claim it at `/claim`. |
| *Claim it first* when using the shared password | Same: the shared password only claims until someone does. |
| *The shared password no longer works* | Ask an Owner to invite you. |
| A script gets `401` after the end date | Give it a personal or service-account key. Host tools: run `boss identity host-key`. |
| Builds say *Sandbox unavailable* | See [Build sandbox](SANDBOX.md). |
| `boss upgrade` says *Rolled back* | The previous release is running again. Read the error above it, then see the [installer guide](https://github.com/seanventures/boss/blob/main/scripts/install/README.md). |

### The only Owner is locked out

Someone with a shell on the BOSS machine can recover them:

```bash
boss identity recover-owner --email you@example.com
```

This makes that person an active Owner and treats the account as compromised: it removes their authenticator app, passkeys and recovery codes, ends all their sessions, revokes all their personal API keys, and prints a link that works once, for 15 minutes. It is recorded in the audit log as **BOSS host CLI**, and every Owner sees a banner about it for 7 days. Nothing in the console or the API can run it.

## Next steps

- [People and access](PEOPLE_AND_ACCESS.md): invite people, roles, groups and keys.
- [Identity administration](IDENTITY_ADMIN.md): the audit log, posture, policy and host commands.
- [Sign in](CONSOLE_AUTH.md): sign-in methods, and reaching the console over HTTPS.
