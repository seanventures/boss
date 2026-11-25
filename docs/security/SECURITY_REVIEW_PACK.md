# Security review pack

For a security and compliance team reviewing a BOSS install before a guided preview. Every claim
cites the code or test it rests on, on `main`. Where something is missing or weaker than you
might assume, it says so. Ticket: BOSSAPP-1290. The pilot checklist that shows each control on an
install is [PILOT_CHECKLIST.md](../preview/PILOT_CHECKLIST.md).

Paths are relative to the repository root. `engine/…` is the Python engine, `apps/api/…` the
HTTP API, `scripts/install/…` the installer.

## 1. What runs where

BOSS is self-hosted: one Docker Compose project on a machine you control
(`scripts/install/community-compose.yml`, installed by `install.sh`). Images are built on that
machine from `Dockerfile.community`.

| Service | Role |
|---|---|
| `db` | Postgres 16 (pgvector, AGE): all BOSS state |
| `api`, `worker` | The control plane; builds run here in per-run sandboxes |
| `egress` | The outbound proxy and allowlist fence for sandboxes |
| `model-gateway` | Streams model calls to providers |
| `web` | The console |
| `mcp-tools`, `kg-mcp`, `build-services` | Tool servers; a throwaway Postgres for builds' tests |
| Optional profiles | `infisical` + `redis` (secrets), `prometheus` + `grafana`, `mailpit`, `registry`, `runner` |

All ports are published on `127.0.0.1` by default. Two can be widened: the API by `BOSS_API_BIND`,
and the app-preview origin by `BOSS_PREVIEW_BIND` (preview paths only, cookies and credentials
stripped); web, Postgres and Infisical stay on loopback (compose file, `api` ports). Repositories are kept as bare clones under `/var/lib/boss/repos`; each build works
in a throwaway clone under `/var/lib/boss/sandboxes` (compose volumes).

## 2. Data flow

```
 people ──HTTPS──▶ web/api ──▶ Postgres (all state, on your machine)
                      │
                      ├──▶ model provider (prompts + the code context an agent reads)
                      ├──▶ GitHub / GitHub Enterprise (clone, push branch, open PR, checks)
                      ├──▶ ticket systems you connect (Jira, Plane, Linear, ...)
                      └──▶ chat/email you connect (Slack, Mattermost, SMTP) — off by default
 sandboxes ──▶ egress fence ──▶ allowlisted hosts only (model APIs, package registries)
```

| Leaves the install | To | When | Code |
|---|---|---|---|
| Prompts and the repository context an agent reads | The model provider you connect | Every agent step | `engine/boss_engine/services/streaming_model_gateway.py` (`ADAPTERS`, `MODEL_HOSTS`), `services/model_proxy.py` |
| Code changes as branches and PRs | GitHub / GHES you connect | Builds | `services/github_adapter.py` (`api_base_url`); sandboxes reach GitHub only through `apps/egress/boss_egress/proxy.py` |
| Ticket reads/writes | The tracker you connect | When used | `engine/boss_engine/connectors/tickets/http.py` (per-connection host allowlist) |
| Messages | Slack, Mattermost, email | Only when real sends are switched on (`BOSS_SLACK_REAL_SEND_ALLOWED` etc., default false) | `engine/boss_engine/config.py` |
| Security events | Your SIEM | Only when you save a destination | `engine/boss_engine/identity/siem_forward.py`; test `engine/tests/test_siem_forward_1249.py` |
| Telemetry (traces, errors, metrics) | Your collector | Only when you configure it; off by default | `services/self_telemetry.py`; test `engine/tests/test_self_telemetry_867.py` (`test_nothing_configured_turns_nothing_on`) |

**Stays on the install:** all state (runs, decisions, audit log, secrets, receipts) in Postgres;
repository clones; build sandboxes. No usage data is sent to the BOSS project
([FEEDBACK_LOOP.md](../community/FEEDBACK_LOOP.md)); the installer itself clones from GitHub.

**Model data handling.** Code goes to the model provider you choose. Supported: Anthropic,
OpenAI, OpenRouter, Azure OpenAI, AWS Bedrock (gateway adapters), OpenAI-compatible endpoints,
and a local model through Ollama ([MODEL_CONNECTIONS.md](../community/MODEL_CONNECTIONS.md)).
Pick a provider and region whose terms your data allows, or a local model. Content masking is
opt-in and deliberately not applied to code (`services/redaction.py`, module docstring). A
personal-data gate blocks card numbers and national IDs in builder messages
(`services/personal_data_gate.py`). **Not yet:** a setting that forces work on a restricted
project onto a local model.

## 3. Secrets

| Control | Code | Test |
|---|---|---|
| Identity vault: AES-256-GCM envelope encryption, per-row key wrapped by a master key from `BOSS_MASTER_KEY_FILE` (0600, created on first install); only `vault.py` decrypts | `engine/boss_engine/identity/vault.py` | `engine/tests/test_identity_agents_vault_and_grants.py` (`test_a_secret_round_trips_and_the_database_holds_no_plaintext`, `test_r9_only_vault_py_decrypts`) |
| Secret references (connections, app secrets): backends `database` (default), `infisical`, Vault and others | `services/secret_backends.py` | `engine/tests/test_secret_access_log_865.py` |
| Agents never hold an API key: a session gets a short-lived run token and a loopback proxy that adds the key | `services/model_proxy.py` | `engine/tests/test_an_agent_session_never_holds_the_model_key.py` |
| Secrets are redacted from logs, error reports and run output | `engine/boss_engine/identity/redaction.py`, `services/run_secret_redaction.py` | `engine/tests/test_redaction_masks_every_credential.py`, `apps/api/tests/test_no_secret_leaves_the_log.py`, `engine/tests/test_identity_audit_redaction.py` |

Know these:
- With the `database` backend and no `BOSS_KMS_MASTER_KEY`, BOSS generates the key and stores it in
  the same database as the ciphertext; its health check says so (`secret_backends.py`,
  `key_mode=auto_provisioned`). The installer writes `BOSS_KMS_MASTER_KEY` to `.env`, so a standard
  install does not hit this. Keep `.env` and the key file out of database backups.
- A **subscription** login (Claude Code, Codex, Grok) is not an API key and is not proxied: it is
  copied into the sandbox run's home (`services/code_agent.py`, comment above the model proxy;
  `services/sandbox_identity.py`). Use API keys for the preview.

## 4. SSO and RBAC

| Control | Code | Test |
|---|---|---|
| OIDC (PKCE, nonce, JWKS), Okta helpers | `engine/boss_engine/identity/sso_oidc.py` | `apps/api/tests/test_identity_okta_sso.py`, `test_identity_authn_sso.py` |
| SAML 2.0 SP | `identity/sso_saml.py` | `apps/api/tests/test_identity_int_a_saml.py` |
| SCIM 2.0; cannot set roles; deprovision ends sessions at once | `identity/scim.py` | `apps/api/tests/test_identity_authn_scim.py`, `test_identity_scim_team_of_twenty.py` |
| Roles `owner`, `admin`, `operator`, `viewer`, `auditor` + custom roles; bindings to people or groups, at org or project scope | `identity/permissions_gen.py`, `identity/bindings.py`, `identity/roles.py` | `apps/api/tests/test_identity_authz_matrix.py`, `test_identity_role_lifecycle.py` |
| One decision point for every permission check, enforced by default (`BOSS_AUTHZ_MODE=enforce`) | `identity/authz.py::check`, `identity/declarations.py::permit`, `config.py` | `apps/api/tests/test_identity_authz_pdp.py`, `engine/tests/test_every_tool_declares_a_permission.py` |

Okta groups to roles: [OKTA_GROUPS_TO_ROLES.md](../operations/OKTA_GROUPS_TO_ROLES.md).

## 5. Human approval

| Control | Code | Test |
|---|---|---|
| Approvals need `decision:approve`, a human session (not an API key or agent) and the tier's seat; tier 3 needs two people unless the org enables `single_operator_waiver` (automatic while the org has one active person) | `identity/approvals.py::require_approval` | `apps/api/tests/test_identity_approval_seats.py` |
| A merge needs an approver independent of the change's creator, on the current head | `services/github_merges.py::authorize` | `apps/api/tests/test_github_merge_authorizations.py` |
| Production (and an unknown target, treated as production) always asks a person | `services/autonomy_policy.py` (`prod: ask`), `services/config_deploy.py::promote` | `engine/tests/test_dev_autonomy_acts_and_logs.py` (`test_production_and_unknown_targets_still_ask`) |

Know this: **outside production, agents act by default** (`autonomy_policy.py`, `dev: act`):
non-production merges and deploys do not wait for a person. Set the org (or a project) autonomy policy to `dev: ask` (`act | act_capped | ask`) if your
review requires a person on every merge. The PRD gate is auto-approved by default (`config.py`).

## 6. Audit

| Control | Code | Test |
|---|---|---|
| Security event log: append-only, hash-chained per org, written in the same transaction as the change | `identity/audit.py`; table `identity_security_events` with an append-only trigger (`db/schema.sql`) | `engine/tests/test_identity_audit_log.py` (`test_a74_the_log_is_append_only`, `test_a75_a_tampered_chain_reports_its_first_break`) |
| Export and verify: `POST /v1/identity/audit/export` (JSON with anchor hash), `POST /v1/identity/audit/verify` | `apps/api/app/routers/identity_admin.py`, `audit.py::export`, `::verify_chain` | `engine/tests/test_identity_audit_admin_api.py` |
| Activity export (CSV or NDJSON): `GET /v1/activity/export` | `apps/api/app/routers/activity.py` | — |
| Receipts for agent actions | `services/receipts.py` | `engine/tests/test_receipts_lookup.py` |
| Retention: security events 400 days by default (minimum 30), receipts kept indefinitely | `config.py` (`BOSS_IDENTITY_AUDIT_RETENTION_DAYS`, `BOSS_RECEIPT_RETENTION_DAYS`), `identity/hygiene.py` | — |

What is never logged: [IDENTITY_ADMIN.md › What is never logged](../community/IDENTITY_ADMIN.md#what-is-never-logged).

## 7. Network egress

| Control | Code | Test |
|---|---|---|
| Sandboxes can reach only loopback, the API and the egress proxy (iptables owner match on the per-run user); on by default (`BOSS_SANDBOX_NETWORK=enforce`) | `scripts/sandbox/entrypoint.sh`, `services/sandbox_identity.py::network_fenced` | `engine/tests/test_sandbox_network_fence.py` |
| The egress proxy allows a fixed list (model APIs, package registries) plus `BOSS_EGRESS_CONNECT_ALLOW`; private and metadata addresses refused; GitHub never tunnelled | `apps/egress/boss_egress/connect.py` (`DEFAULT_ALLOW`, `NEVER`, `_private_target_ok`) | `engine/tests/test_egress_enterprise_fence.py` |
| Corporate proxy (`BOSS_UPSTREAM_PROXY`) and TLS-inspection CA (`BOSS_EXTRA_CA_FILE`) | `services/enterprise_network.py`, `scripts/install/with-extra-ca.sh` | `engine/tests/test_enterprise_network.py`, `scripts/tests/test_runtime_extra_ca.py` |

Strict mode, which also removes model hosts from the sandbox allowlist, is off by default
(`BOSS_EGRESS_STRICT_SECRETS`, `connect.py::strict_secrets`). Details and gaps:
[ENTERPRISE_NETWORK.md](../community/ENTERPRISE_NETWORK.md) (see "Not covered yet").

## 8. Isolated execution

The default is a **per-run unprivileged Unix user with a network fence, inside the api/worker
container** (`BOSS_SANDBOX_PROVIDER=local`, `BOSS_SANDBOX_ISOLATION=required`;
`config.py`, `services/sandbox_identity.py`). It shares the host kernel. It is not a VM.
[SANDBOX.md](../community/SANDBOX.md) says the same.

MicroVM execution is **not available yet**: `BOSS_MICROVM_CLAIMS_FROZEN=true` by default
(`.env.example`, `config.py`), and any microVM execution claim is refused with 422
(`services/sandbox_provider.py`). Test: `engine/tests/test_microvm_claims_freeze.py`. Enable
plan: [docs/infra/MICROVM_ENABLE.md](../infra/MICROVM_ENABLE.md).

## 9. On-prem / VPC deployment

1. A Linux VM in your VPC (4 vCPU / 8 GB is the release-lane size,
   [TEST_TIERS.md](../operations/TEST_TIERS.md)), Docker installed.
2. Run the installer per [QUICKSTART.md](../community/QUICKSTART.md). Behind a proxy or TLS
   inspection, set the values in [ENTERPRISE_NETWORK.md](../community/ENTERPRISE_NETWORK.md) first.
3. Put HTTPS in front with a hostname you control ([hostname.md](../community/hostname.md)).
4. Connect SSO and SCIM ([SIGN_IN_SETUP.md](../community/SIGN_IN_SETUP.md)), then models and
   repositories.

**Not yet:** an air-gapped install. The installer clones from GitHub and builds images from public
base images (mirrors and a proxy are supported). Registry credentials and self-hosted Git through
the sandbox fence are listed under "Not covered yet" in ENTERPRISE_NETWORK.md.
