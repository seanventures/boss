# Model Entitlement Broker

Verified against official provider documentation on June 1, 2026.

## Purpose

The Model Entitlement Broker is the control-plane service that decides which AI capacity source BOSS may use for a given task. It exists to keep BOSS aligned with provider rules, budget limits, reliability needs, and risk boundaries.

BOSS must treat model access as governed capacity, not as an undifferentiated pool of "AI compute".

## Supported source kinds

- `official_cli`
- `official_coding_agent`
- `official_api`
- `approved_local_model`

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
  - Codex/ChatGPT entitlement for founder or sandbox workflows
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
  - Claude Code entitlement for founder or sandbox workflows
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
  - `official_api`
- Preferred use:
  - API-backed execution only
- Hard notes:
  - xAI documents per-model RPM and TPM limits for API usage
  - consumer services may be rate-limited at xAI's discretion
  - safest initial route is the official API, not consumer surfaces

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
