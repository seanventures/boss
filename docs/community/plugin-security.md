# Plugin security

Plugins can create and change infrastructure, so BOSS treats them as untrusted until they are
verified.

## Sources and trust

| Source | Where | Default | Trust |
|---|---|---|---|
| builtin | `plugins/official/` | enabled | Reviewed with BOSS. Runs in-process. |
| verified | `plugins/verified/` | enabled | Its version and tree checksum must match `plugins/registry.lock.yaml`, or it is refused. |
| community | `plugins/community/`, `BOSS_PLUGIN_PATHS` | **disabled** | Unverified. An operator enables it in Infra › Plugins. |

- **Checksum.** A plugin's checksum is the sha256 of its directory tree. Get it with
  `python -m boss_engine.platform checksum <dir>`.
- **Enable approval.** Enabling a community plugin records the checksum the operator approved.
  If any file changes later, the plugin is disabled again until someone re-approves it.
- **Support level.** Only builtin and verified plugins may claim `certified` or
  `experimental`. A community plugin is always shown as `community`.
- **Reserved namespace.** `official.` belongs to BOSS.

## Isolation and permissions

- Only builtin plugins run inside BOSS.
- Verified and community plugins run in a separate process with an empty environment. That
  process holds no host connection and no vault access. Each host command and each secret
  operation is a request to BOSS, and BOSS checks it against the manifest's `permissions`:

  | Operation | Permission it needs |
  |---|---|
  | `host.run` | `docker` or `subprocess` |
  | `secrets.put` | `write-scoped` |
  | `secrets.resolve` | `read-scoped` or `write-scoped` |

- Permissions are deny-by-default and shown to the operator before enabling.
- Container-runtime plugins need images pinned by digest. Their isolated runner is P1.

## Secrets

- **Values live in one place.** Connection values (`DATABASE_URL`, ...) are stored in the app
  secret vault, env `deploy`. The app receives them by name in `.boss/app.env` at deploy.
- **Plugins see references.** A plugin, a plan, a decision, a receipt, a log or a PR sees only
  `app_secret://deploy/<app>/<NAME>`.
- **A broker is scoped.** It serves one resource. It accepts only the binding names the
  manifest declares, and resolves only that app's refs.
- **Output is redacted.** Every value a broker sees is stripped from plugin output, along with
  BOSS's credential patterns.
- **The harness checks for leaks.** A plugin fails if a value reaches a result or a log line.

## Policy

- **No public ports by default.** Database, cache, storage and similar capabilities bind
  privately. A plan that exposes a port nobody asked for is refused.
- **Some changes need approval:**
  - `public: true`;
  - any destructive change (destroy, replace, destructive migration);
  - a production environment.

  The deploy approval shows each of these, and its content hash pins the plan.
- **Attached resources are never destroyed.** BOSS refuses before any plugin code runs.
- **Every operation leaves a receipt.** The receipt records the plugin version and checksum,
  the plan digest, the outcome and the evidence.

## Emergency disable

Disable a plugin in **Infra › Plugins**. A disabled plugin cannot be planned or run.
