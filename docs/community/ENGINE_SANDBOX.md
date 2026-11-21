# Engine sandbox and managed policies

BOSS runs each coding engine (Claude Code, Codex, Gemini CLI, opencode and the others) inside its own sandbox: an ephemeral clone, a per-build user, a fenced network and the container. Some engines also start an inner sandbox of their own, and some logins carry their organisation's policy into BOSS. This page explains what BOSS does when either stops an engine from working, and what a host administrator can change.

## The engine check in Setup

When a subscription connects (**Setup › Models**, `boss connect`), BOSS first proves it with one trivial turn. Connect then returns, and the row shows **Checking…** while BOSS runs one governed writer session where builds run: a queued job, under a build's own sandbox user, its own home directory and the egress proxy, with none of the control plane's environment. That session must write a file and run a command in an empty directory:

```
printf BOSS_PROBE_OK > .boss-probe && cat .boss-probe
```

BOSS checks the file on disk and the command's output, not the model's reply. A metered login reserves the session's spend first, like any build. The row then shows one of:

- **Can write.** BOSS records what it found for that login.
- **Cannot write.** The engine's sandbox could not start, or its organisation's policy refused the write. Setup says so in one sentence. Retrying a build would not help: a host administrator or the organisation's admin fixes it.
- **Write not proven.** The session did not try (the model answered without running the command). Nothing is assumed.
- **Not checked.** The check could not run (no sandbox user free, the budget refused it); the row says why.

## Agent commands fail with a namespace error

```
bwrap: No permissions to create a new namespace
```

The engine could not start its command sandbox (bubblewrap). Docker's default seccomp profile restricts the namespace calls bubblewrap needs, and Ubuntu 24.04 kernels (including the VM a Colima install runs) restrict unprivileged user namespaces through AppArmor.

BOSS treats this as the engine being unable to run, on every engine that streams its turns to BOSS: Claude Code, the Claude Agent SDK, Codex, and the ACP engines (Gemini CLI, opencode). The native, Azure and scripted engines run no inner sandbox of their own, so they never meet this. BOSS reads only a command that failed and whose output opens with bubblewrap's own words; a search or a test log that merely mentions them is ordinary output. The first such failure stops the run with `sandbox_unavailable` and the evidence. Every later session of that run refuses at once. The build never reports "no test files" or a failing test for it, and the app's existing suite never stands in for tests nobody could write.

What to do:

- Most installs never see this. BOSS runs Codex writers and review seats without Codex's inner sandbox, because BOSS's own sandbox is the boundary.
- If it appears, a host administrator must allow the engine's sandbox at both layers (seccomp, and AppArmor where the host has it). Do not disable the engine's managed policy, run privileged containers, or turn off AppArmor host-wide. Test any scoped profile in a disposable container that holds no credentials before applying it to BOSS's services.
- Scoped profiles that BOSS ships and checks are planned (BOSSAPP-1414). Until then, treat any profile as host configuration you own.

References: [Docker seccomp](https://docs.docker.com/engine/security/seccomp/) and [Ubuntu AppArmor](https://documentation.ubuntu.com/security/security-features/privilege-restriction/apparmor/).

## Managed engine policies

**Codex.** A ChatGPT Enterprise or Business workspace can set Codex requirements that forbid `danger-full-access`, the mode a BOSS writer runs in. Codex then falls back to read-only and says so, for example:

```
Configured value for `permission_profile` is disallowed by requirements; falling back from ...
```

BOSS recognises every downgrade wording Codex prints in its own diagnostics. A command's output that quotes them (a grep of this page) is never read as one. In a writer session it stops the run with `policy_denied` and the evidence. A review seat that was held to read-only is not stopped, since that is what it asked for. A 401 in one of Codex's diagnostics (an MCP server, a retried request) never marks the Codex login as refused.

At Setup, when the engine check finds the login managed, BOSS tests Codex's own `workspace-write` sandbox where builds run, with no model. It must start, write in its workspace, write the toolchain caches (`~/.npm`, `~/.cache` including the Go build cache, `~/go`, `~/.m2`) and reach the package registry. Only when all of that passes does BOSS record `workspace-write` for that login, with network access and those caches writable. It then runs the writer check again in that mode. Otherwise the login keeps today's mode and Setup says why it cannot write.

BOSS never switches every Codex login to `workspace-write`. On a host where the inner sandbox cannot start, that would stop every Codex command.

**Claude Code.** An organisation can manage Claude Code through server-managed settings or `/etc/claude-code/managed-settings.json`: force its Bash sandbox, deny tools, or disable bypass modes. Setup records which of these a managed-settings file sets, by key only: a value such as `env` is never stored or shown. The engine check catches what server-managed settings do. In a writer session, the CLI's own refusal line ("Bash is blocked by enterprise managed policy") stops the run with `policy_denied`; a proxy's "blocked by your organization's policy" page does not. A deny rule BOSS set itself does not.

**Other engines.** Every coding-CLI login gets the same writer check. An engine whose system settings force a sandbox that cannot start in BOSS's container fails it with that engine's own words.

## Package registries

Each failed dependency install (npm, pnpm, yarn, pip, Go, Maven) is classified once, and the build acts on the class:

| What the install said | Class | What BOSS does |
|---|---|---|
| Certificate not trusted (`UNABLE_TO_VERIFY_LEAF_SIGNATURE`, `CERTIFICATE_VERIFY_FAILED`, `x509`, `PKIX`) | environment | Stops repair. A TLS-intercepting proxy needs its CA trusted by the build images. |
| DNS, connect or reset (`EAI_AGAIN`, `ECONNRESET`, `no such host`) | environment, transient | One retry after a short wait, then stops repair. |
| Registry credential missing (`E401`, `ERR_PNPM_FETCH_401`, `401 Unauthorized`), proxy credential (`E407`, `407`) | environment | Stops repair. Give the builds the registry's or proxy's credential on the host. |
| The registry, or BOSS's egress proxy, refuses the registry itself (`E403` on its root or metadata, a refused tunnel) | environment | Stops repair. Check the registry's permissions and the egress allowlist (`BOSS_EGRESS_CONNECT_ALLOW`). |
| A curation firewall blocks one package version (Artifactory Curation, Nexus Firewall: `403` on that version's tarball, wheel, jar or module zip) | the coder's | One round to choose a version the registry allows. The coder may not remove the dependency or bypass the registry. |
| The install timed out | the coder's, unless the output shows a slow network | One retry with double the time, within a per-build budget. |

Retries are keyed on the manifest and lockfiles. BOSS never runs the same failing install twice on the same files in one build. A changed `package.json` or lockfile is a new install. An environment failure opens the pull request as a labelled draft that says the tests did not run.
