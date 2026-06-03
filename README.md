# BOSS

BOSS, the Business Operating System Sentinel, is a financially controlled autonomous company operating system. This repository is the real control plane and dogfood environment for BOSS itself.

## Operating invariants

- No external effect without a `Receipt`.
- No spendy action without a `BudgetReservation`.
- No worker execution without a lease.
- No silent drops.
- No self-modification without PR, tests, approval, and rollback.

## Real-only posture

This repo does not use fake providers or synthetic side effects. The first real integrations are:

- GitHub for branch, commit, and pull-request creation against `seanheiney/boss-sandbox`
- OpenAI for coding and planning calls via `OPENAI_API_KEY`

If a required credential is missing, the API must fail closed and create visible decisions and events rather than silently degrading to a mock path.

## Repo layout

- `AGENTS.md`: active repository rules
- `docs/product/PRD-v1.0.md`: governing product intent
- `api/openapi.yaml`: active API contract
- `db/schema.sql`: active starter schema
- `apps/web`: Next.js executive console shell
- `apps/api`: FastAPI control plane
- `apps/worker`: Python worker shell
- `packages/specpack`: active spec objects, workflows, and policies
- `templates/boss-sandbox`: starter contents for the real sandbox target repository
- `reference/boss_build_spec_pack`: original imported build pack

## Prerequisites

- Node `20.x`
- `pnpm` `9.x`
- Python `3.9+`
- Docker with Compose
- GitHub credentials available through `BOSS_GITHUB_TOKEN`, `GITHUB_TOKEN`, or the macOS keychain
- `OPENAI_API_KEY`

## Setup

1. Copy `.env.example` to `.env` and fill in the required secrets.
2. Run `make install`.
3. Run `make seed`.
4. Run `make dev`.

## Commands

- `make dev`: start Postgres, web, API, and worker
- `make test`: run web and Python tests
- `make lint`: run TypeScript and Python checks
- `make seed`: apply schema and seed the dogfood org/workspace/project
- `make smoke`: run a local schema/bootstrap smoke check

## GitHub bootstrap

The bootstrap script can use either an explicit GitHub token or the machine's stored macOS Git credentials:

```bash
scripts/bootstrap_github_repos.sh
```

That script creates:

- `seanheiney/boss`
- `seanheiney/boss-sandbox`

It also pushes the sandbox template and attempts to apply branch protection to `boss-sandbox/main`. If the GitHub account tier does not allow branch protection on private repositories, the script reports that entitlement limit explicitly and stops short of bypassing it.
