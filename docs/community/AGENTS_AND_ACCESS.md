# Agents and access

BOSS agents build, review and run workflows. This page explains what an agent may do, how it acts for a person, how GitHub access works for builds, and what stops when you revoke something.

## Every agent has a Passport

An agent's **Passport** (in **Identity › Agents**) says:

- its **ceiling**: the most it may ever do, whoever it works for;
- its **sponsor**: the person answerable for it;
- its **lineage**: which agents it may start, and how deep a chain may go.

An agent never does more than its ceiling. Some permissions are **never delegable**: approving decisions, managing people and access, and other critical actions stay with people. **Agents for me › What agents never get** lists them.

On a Passport an admin can:

| Action | Effect |
| --- | --- |
| Edit the ceiling | Saved after a review of what changes. |
| **Suspend** | Halts its runs and pauses its grants. **Resume** restarts them. |
| **Revoke for good** | Ends the Passport. Re-issuing creates a new one. |

## Agents act for a person

When an agent works for you, it acts **on your behalf**: it can do only what **you**, **its ceiling** and **your grant** all allow, at that moment. If any of the three shrinks, so does the agent, on its next call.

### Let an agent act for you

**My account › Agents for me › Let an agent act for me**: choose the agents, what they may do (abilities such as *Build and steer code*, *Run workflows*, *Push branches and open pull requests*), which projects, and an expiry. You cannot grant what you do not hold.

When you start a build or a run that needs a grant you have not given, BOSS asks for it on the spot. The grant can last for that one build only.

Grants last 30 days by default (90 at most). An Owner can allow grants that last until revoked.

### What a run carries

A running agent holds a short-lived token for that one run, bound to its process. Tokens are not stored in the build, and they die when the run ends or the grant is revoked.

## Revoking stops things at once

| You do | What stops |
| --- | --- |
| Revoke a grant | Its agents' tokens die, their runs halt, schedules on it pause, queued jobs are cancelled. |
| Suspend or revoke a Passport | That agent's runs halt and its grants pause. |
| Disable a person | Their sessions and keys stop, and every grant they gave is suspended: runs for them halt. |
| A grant expires | The same as revoking it. |

Before you revoke, BOSS shows what will stop. Every step is in the audit log.

**Identity › Agents › Delegations** shows every grant, who gave it, its chain and its tokens.

## GitHub access for builds

A build never holds a GitHub credential itself. **boss-egress**, a separate service, adds one to each git and API request and applies the rules: the build's own branch only, no merges, no admin endpoints. **Setup › GitHub access** says which credential it uses.

| Way | What egress uses | Set up |
| --- | --- | --- |
| **GitHub App** (recommended) | A token per build, for that build's repositories only, revoked when the build ends. | **Create GitHub App**, then **Install on GitHub** and pick repositories. Nothing to paste. |
| **As me** | Your own GitHub connection, only for runs whose grant says "act as me on GitHub". | **My account › Agents for me › Connections › GitHub**. |
| **Fine-grained token** (less safe) | A token kept in the vault, for the repositories it was given. It is long-lived and acts as whoever made it. | Paste it in **Repository**. |

An older install that sets `BOSS_GITHUB_TOKEN` shows **Move it into the vault**. Details: [Identity administration](IDENTITY_ADMIN.md#github-for-builds).

## Egress: what a build can reach

A build runs as its own user, with a fenced network (see [Build sandbox](SANDBOX.md)). It can reach:

- the BOSS API;
- boss-egress, which forwards to GitHub, to model providers and to package registries on an allowlist.

Everything else is refused. Egress records each request (allowed or denied) for 30 days; denials also go to the security audit log.

## Next steps

- [People and access](PEOPLE_AND_ACCESS.md): roles, groups and keys.
- [Build sandbox](SANDBOX.md): how builds are isolated.
