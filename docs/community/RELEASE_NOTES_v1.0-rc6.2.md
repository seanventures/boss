# BOSS Community 1.0.0-rc.6.2

A setup-experience release on top of `v1.0-rc6.1`: a new install goes from install to a first deployed app without help. It also adds the first capabilities needed for full-stack apps with CI and monitoring.

## Install or upgrade

```sh
curl -fsSL https://boss.sean.network/install.sh | bash      # new install
boss upgrade                                                # existing install
```

## First-run setup

- **GitHub that just works:**
  - one place builds every GitHub link, correct for personal accounts and organisations;
  - setup checks what GitHub actually granted ("Installed on @you", "Workflows: accepted ✓") and links to exactly what's missing;
  - the GitHub App can be created from a laptop, a tailnet name or a private IP;
  - each install's App gets its own name.
- **Deploying needs only a machine:**
  - a workflow's deploy step asks only where;
  - the first deploy creates the target itself: a free port, the URL, health checks and the host's key;
  - builds write the deploy manifest;
  - the approval card shows the plan, with Edit.
- **A database for builds, automatically:** when the brief, a template, the plan or the code needs PostgreSQL, each build gets its own temporary database (an Auto / Always / Never override is in Setup).
- **Confirm it's you everywhere:** every action that needs a fresh second factor opens "Confirm it's you" and continues, never a raw error.
- **Claiming a new install** is shown by `boss open`, `boss doctor` and the sign-in page (`boss identity setup-link`).
- **A token entered in the Repository row** reaches build access, or says plainly why not.
- **Small things:**
  - the "add BOSS's key to the server" step can name your key file;
  - workflow warnings no longer block Save, and "Needs Deploy set up" links to the fix;
  - the README quick start was rewritten from install to first deploy.

## New capabilities

- **GitHub Actions CI, opt-in per repo:**
  - builds may add or change CI workflows only when allowed;
  - every such PR is flagged "CI changed" and needs a person's approval;
  - CI changes are checked for secret leaks, unsafe triggers and unpinned actions;
  - a new **Wait for CI** step blocks the deploy approval while checks are red.
- **App traffic metrics:** BOSS reads a deployed app's `/metrics` and shows request count, error rate and p50/p95 latency, with an alert on errors.
- **Architecture approval:** a workflow step where an architect writes the architecture from the approved PRD and a person approves it, asks for changes or rejects it.

## Fixes

- **Infra:** a new deploy target now always gets its app port.
- **Captain:** the Captain backs off after a blocked or failed pass instead of starting a new one every minute. The Factory's "building" count shows only builds that are running.

## Known issues

- The Approvals page can show "Decision API unavailable" for an Owner; approvals still work from the decision cards and the API. Fixed in the next release.
- The domain-origin path of first-run setup was covered by automated tests only; the fresh-install walk covered localhost and an IP address.
