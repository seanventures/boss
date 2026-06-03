# BOSS Build Spec Pack for Codex

BOSS = **Business Operating System Sentinel**: a financially controlled autonomous company OS that dogfoods itself by turning CEO commands into specs, decisions, budget-gated jobs, validated PRs, receipts, and audit trails.

This pack is designed to be dropped into a new repo and given to Codex. It contains:

- `AGENTS.md` — repository instructions Codex should follow on every task.
- `.codex/config.example.toml` — safe local Codex configuration template.
- `docs/` — PRD, technical spec, roadmap, build plan, and Codex runbook.
- `specpack/` — copy/pasteable canonical object, role, policy, workflow, model, skill, and eval specs.
- `api/openapi.yaml` — minimal v0 API contract.
- `db/schema.sql` — starter relational schema.
- `codex/prompts/` — staged prompts to paste into Codex for implementation.
- `skills/` — optional Codex Skill packages for repeated BOSS workflows.

## Recommended v0 stack

- Web: Next.js + TypeScript + Tailwind
- API: FastAPI + Pydantic + SQLAlchemy + Alembic
- Worker: Python worker process polling Postgres leases
- DB: Postgres
- Object storage: local filesystem in v0; S3/R2/GCS later
- Model gateway: interface-first in v0; LiteLLM integration later
- Software factory: interface-first in v0; OpenHands/Codex/OpenHands-style coding worker later

## First vertical slice

CEO command → Task → Plan artifact → Budget quote/reservation → Job lease → Worker execution → Decision card → Approval → Receipt → Event feed.

Do not build every agent. Build the control plane first.

## Quick start with Codex

1. Create a new repo.
2. Copy this pack into the repo root.
3. Copy `.codex/config.example.toml` to `.codex/config.toml` and edit for your environment.
4. Run Codex from the repo root.
5. Ask Codex: `Summarize the AGENTS.md instructions and the first implementation milestone.`
6. Paste `codex/prompts/00-bootstrap-monorepo.md`.
7. Work through prompts in order.

## Non-negotiable architecture rules

- No external side effect without a `Receipt`.
- No spendy action without a `BudgetReservation`.
- No worker execution without a lease.
- No silent drops: every inbound/outbound message has ACK, escalation, or deadletter.
- No uncontrolled self-modification: BOSS changes itself only through PRs, tests, approvals, and receipts.
