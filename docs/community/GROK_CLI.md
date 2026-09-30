# Use a Grok Build subscription you already have

Connect an existing Grok Build subscription so BOSS can drive the [official Grok Build CLI](https://docs.x.ai/build/cli/headless-scripting) as a coding harness on a machine you administer. BOSS records this source as `xai_grok_build`, scoped to the operator and single-tenant work.

The CLI session stays in the service user's `GROK_HOME`, which is `/home/boss/.grok` in the BOSS images. BOSS never extracts, copies, or uploads `auth.json`, and it never substitutes `XAI_API_KEY` for a subscription. To use an xAI API key instead, see [model connections](MODEL_CONNECTIONS.md).

## Install the CLI

The Community API and worker images already include the Grok binary, so most installs can skip this step.

`--install-coding-clis` adds a separate copy for your user on the VM. Keep in mind that a binary on the host is not the copy inside the container. If you build a custom image, install Grok in every container that will run coding work and put it on that container's `PATH`.

If you do need the CLI, the [xAI installer](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/01-getting-started.md) is `curl -fsSL https://x.ai/cli/install.sh | bash`.

## Sign in

For a Community Compose install, run the [official device login](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/02-authentication.md) as the `boss` user in the API service. From the install directory:

```bash
docker compose -f scripts/install/community-compose.yml --env-file .env \
  exec -u boss api grok --version
docker compose -f scripts/install/community-compose.yml --env-file .env \
  exec -u boss api grok login --device-auth
```

Complete the displayed code in a browser. The CLI stores its own private `auth.json`.

To keep the session when containers are recreated, give the API and worker containers a persistent volume at `/home/boss/.grok`, owned by `boss`.

BOSS checks that the auth file is owned by the right user, readable, and mode `0600`, and it checks the CLI's auth response.

!!! note
    A signed-out `grok models` can exit successfully and print cached models, so that command alone does not prove a working subscription.

### Remove API billing settings

Grok gives per-model API keys and API endpoint settings precedence over the active session. Before each coding turn, BOSS inspects the documented user and managed TOML layers and refuses the subscription route if model API billing settings are present or a config cannot be parsed. If that happens, remove those model settings from the service user's Grok config and retry. The diagnostic never displays config values, and BOSS also strips `XAI_API_KEY` from the child process. See [xAI authentication precedence](https://docs.x.ai/build/enterprise#authentication) and [configuration scopes](https://docs.x.ai/build/settings#scopes).

## Record the login

1. Check detection and the suggested next step with `GET /v1/setup/coding-agents`.
2. After signing in with `grok login --device-auth` as `boss` inside the API container, record the login in either of these ways:
    - Rerun the installer with `--connect-grok-local`.
    - Call `POST /v1/setup/coding-agents/connect` with `{"project_id":"...","provider":"grok","prove":false}`.
3. Once the connection succeeds, set `BOSS_CODE_AGENT_ENGINE=grok` for code work.

Recording the login does not start a model call, and a missing login is reported as an error. A later governed job is what confirms the subscription can answer.

!!! note "Code work only"
    This path covers code work. `BOSS_PROVIDER_LOCK=grok` is not supported for chat, because there is no Grok subscription chat adapter on this path.

## How BOSS runs Grok

- The harness uses headless `--single` mode, disables auto-update and subagents, and sets a turn limit.
- Reviews use Grok's read-only sandbox and a read-only tool allowlist. Implementation steps use the workspace sandbox.
- BOSS runs its own verification afterward.
- Subscription cost is tracked as unreported rather than as an invented per-turn price.
- A nonzero CLI exit fails the stage visibly.

The generic harness remains `verified=false` until a governed code task completes on the target Linux deployment.

!!! warning "Isolation and terms of use"
    In some modes, the CLI's [sandbox profiles](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/18-sandbox.md) allow reads beyond the checkout. Run BOSS in its isolated coding container, keep host secrets out of that container, and apply the normal task policy, budget, pull request, and receipt gates.

    The [xAI consumer terms](https://x.ai/legal/terms-of-service) do not establish an entitlement to a shared multi-tenant backend. For that kind of workload, use an appropriate business or API agreement.

## Next steps

- [Model connections](MODEL_CONNECTIONS.md), for API keys and other CLI logins.
- [Installer guide](https://github.com/seanventures/boss/blob/main/scripts/install/README.md), for `--connect-grok-local` and the secret store.
- [Guide index](README.md), for the console, terminal, and feedback.
