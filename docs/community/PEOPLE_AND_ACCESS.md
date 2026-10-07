# People and access

Everyone who uses BOSS signs in as themselves. What they can see and do comes from their **access role**, at the whole install or in one project. This page covers people, roles, groups, service accounts and keys, and your own account.

Open **Identity** in the console. Its tabs are **Overview**, **People**, **Groups**, **Access roles**, **Service accounts**, **Agents**, **Sign-in & SSO** and **Audit**. You see only the tabs your role allows.

## Access roles

Five roles come with BOSS. They cannot be edited.

| Role | What it is for |
| --- | --- |
| **Owner** | Everything a person can hold. There is always at least one active Owner. |
| **Admin** | Runs the install: people, roles, agents, connections, infra, policy. |
| **Operator** | Does the work: builds, workflows, panels, research, approvals within their seat, previews, non-production deploys. Lets agents act for them. |
| **Viewer** | Reads every non-sensitive page. |
| **Auditor** | Viewer, plus the security audit log and read-only identity settings. |

Owners, Admins and Auditors must have a second factor (a passkey or an authenticator app). An Owner can change which roles need one in **Identity › Sign-in & SSO**.

### Custom roles

In **Access roles**, open a built-in role and clone it, then add or remove permissions. Before you save, **Impact preview** shows who gains or loses what. Saving needs step-up (a fresh second factor). Every save is a new version, and you can go back to an older one.

You can only add permissions you hold yourself.

## People

### Invite people

**People › Invite people**: enter one or more emails and pick an access role. With email (SMTP) set up, BOSS sends each person a link. Without it, BOSS shows the links once; hand them over yourself.

Each invite shows as **Invited** until it is accepted. The person sets a password (and a second factor if their role needs one), or signs in with single sign-on.

People can also join through:

- single sign-on, when the provider has **Create people on first sign-in** on (see [Sign-in setup](SIGN_IN_SETUP.md));
- SCIM, from your identity provider;
- self sign-up, when an Owner allows it for some email domains.

### Give access

Open a person, then **Add access**: pick a role and **the whole install** or **one project**. A person can hold several roles; they get the sum.

Access can have an end date. It then shows *expires* next to the role and stops by itself. The API sets it (`expires_at` on `POST /v1/identity/role-bindings`).

**Effective access** on a person's page shows every permission they hold and why: which role, at which scope, through which group.

### Disable, reset, sign out

On a person's page:

| Action | What happens |
| --- | --- |
| **Disable** | All their sessions end and their keys are suspended. Grants they gave agents are suspended, and runs for them halt. **Enable** lets them sign in again. |
| **Reset MFA** | Removes their second factors and recovery codes. They enrol again at next sign-in. Every admin is told. |
| **Sign out everywhere** | Ends every session on every device. |

A placeholder person from an older install (for example `cto`) shows the role it maps to. Invite the real person into it.

## Groups

A group is a set of people who share access roles. In **Groups**, create a group, bind roles to it, and add members. Members get the group's roles on top of their own.

Groups from SCIM or single sign-on are read-only here: change them in your identity provider.

## Service accounts and keys

A **service account** is an account for CI, scripts or integrations. In **Service accounts › New service account**:

1. Name it and say what it is for. You become its owner.
2. Give it a role, at the install or in one project.
3. **Create key**: pick a preset (**Read-only**, **CI deploy**, **MCP client**) or each scope yourself. Scopes never exceed what the account holds. You can limit a key to some projects and some networks, and give it an expiry.
4. Copy the key. **It is shown once.** After that BOSS shows only its id.

**Rotate key** issues a new key; both work until the overlap ends. **Revoke key** stops it at once.

Two service accounts are made for you:

- **Host tools**: the key the `boss` command and other tools on the BOSS machine use (`BOSS_HOST_API_KEY` in `.env`). You can narrow its role or revoke its key; the next `boss` command issues a new one.
- **MCP (project)**: one per project, made by `make mcp-setup` (see [MCP](MCP.md)).

A service-account key may use the sensitive (step-up) permissions in its scopes. A personal key never can.

### Key reminders and clean-up

- 14 and 3 days before a key expires, its owner sees a banner, and gets an email when SMTP is set up.
- An Owner can set **Revoke keys unused for N days** (`key_auto_revoke_unused_days`). BOSS then revokes any key not used for that long (counted from its last use, or from when it was made), records it in the audit log, and emails the owner.
- **Identity › Overview** flags keys unused for 30 days or expiring soon, and service accounts with no owner.

## My account

Open the account menu (top right). Your pages are:

| Page | What is there |
| --- | --- |
| **Security** | Your password, passkeys, authenticator app, recovery codes and linked single sign-on. |
| **Sessions** | Where you are signed in. End any of them. |
| **Agents for me** | Agents you let act for you, and your connections (such as GitHub). See [Agents and access](AGENTS_AND_ACCESS.md). |
| **API keys** | Your personal keys, for the CLI or an MCP client. |

### Passkeys and the authenticator app

- **Passkey**: sign in with your device (Touch ID, Windows Hello, a security key). Add more than one.
- **Authenticator app**: **Set up an authenticator**, scan the code, type a code to confirm.

If your role needs a second factor and you have none, a banner says so on every page until you add one.

### Recovery codes

Recovery codes let you in when you lose your second factor. Each works once. BOSS shows them once: save them somewhere safe.

When fewer than 3 are left, every page says so; **Make new codes** replaces them all. Signing in with a recovery code that leaves fewer than 3 takes you straight to **Security**.

### Personal API keys

**API keys › New personal key**. A personal key acts as you, with your access, narrowed to the scopes you pick. It always expires. It can never use the sensitive (step-up) permissions, even if you hold them. Copy it when it is shown; it is shown once.

## Banners

These show at the top of every page while they apply:

| Banner | Means |
| --- | --- |
| Add a second factor | Your role needs one. |
| Fewer than 3 recovery codes | Make new ones. |
| Your API key expires in N days | Rotate or replace it. |
| Signed in with the shared password | It stops on the date shown. Ask an Owner to invite you. |
| Network sign-in ends | Network-trusted access ends on the date shown. |
| An Owner account was recovered from the host shell | Shown to Owners for 7 days. Check **Identity › Audit**. |

## Next steps

- [Sign-in setup](SIGN_IN_SETUP.md): email, single sign-on, SCIM.
- [Agents and access](AGENTS_AND_ACCESS.md): what agents may do, and acting for you.
- [Identity administration](IDENTITY_ADMIN.md): the audit log, posture and policy.
