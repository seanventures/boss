# BOSS Community 1.0.0-rc.10.37 candidate

RC10.37 adds GitHub Enterprise Server, a CloudWatch connector, and runbooks that run every step. Model rows now say when a plan limit resets instead of "Check failed". It also closes a set of ways a GitHub token could be sent to the wrong host.

This is a test candidate. Cutting the tag does not deploy a host or publish an image.

## What is better for you

**GitHub Enterprise Server**

- A GitHub connection can point at your own GitHub Enterprise Server. These all go to that server:
  - clone and branch;
  - commits and PRs;
  - check runs;
  - signed webhooks;
  - the poller.
- Every GitHub address in BOSS now comes from one place, and a test fails the build if a new hardcoded `api.github.com` appears.
- A connection that uses its own GitHub App on your server can sign in with that App.
- `docs/operations/ENTERPRISE_NETWORK.md` has a GitHub Enterprise Server section covering addresses, a custom CA, the proxy and what has been proven so far.

**Plan limits say when they reset**

- When Claude Code or Codex hits its plan limit, the model row reads **Limited · 11:00 AM** with "Limit resets Fri 11:00 AM." in your own time zone. It no longer says "Check failed".
  - Details shows the provider's own words.
  - The row offers **Test**, not **Sign in**. Signing in would replace a working login.
  - The next check runs just after the reset.
- One limit reader now serves Setup, model routing, decisions and the build shepherd, so they can no longer disagree.
- A limited model is used again once its reset time passes. Being out of credit clears only after a successful check.
- Output from the app being built, such as a test that looks for "Weekly limit reached", is no longer mistaken for the model's own limit.

**CloudWatch and runbooks**

- A read-only CloudWatch connector reads metrics, logs and alarms. It uses secret-backed credentials or the standard AWS credential chain, and only read calls are allowed. Setup is in `docs/operations/CONNECT_CLOUDWATCH.md`.
- Runbooks run every step in order; before, only the first step ran.
  - A read-only telemetry query runs without approval.
  - A rollback or redeploy always waits for an approval and records a receipt.

**Connectors**

- Ticket and Confluence connectors check IP addresses in every form, including bracketed IPv6, IPv4-mapped and decimal/hex/octal IPv4, and block cloud metadata addresses in every spelling.
- Plain http is allowed only to the tailnet range.
- A Confluence write that timed out is checked against the version and a hash of the whole body.

**Long runs and service accounts**

- A run's token renews for as long as the run lasts. The token it replaced stops working, every renewal is audited, and renewal stops when the run ends.
- `IDENTITY_ADMIN.md` explains, step by step, how to set up a service account for unattended agents, and says which workload identities are supported.

## Security

- **A GitHub token goes only to its own host.** Every place BOSS sends a GitHub token checks the token's host first: HTTP calls, git, the egress proxy, the pre-run check and the micro-VM agent.
  - A token reaches a GitHub Enterprise Server only when it was saved through that connection's own Setup screen. BOSS stores it in a private slot that no other secret can name.
  - Every other token, from the server's environment, a file on the host, or an org-wide or project secret, goes only to the install's own GitHub.
  - A code scan fails the build if a new send point skips the check.
- An older sandbox host that cannot pass the GitHub Enterprise host to the micro-VM is never handed a token.
- Creating a secret that points at a file on the BOSS server needs org-level integration rights.

## Upgrade notes

- Run `boss upgrade` or `remote.sh update` as usual.
- **GitHub Enterprise Server connections made before this release:** if one uses a token that was not saved through the connection itself, it stops with "Re-save this connection's token". Paste the token again in Setup › GitHub access › Replace token.
- A project admin connecting a model whose key lives in the server's environment now needs org integration rights, or can paste a key for the project. The message says which.

## Known issues

- **GitHub Enterprise Server:**
  - It has been checked against a local stand-in only. A live check needs a real server.
  - Setup has no field for the server's address yet; set it through the API or install-wide.
  - A sandboxed agent's git through the egress fence still reaches github.com only.
- **CloudWatch:**
  - The live check needs read-only AWS credentials.
  - Alerts are not yet routed to runbooks automatically.
  - The console has no screen for the new runbook steps.
- **Follow-ups:**
  - A project can, in some setups, read another project's stored secret on the install's own GitHub (BOSSAPP-1489; older than this release).
  - Two hardening items for GitHub Enterprise token slots (BOSSAPP-1490).
  - A limit message with no reset time does not pause routing (BOSSAPP-1488).

## Checks

- **Reviews:** every PR had an independent review, repeated until a review found nothing serious.
  - The GitHub Enterprise work went through ten security reviews. Each round tried to send a token to a host it did not belong to. The design was narrowed until no route was left.
  - The plan-limit change went through three reviews; the gate-harness fix through one.
- **Merge gate:** each PR passed the local merge gate on the exact commit that was merged.
- **End-to-end gates:** after the GitHub Enterprise merge, the gates' own stand-in GitHub was out of date and all builds failed. It was fixed (PR #347) to use the supported setting, with a guard test so it cannot drift again. The product needed no change.
- **Enterprise lane:** passed every step on a fresh, locked-down Ubuntu 24.04 box (4 vCPU, 8 GB, 30 GB disk, AppArmor) behind an inspecting proxy with private mirrors:
  - the install took 6 minutes and left 12 GB free;
  - sandbox and Codex sandbox;
  - plumbing 20 of 20, engines 21 of 21, scripted build 3 of 3.
- **Laptop install:** plumbing 20 of 20, engines 21 of 21, golden journeys 2 of 2. The install was on the commit before the gate-harness fix, which changed test files only.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
