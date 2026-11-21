# BOSS Community 1.0.0-rc.10.32 candidate

RC10.32: agents fix failures on their own, and BOSS installs inside company networks. When work you started fails, BOSS's agents now carry on with your authority instead of handing you a card. An install behind a company CA, a registry mirror or an outbound proxy, or on a Mac with Colima, now works from settings in `.env`.

Much of the enterprise-network and Colima work came from an enterprise contributor, whose patches were adopted and generalised so every install benefits. Thank you.

This is a test candidate. Cutting the tag does not deploy a host or publish an image.

## What is better for you

**Enterprise networks and Colima**

- One block of settings in `.env` covers a company network: your company CA (`BOSS_EXTRA_CA_FILE`), registry mirrors for npm, pip, Go, Maven and Playwright, and an outbound proxy (`BOSS_UPSTREAM_PROXY`). Empty means a stock install. See [Enterprise network](ENTERPRISE_NETWORK.md).
- The company CA is trusted when images are built and when they run. TLS verification always stays on, and a URL that carries a password is refused before it reaches `.env` or an image.
- The sandbox's egress allowlist now adds your hosts to the built-in list (`BOSS_EGRESS_CONNECT_ALLOW`). An internal registry on a private address can be allowed by exact name (`BOSS_EGRESS_PRIVATE_ALLOW`).
- The coding CLIs (Claude Code, Codex, Grok) and pnpm install from a committed lockfile with checksums, so a mirror or gateway cannot swap them.
- Macs running Colima get sizing, mount and Docker-context guidance ([Colima](COLIMA.md)), and a stopped Docker Desktop or Colima gets a clear message. `boss doctor` checks the network settings and each configured registry.
- An administrator can let a coding engine's own sandbox (Codex's bubblewrap) start inside BOSS's containers with a scoped seccomp and AppArmor profile, without `--privileged`. It is **off** by default.
- When a coding engine's sandbox cannot start, or a company policy blocks it, the build stops and says so instead of reporting "no test files". Setup's engine check now tests that too. Install failures behind a proxy or private registry are named as such, not retried as dependency conflicts.

**Agents fix failures with the starter's authority**

- When work you started fails, BOSS's agents fix it using your own permissions, without asking you for a grant again. Ended grants, merge conflicts, contract tests that can never pass as written, and held model checks are now handled by agents.
- What stays with people: merging, production deploys, approvals, secrets, running commands on a host, and budgets. The agents' authority never reaches another organisation, and every use is in the audit log.
- Cards that still need you say what failed, why, the exact steps, and what happens after.
- Step recovery reads the run's evidence with a read-only token, so it no longer decides blind. A nested run can never widen its authority.

**Validation and remediation you can trust**

- A build asked to fix a file must ship an edit of that file. Copying a reference answer, leaving the file unchanged, or claiming a validator ran when it did not now fails the build into its repair loop.
- The validator's findings before and after, and each fix, are recorded on the build and the workflow step, so you can see what `gwlint` (or your validator) reported.

**The Work panel, and checks that tell the truth**

- Each piece of work has one landing page: who did what (Spec, Build, Review and tests, Merge, Staging, Production), what was asked, discussed and built, review comments with **Let BOSS fix these**, and one next step.
- Work rows are one line of state, a small flow and one action. An investigation is shown as an investigation, not a code pipeline.
- A change whose checks still fail never reads "Ready to merge". It says **Checks failing**, and Merge goes to the card that asks for your reason.
- A model at its usage limit says so in its own words. A model check names the model source it belongs to.

**Health checks and API previews**

- Health probes use the checks your app declares (for example `/healthz`), so a healthy API no longer shows as Down. A target with nothing to probe is "unknown", not failing.
- **Open preview** on an API-only app shows a landing page with the routes that answer, instead of a bare 404.
- Availability and SLO figures count only the current probe.
- Config deploys are checked at their service and read the host's exit code over SSH.

**Helm chat keeps your message, and can forget**

- A Helm message is saved before the model starts, so a reload, a failure or a restart no longer loses it. The reply lands in the thread you opened.
- `forget` can retire a wrong lesson from the knowledge graph, and it stays retired.

**Fewer surprises**

- Saying no to a step is an answer: BOSS never re-runs a step you refused.
- An app with its own test suite never fails a build for "no new test files".
- A chat turn no longer blocks the project's budget while it works.
- A scan keeps private infrastructure and customer names out of the public tree.

## Upgrade notes

- `remote.sh update` (and `boss upgrade`) now runs the enterprise-network finalize step. It checks the enterprise-network settings in `.env` and applies any new values you exported. A stock install has nothing to set.
- **Agents fix failures is on for existing installs after upgrade.** To turn it off for the whole install, use **Govern › Each failed step › Agents fix failures with your authority** (or set `BOSS_AGENTS_FIX_FAILURES=0` in `.env` and recreate the containers). To turn it off for one project, set the project setting `agents_fix_failures` to `0`. Off means a failure that needs your authority comes back to you as a card, as before. Revoking your own fixer grant also stops it, and BOSS does not re-create it.

## Known issues and what was not verified

- The engine inner-sandbox AppArmor profile on **AppArmor 3** hosts was checked as text only, not loaded on a real AppArmor 3 host.
- The seccomp and AppArmor profiles were validated in throwaway Ubuntu 24.04 containers only.
- Codex signed in through a managed (company) login has not been tested.
- The enterprise-network settings were tested in containers and on the contributor's install; a nightly run behind an intercepting proxy and a fresh Colima install on a real Mac are still to come.
- The issues listed in the [v1.0-rc10.31 notes](RELEASE_NOTES_v1.0-rc10.31.md) still apply.

## Not in this tag

- Opening a pull request on GitHub still needs a GitHub account and the BOSS GitHub App; a model still needs a subscription sign-in, an API key, or a local model.
- Registry credentials injected by egress, and self-hosted Git (GitHub Enterprise Server, GitLab) through egress, are not in this tag.
- The release still has no published container images; the installer builds them on your machine (about 10 minutes on a fast host).

## Checks

- The version pins match `1.0.0-rc.10.32` (`engine/tests/test_distribution_contract.py`).
- The public-tree scan passes (`engine/tests/test_the_public_tree_names_no_private_infrastructure.py`).
- The clean-install stranger test runs on this commit before the tag is published.
