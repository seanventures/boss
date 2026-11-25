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
- If it appears (in practice: a Codex login whose organisation forbids full access, so Codex's own sandbox must start), a host administrator turns on [engine inner-sandbox support](#engine-inner-sandbox-support) below. Do not disable the engine's managed policy, run privileged containers, or turn off AppArmor host-wide.

References: [Docker seccomp](https://docs.docker.com/engine/security/seccomp/) and [Ubuntu AppArmor](https://documentation.ubuntu.com/security/security-features/privilege-restriction/apparmor/).

## Engine inner-sandbox support

Off by default, and nothing changes for an install that does not turn it on. It lets a coding engine's own sandbox (today: Codex's bubblewrap) create the namespaces it needs inside BOSS's containers, without `--privileged`, without adding a capability, and without turning AppArmor off. Turn it on only when an engine must use its own sandbox, which is when a Codex login's organisation forbids full access.

It is one line in the install's `.env`. The shell's environment is never read for it:

| Docker host | Set | What applies |
|---|---|---|
| Docker Desktop (Mac, Windows) | `BOSS_ENGINE_INNER_SANDBOX=seccomp` | BOSS's seccomp profile. Docker Desktop's VM has no AppArmor. |
| Colima (Mac) | `BOSS_ENGINE_INNER_SANDBOX=seccomp+apparmor` | seccomp, plus the AppArmor profile `boss-engine` loaded in the Colima VM ([Colima](COLIMA.md#engine-inner-sandbox-support)). |
| Ubuntu 24.04, native Docker | `BOSS_ENGINE_INNER_SANDBOX=seccomp+apparmor` | seccomp, plus `boss-engine` loaded on the host. |
| Linux with AppArmor 3 (Ubuntu 22.04 and older) | `BOSS_ENGINE_INNER_SANDBOX=seccomp+apparmor` | The same; AppArmor 3 does not mediate user namespaces, so the loader leaves that rule out. |
| Linux without AppArmor | `BOSS_ENGINE_INNER_SANDBOX=seccomp` | seccomp only. |

### Turn it on

From the install directory:

1. With AppArmor (Colima, Ubuntu), load the profile on the Docker host first. Native Linux: `sudo scripts/sandbox/load_apparmor.sh`. Colima: `colima --profile boss ssh -- sudo "$PWD/scripts/sandbox/load_apparmor.sh"`. It installs `/etc/apparmor.d/boss-engine`, so it is loaded again after a reboot. With `--check` it only reports.
2. Add the line from the table to `.env`.
3. `boss start`. It recreates api and worker with the profiles. The agent-host `runner` gets them too whenever it is started with the files below. Do it when no build is running.
4. `boss doctor`. The `engine sandbox` line runs Codex's own sandbox in the worker, with no model, and names the layer that stops it, with the fix. The Setup engine check does the same where builds run, and shows the result on the login's row.

Every lifecycle command applies it: `boss`, the installer and `boss upgrade`, `boss rollback`, restore, `remote.sh update`, `boss doctor --repair` and BOSS's self-fix installs all take their compose files from `scripts/install/compose_files.sh`. To run Compose by hand, use the files it prints: `bash scripts/install/compose_files.sh .`.

To turn it off, remove the line from `.env`, run `boss start`, then unload the profile: `sudo scripts/sandbox/load_apparmor.sh --remove` (Colima: through `colima ssh`, as above). No host sysctl changes.

### What it allows

- **seccomp** (`scripts/sandbox/engine-seccomp.json`). This is Docker's default profile ([moby/profiles](https://github.com/moby/profiles), Apache-2.0, vendored in `scripts/sandbox/vendor/` and checked by hash; nothing is downloaded at install time) plus three rules. `clone` and `unshare` are allowed only when they create a new user namespace. `mount`, `umount2` and `pivot_root` are allowed. Their arguments are pointers that seccomp cannot inspect, so the remaining guard is the kernel: each needs CAP_SYS_ADMIN in the user namespace that owns the mount, which a container process has only inside a user namespace it created itself. `sethostname`, `setns`, `clone3`, `bpf`, `keyctl` and every capability stay as in Docker's default.
- **AppArmor** (`scripts/sandbox/boss-engine.apparmor`). The control-plane profile follows Docker's `docker-default`: mounts denied, the same `/proc` and `/sys` rules, plus the AppArmor 4.0 ABI to mediate user namespaces. The fixed, root-owned BOSS launcher enters `engine_sandbox` before dropping to the build uid and setting `no_new_privs`. Build workloads inherit this profile, including its namespace, mount and pivot-root permissions; they do not gain host capabilities. This avoids attempting a privilege-increasing profile transition when Codex later executes bubblewrap. Direct execution of the bundled bubblewrap or `/usr/bin/bwrap` also enters that profile. On an AppArmor host, use `seccomp+apparmor`, not `seccomp` alone. On AppArmor 3 the loader drops the ABI line and the `userns` rule; AppArmor 3 does not mediate user namespaces.

### The honest attack surface

Unprivileged user namespaces are a real kernel attack surface. Bugs in overlayfs, nf_tables and the mount API have each let a process inside one become root. With this on, a process in api, worker or runner can reach that surface. Those containers run builds, so that includes repository code (an npm postinstall script, a test suite). On Docker Desktop and on hosts without AppArmor, seccomp is the only gate, and it is the same for every process in the container. With AppArmor the grant goes through the helper's path. That narrows the surface, but it is not a boundary: any process may execute that helper.

What still holds: no capability is added, each build runs as its own unprivileged uid, a new network namespace has no route out, so the per-build egress fence still applies, and on a Mac the Colima or Docker Desktop VM separates all of it from the Mac. What a kernel exploit from inside would reach: the container's root, which has NET_ADMIN to manage that fence, and the API's environment. Keep the host kernel patched. Turn it on only when an engine needs it, and review the two profiles before you do.

When a future Codex release needs a call these profiles do not allow, the check in `boss doctor` and Setup fails and names it. BOSS never widens the profiles on its own.

### Trusted launcher and unattended repair

With inner-sandbox support enabled, the launcher permission applies automatically to
isolated workloads, not to a particular named agent or logged-in user. The launcher
remains root-owned, executable only by the BOSS group, and restricted to its fixed uid
pool and operations. AppArmor stays enforced; per-build uid isolation, zero effective
workload capabilities, `no_new_privs`, seccomp and the egress fence remain in place.
The tradeoff is explicit: all launcher workloads can use user namespaces, rather than
only commands that first execute bubblewrap. They still cannot modify host policy.

An older profile can let a direct `codex sandbox` smoke check pass while an isolated
build fails with an AppArmor exec denial marked `no new privs`. Load this release's
profile through the administrator-controlled loader, then run Setup's engine check.
Only its successful isolated file-write and command proof clears `cannot_write`.
Do not grant agents unrestricted sudo, Docker socket access, or permission to rewrite
AppArmor policies. Host security changes remain administrator-controlled; application
repairs use the existing bounded recovery and governance rules.

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
