# Use a Grok Build subscription you already have

BOSS can drive the [official Grok Build CLI](https://docs.x.ai/build/cli/headless-scripting) as a coding harness on a machine you administer. The session stays in the service user's `GROK_HOME`, normally `/home/boss/.grok` in the BOSS images. BOSS does not extract, copy, or upload `auth.json`, and it does not substitute `XAI_API_KEY` for a subscription. This source is recorded as `xai_grok_build`, scoped to the operator and single-tenant work.

The Community API and worker images already include the Grok binary. `--install-coding-clis` adds a separate copy for your user on the VM. A binary on the host alone is not the copy inside the container. If you build a custom image, install Grok in every container that will run coding work and put that binary on the container's `PATH`.

## Install the CLI and sign in

The [xAI installer](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/01-getting-started.md) is `curl -fsSL https://x.ai/cli/install.sh | bash`. You only need that when the image or the host copy is not already there.

For the Community Compose install, run the [official device login](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/02-authentication.md) as the `boss` user in the API service. From the install directory:

```bash
docker compose -f scripts/install/community-compose.yml --env-file .env \
  exec -u boss api grok --version
docker compose -f scripts/install/community-compose.yml --env-file .env \
  exec -u boss api grok login --device-auth
```

Complete the displayed code in a browser. The API and worker containers need a persistent volume at `/home/boss/.grok`, owned by `boss`, to keep the CLI session after recreation. The CLI stores its own private `auth.json`. BOSS checks file ownership, readability, mode `0600`, and the CLI's auth response. A signed-out `grok models` may exit successfully and print cached models, so that check alone does not prove a working subscription.

Grok gives per-model API keys and API endpoint settings precedence over its active session. BOSS inspects the documented user and managed TOML layers before each coding turn and refuses a subscription route if model API billing settings are present or a config cannot be parsed. Remove those model settings from the service user's Grok config and retry. The diagnostic never displays config values. BOSS also strips `XAI_API_KEY` from the child process. See [xAI authentication precedence](https://docs.x.ai/build/enterprise#authentication) and [configuration scopes](https://docs.x.ai/build/settings#scopes).

## Record the login

Use `GET /v1/setup/coding-agents` to see `grok` detection and the next step. After `grok login --device-auth` as `boss` inside the API container, rerun the installer with `--connect-grok-local`, or `POST /v1/setup/coding-agents/connect` with `{"project_id":"...","provider":"grok","prove":false}`. That records the CLI-owned login and does not start a model call. A missing login is an error. A later governed job is what checks that the subscription can answer.

Set `BOSS_CODE_AGENT_ENGINE=grok` for code work after the connection succeeds. `BOSS_PROVIDER_LOCK=grok` is not supported for chat. There is no Grok subscription chat adapter on this path.

## Limits

The harness uses headless `--single`, disables auto-update and subagents, and sets a turn limit. Reviews use Grok's read-only sandbox and a read-only tool allowlist. Implementation steps use the workspace sandbox. BOSS runs its own verification and tracks subscription cost as unreported rather than inventing a per-turn price. A nonzero CLI exit fails the stage visibly. The generic harness remains `verified=false` until a governed code task completes on the target Linux deployment.

The CLI's [sandbox profiles](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/18-sandbox.md) allow reads beyond the checkout in some modes. Run BOSS in its isolated coding container, keep host secrets out of that container, and apply the normal task policy, budget, pull request, and receipt gates. The [xAI consumer terms](https://x.ai/legal/terms-of-service) do not establish a shared multi-tenant backend entitlement. Use an appropriate business or API agreement for that kind of workload.

## Next

- [Model connections](MODEL_CONNECTIONS.md) for API keys and the other CLI logins.
- [Installer guide](https://github.com/seanventures/boss/blob/main/scripts/install/README.md) for `--connect-grok-local` and the secret store.
- [Guide index](README.md) for the console, terminal, and feedback.
