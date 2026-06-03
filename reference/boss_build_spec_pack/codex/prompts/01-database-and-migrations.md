# Codex Prompt 01 — Database schema and migrations

Goal: Implement BOSS canonical database schema with migrations and seed data.

Context to read first:

- AGENTS.md
- db/schema.sql
- specpack/objects/*.yaml
- docs/TECHNICAL_SPEC.md

Constraints:

- Use Postgres.
- Use migrations, not ad hoc schema creation.
- Include tenant scope checks in model relationships where possible.
- Keep IDs as strings for now.

Implement:

1. SQLAlchemy models or equivalent backend ORM models for:
   - orgs, workspaces, projects, roles
   - tasks, artifacts, decisions, jobs
   - receipts
   - budget_policies, budget_allocations, budget_reservations, cost_receipts
   - outbox_messages, inbox_messages, deadletters, events
2. Alembic migration based on `db/schema.sql`.
3. Seed script creating:
   - org: BOSS Demo Org
   - workspace: BOSS Workspace
   - project: boss-dogfood
   - roles from `specpack/roles/*.yaml`
   - default safety and budget policies
4. Tests proving migrations run and seed creates expected data.

Done when:

- Local DB can migrate from empty.
- Seed script is idempotent.
- Tests pass.
