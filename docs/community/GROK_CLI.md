# Grok Build subscription for coding work

BOSS can drive the [official Grok Build CLI](https://docs.x.ai/build/cli/headless-scripting)
as a coding harness on a self-hosted, owner-operated install. Its session stays in the
service user's `GROK_HOME` (normally `/home/boss/.grok` in the BOSS images). BOSS does not
extract, copy, or upload `auth.json`, and does not substitute `XAI_API_KEY` for a subscription.
This source is recorded as `xai_grok_build`, scoped to the operator and single-tenant work.

## Install and sign in

The [xAI installer](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/01-getting-started.md)
is `curl -fsSL https://x.ai/cli/install.sh | bash`. The community installer can opt to
install coding CLIs in the BOSS image. If using a custom image, install Grok in every
container that will run coding work and put its binary on that container's `PATH`.
Installing it on the VM host alone does not put it inside Docker.

For the Docker Compose install, run the [official device login](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/02-authentication.md)
as the `boss` user in the API service, which runs the current coding and setup paths:

```bash
docker compose exec -u boss api grok --version
docker compose exec -u boss api grok login --device-auth
```

Complete the displayed code in a browser. The API and worker containers need a persistent
volume at `/home/boss/.grok`, owned by `boss`, to keep the CLI session after recreation.
The CLI stores its own private `auth.json`; BOSS checks only file ownership, readability,
mode 0600, and the CLI's auth response. A signed-out `grok models` may exit successfully
and print cached models, so this check alone does not prove a working subscription.
Grok gives per-model API keys and API endpoint settings precedence over its active session.
BOSS inspects the documented user and managed TOML layers before each coding turn and
refuses a subscription route if model API billing settings are present or a config cannot
be parsed. Remove those model settings from the service user's Grok config and retry;
the diagnostic never displays config values. BOSS also strips `XAI_API_KEY` from the child
process. See [xAI authentication precedence](https://docs.x.ai/build/enterprise#authentication)
and [configuration scopes](https://docs.x.ai/build/settings#scopes).

Use `GET /v1/setup/coding-agents` to see `grok` detection and the next step. Use the setup
screen or `POST /v1/setup/coding-agents/connect` with `{"project_id":"...","provider":"grok"}`
to make one read-only proof turn and record the entitlement. Do not set `prove: false` for
qualification. Set `BOSS_CODE_AGENT_ENGINE=grok` for code work after the connection succeeds.
`BOSS_PROVIDER_LOCK=grok` is not supported for chat; there is no Grok subscription chat
adapter in this change.

## Runtime boundaries

The harness uses headless `--single`, disables auto-update and subagents, and sets a turn
limit. Reviews use Grok's read-only sandbox and a read-only tool allowlist; implementation
steps use the workspace sandbox. BOSS runs its own verification and tracks subscription
cost as unreported rather than inventing a per-turn price. A nonzero CLI exit fails the
stage visibly. The generic harness remains `verified=false` until a governed code task
completes on the target Linux deployment.

The CLI's [sandbox profiles](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/18-sandbox.md)
allow reads beyond the checkout in some modes. Run BOSS in its isolated coding container,
keep host secrets out of that container, and apply the normal BOSS task policy, budget, PR,
and receipt gates. The [xAI consumer terms](https://x.ai/legal/terms-of-service) do not
establish a shared multi-tenant backend entitlement; use an appropriate business or API
agreement for such workloads.
