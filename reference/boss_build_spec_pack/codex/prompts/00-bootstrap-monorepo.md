# Codex Prompt 00 — Bootstrap BOSS monorepo

Goal: Scaffold the BOSS v0 monorepo with web, api, worker, local Postgres, tests, and developer commands.

Context to read first:

- AGENTS.md
- docs/PRD.md
- docs/TECHNICAL_SPEC.md
- docs/BUILD_PLAN.md
- db/schema.sql

Constraints:

- Follow AGENTS.md.
- Do not integrate real external services.
- Keep all model/tool/infra integrations behind mockable adapter interfaces.
- Prefer the recommended stack unless there is a strong reason not to:
  - apps/web: Next.js + TypeScript
  - apps/api: FastAPI + Pydantic + SQLAlchemy + Alembic
  - apps/worker: Python worker process
  - Postgres for local DB
- Create simple Makefile commands: `make dev`, `make test`, `make lint`, `make seed`, `make smoke`.
- Add README setup instructions.

Implement:

1. Repo structure:
   - apps/web
   - apps/api
   - apps/worker
   - packages/specpack
   - infra/docker-compose.yml
2. Minimal API health endpoint.
3. Minimal web dashboard that calls API health/status or displays a placeholder if API unavailable.
4. Worker process that starts, logs, and exits cleanly or idles safely.
5. Test scaffolding for API and web.
6. Local `.env.example` files.

Done when:

- The repo has a coherent scaffold.
- `make test` runs at least minimal tests.
- `make lint` exists, even if initially simple.
- README documents local startup.

Before finishing:

- Review your diff against AGENTS.md.
- List follow-up tasks for Prompt 01.
