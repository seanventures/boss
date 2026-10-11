# Writing an infrastructure plugin

Apps ask for **capabilities** (`database.relational`, `cache`, `object-storage`). Plugins
provide them. BOSS owns the plan, the approval, the secrets, the state and the evidence. Your
plugin implements only the provider-specific steps.

Start from `plugins/community/example/`. It is a complete plugin that passes the harness.

## 1. Scaffold

```bash
python -m boss_engine.platform init plugins/community/my-queue --capability queue
python -m boss_engine.platform validate plugins/community/my-queue
```

`boss-engine plugin init|validate|test` runs the same commands.

## 2. The manifest: `boss-plugin.yaml` (`boss.plugin/v1`)

| Field | What it means |
|---|---|
| `metadata.id` | `<namespace>.<name>`. Community plugins use `community.`; `official.` is reserved for BOSS. |
| `metadata.version` | A semantic version. A receipt records it on every operation. |
| `spec.supportLevel` | `certified`, `experimental` or `community`. A community-source plugin is shown as `community`, whatever it claims. |
| `spec.capabilities` / `features` / `engines` | What the registry searches by. |
| `spec.requires` | Capabilities another resource must provide first. For example, pgvector requires `database.relational`. |
| `spec.runtime` | `python` with `entrypoint: module:Class`. `container` (an image pinned by digest) is validated, but its runner is not built yet (P1). |
| `spec.permissions` | `secrets`, `docker`, `subprocess`, `network`, `filesystem`. Anything not declared is denied. |
| `spec.hooks` | Every plugin has `validate`, `plan`, `provision`, `verify` and `destroy`. The optional ones are `configure`, `observe`, `backup` and `restore`. |
| `spec.bindings` | The env names your plugin can give the app, such as `DATABASE_URL`. |
| `spec.configSchema` | A JSON Schema for the request's `config:`. |

The JSON Schemas are in `engine/boss_engine/contracts/boss_plugin_v1.schema.json` (and the
`resource_request` and `operation_result` schemas next to it).

## 3. The hooks

Each hook takes a `HookContext` and returns an `OperationResult` (`ctx.result(...)`, `ctx.fail(...)`).

| Hook | Must |
|---|---|
| `validate` | Check the request and config, and change nothing. |
| `plan` | Return a `ResourcePlan`: changes, ports, `publicAccess`, binding names, cost and limitations. It changes nothing. |
| `provision` | Create the resource. It must be **idempotent**: calling it twice returns the same `resourceId` and the same binding refs. |
| `configure` | Apply settings. Report them as `Evidence(kind="config")`. |
| `verify` | Open a **real connection** and report it as `Evidence(kind="connection", passed=True)`. "The command returned 0" is not verification. |
| `observe` | Return normalized health in `observed`. |
| `backup` / `restore` | Return an artifact reference. A restore must be tested. |
| `destroy` | Remove everything `provision` created, and delete your secret refs. BOSS never calls `destroy` on an attached resource. |

Rules:

- **Run commands through `ctx.host.run(...)`.** This is the deploy's target host (BOSS's governed
  SSH), or this machine in the harness. Never open your own connection to a host.
- **Label everything** with `sdk.docker_labels_args(ctx)`, so BOSS can find and clean up what a
  resource owns.
- **Secrets travel by reference.** Pass a value to `ctx.secrets.put("DATABASE_URL", value)` and
  return the `SecretRef` you get back. A value must never appear in a result, a log, an
  artifact name or an error. Pass passwords on stdin, not on a command line.
- **Keep ports private.** Bind to `127.0.0.1` unless `ctx.resource.public` is set
  (`sdk.bind_address(ctx)`). Public access needs a person's approval.
- **Put non-secret state in `state`.** For example, a container name or a host port. BOSS
  stores it and hands it back as `ctx.state`.
- **Declare your limitations.** Do not claim atomicity you do not have.

## 4. Test against real containers

```bash
python -m boss_engine.platform test plugins/community/my-queue   # --json, --keep
```

The harness runs your hooks against ephemeral containers on free ports and checks:

- read-only hooks change nothing;
- the plan is private by default;
- provision is idempotent;
- what you create is labelled;
- verify makes a real connection;
- secrets are redacted;
- destroy works and leaves no orphans;
- attached resources are never destroyed.

`harness.yaml` sets the request the harness uses.

## 5. How an app uses it

```yaml
# boss.deploy.yaml
resources:
  cache:
    capability: cache
    provider: community.my-queue   # optional; otherwise the best enabled plugin for the capability
    version: "^0.1"                # optional pin
    public: false                  # the default
  legacy-db:
    capability: database.relational
    mode: attached                 # an existing database: BOSS connects to it and never destroys it
    attach: {bindings: {DATABASE_URL: LEGACY_DB_URL}}
```

The deploy approval shows the plan: resources, bindings (names only), ports, public access,
destructive actions and cost. Approving it pins the plan's hash.

## 6. Submit

Open a PR that adds `plugins/community/<name>/` with its README, its limitations and the harness
output. Community plugins ship disabled, and an operator enables each one in **Infra ›
Plugins**. To become `verified`, a maintainer pins the plugin's version and checksum in
`plugins/registry.lock.yaml`. To become `certified`, every lifecycle stage must pass in CI.
After either change, regenerate the matrix:
`python -m boss_engine.platform matrix --write` ([support matrix](support-matrix.md)).
