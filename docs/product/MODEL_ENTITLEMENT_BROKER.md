# Model Entitlement Broker

Verified against official provider documentation on June 1, 2026.

## Purpose

The Model Entitlement Broker is the control-plane service that decides which AI capacity source BOSS may use for a given task. It exists to keep BOSS aligned with provider rules, budget limits, reliability needs, and risk boundaries.

BOSS must treat model access as governed capacity, not as an undifferentiated pool of "AI compute".

In PRD v2.0, model-provider accounts, usage/cost sync, quota health, and API key ownership also participate in ProviderOps. The Model Entitlement Broker chooses eligible AI capacity; the Resource Plane Kernel tracks provider accounts, cost events, health, commitments, and receipts for provider-side actions.

## Supported source kinds

- `official_cli`
- `official_coding_agent`
- `official_api`
- `approved_local_model`

## Supported connection methods

- `api_key`: official provider API keys for usage-billed production paths.
- `official_cli_oauth`: a local official CLI session connected by the entitled user.
- `official_device_oauth`: a local or headless device-code session exposed by an official provider CLI.
- `official_browser_oauth`: a browser-based official provider login flow.
- `local_host`: approved local model capacity owned by an explicit runner host.

Subscription-backed public model sources must have a connected official OAuth/CLI session before routing. BOSS stores only a `connection_ref` or `SecretRef`, not raw OAuth tokens.

## Connection lifecycle

BOSS does not perform stealth login flows or collect raw provider OAuth tokens. A connection is recorded only after an entitled operator has completed the provider's official login flow through an official CLI, coding agent, device flow, browser OAuth flow, API key, or approved local runner.

Connection operations:

- `connect`: records the source's `connection_ref`, connection method, non-secret metadata, scopes, and observed capacity; emits `entitlement.source_connected`.
- `disconnect`: revokes the local connection reference, marks credentials absent, resets capacity to `unknown`, and emits `entitlement.source_disconnected`.
- invalid subscription connections using non-OAuth methods are rejected before route eligibility.

Supported `connection_status` values are `not_connected`, `connected`, `expired`, `revoked`, and `unsupported`.

## Runtime API surfaces

The v2 broker API is API-first and UI-agnostic:

- `GET /v1/model-adapters`: lists adapter definitions from the provider registry, including access class, auth method, supported models, workload rules, runtime support, and policy notes.
- `GET /v1/entitlement-pools`: lists normalized project-scoped entitlement pools derived from configured sources.
- `POST /v1/entitlement-pools/{id}/probe`: records operator/provider-observed capacity and writes a durable `CapacityReceipt`; repeated calls with the same `dedupe_key` return the prior receipt and do not mutate capacity again.
- `POST /v1/model-route-decisions/simulate`: evaluates routing through the same fail-closed policy path as runtime selection and persists a canonical `ModelRouteDecision`.
- `GET /v1/model-route-decisions`: lists recent canonical route decisions with selected pool, rejected-source reasons, capacity snapshot, budget state, and policy result.

Capacity probes do not scrape providers or consumer interfaces. They only record authorized capacity observations supplied through BOSS control-plane APIs or future approved provider adapters.

## Capacity states

Observed capacity is policy input, not a best-effort hint. Supported `capacity_state` values are:

- `available`
- `degraded`
- `exhausted`
- `rate_limited`
- `blocked`
- `disabled`
- `suspended`
- `unavailable`
- `unknown`

`exhausted`, `rate_limited`, `blocked`, `disabled`, `suspended`, and `unavailable` sources fail closed during route selection. Numeric counters such as `remaining_units`, `remaining_requests`, or `remaining_tokens` at zero also fail closed. Reset and observation timestamps should be exposed as `reset_at`, `rate_limit_reset_at`, `observed_at`, or `last_checked_at`.

## Routing rule

For each eligible task, BOSS should choose the best source that satisfies all of the following:

- provider rules permit the workload
- tenant/workload class is allowed for that source
- current observed capacity is sufficient
- budget reservation exists
- risk tier is within the source policy ceiling
- quality and task-fit are acceptable

If no eligible source remains, BOSS must deny execution, emit an event, and create a visible decision or escalation.

## Non-negotiable guardrail

BOSS must never:

- scrape consumer web or desktop interfaces
- call undocumented or private provider endpoints
- share personal or team credentials across users or tenants
- evade or rotate around provider limits
- use personal subscriptions as shared backend infrastructure for multi-tenant customer workloads

Optimize across limits. Do not bypass limits.

## Workload classes

- `owner_sandbox`: founder-owned, interactive, dogfood, or explicitly approved sandbox work
- `single_tenant_customer`: dedicated customer workspace with explicit source approval
- `multi_tenant_customer`: shared production or customer-serving workloads
- `internal_batch`: internal operational work that still requires policy approval

## Initial provider posture

### OpenAI

- Allowed source kinds:
  - `official_cli`
  - `official_api`
- Preferred use:
  - Codex/ChatGPT entitlement for founder or sandbox workflows after official Codex login
  - OpenAI API for durable production or multi-tenant execution
- Hard notes:
  - Codex usage limits depend on plan and count toward agentic usage
  - API billing is separate from ChatGPT billing
  - OpenAI terms prohibit credential sharing, programmatic extraction from consumer services, and rate-limit circumvention

### Anthropic

- Allowed source kinds:
  - `official_cli`
  - `official_api`
- Preferred use:
  - Claude Code entitlement for founder or sandbox workflows after official Claude login
  - Anthropic API for dedicated production paths if enabled later
- Hard notes:
  - Claude and Claude Code usage share the same plan limits
  - source selection must account for shared capacity depletion across product surfaces

### Google

- Allowed source kinds:
  - `official_cli`
  - `official_coding_agent`
  - `official_api`
- Preferred use:
  - Gemini CLI or Code Assist only when the license/workspace rules match the workload
  - Google APIs for more durable production execution later
- Hard notes:
  - Gemini Code Assist agent-mode and Gemini CLI quotas are combined
  - quotas vary by edition or license type

### Cursor

- Allowed source kinds:
  - `official_cli`
  - `official_coding_agent`
- Preferred use:
  - automation and scripting workflows owned by the entitled operator
- Hard notes:
  - Cursor positions the CLI for scripts and automation workflows
  - Cursor states the CLI can run multiple agents in parallel
  - BOSS must still honor subscription scope and must not repurpose Cursor as hidden shared backend infrastructure

### xAI

- Allowed source kinds:
  - `official_cli`
  - `official_api`
- Preferred use:
  - Grok Build OAuth/OIDC CLI sessions for owner or single-tenant coding workflows
  - API-backed execution for production or multi-tenant workloads
- Hard notes:
  - Grok Build documents browser OIDC and device-code authentication
  - xAI documents per-model RPM and TPM limits for API usage
  - Grok/X consumer subscription benefits are separate from xAI API billing
  - consumer web/app surfaces remain out of bounds for BOSS automation

### Local models

- Allowed source kinds:
  - `approved_local_model`
- Requirements:
  - explicit approval
  - host inventory and ownership
  - resource limits
  - observability
  - same receipt/reservation/audit flow as external providers

## Required source record fields

- `source_id`
- `provider`
- `source_kind`
- `entitlement_kind`
- `owner_type`
- `owner_id`
- `workload_classes_allowed`
- `multi_tenant_allowed`
- `credential_ref`
- `auth_method`
- `connection_status`
- `connection_ref`
- `oauth_scopes`
- `connection_metadata`
- `cost_mode`
- `limit_shape`
- `supported_models`
- `task_strengths`
- `risk_tier_max`
- `status`
- `policy_notes`

## Required broker outputs

- selected source and reason
- rejected sources and why they were rejected
- estimated cost and capacity impact
- reservation requirement
- provider-rule compliance result
- fallback order
- selected source auth method, connection reference, and capacity state
- rejected source provider, source kind, auth method, connection state, capacity state, reset timestamp, and rejection reason

## Official references

- [OpenAI Codex plan usage](https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan)
- [OpenAI API billing separate from ChatGPT](https://help.openai.com/en/articles/8156019)
- [OpenAI Terms of Use](https://openai.com/policies/terms-of-use/)
- [Anthropic Claude Code with Pro or Max](https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan)
- [Google Gemini Code Assist quotas](https://developers.google.com/gemini-code-assist/resources/quotas)
- [Cursor Agent CLI](https://cursor.com/blog/cli)
- [Cursor CLI](https://cursor.com/en-US/cli)
- [xAI API rate limits](https://docs.x.ai/developers/rate-limits)
- [xAI consumer terms](https://x.ai/legal/terms-of-service)
