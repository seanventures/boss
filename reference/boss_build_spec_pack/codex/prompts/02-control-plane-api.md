# Codex Prompt 02 — Control plane API

Goal: Implement core BOSS API endpoints for commands, tasks, decisions, budget status, receipts, and events.

Context to read first:

- AGENTS.md
- api/openapi.yaml
- specpack/workflows/ceo_command_to_pr.yaml
- specpack/workflows/decision_inbox.yaml

Constraints:

- Every mutation must emit an event.
- Use typed request/response schemas.
- Do not build auth yet; include TODOs and project_id scope.
- Do not implement real model/tool calls.

Implement:

1. `POST /v1/commands/propose`
   - Creates task from CEO command.
   - Creates initial planning job.
   - Emits events.
2. `GET /v1/status`
   - Returns top-level counts: open tasks, pending decisions, queued jobs, deadletters, budget state.
3. `GET /v1/tasks` and `GET /v1/tasks/{id}`.
4. `GET /v1/decisions/inbox`.
5. `POST /v1/decisions/{id}/approve` and `/reject`.
6. `GET /v1/events`.
7. `GET /v1/receipts/{dedupe_key}`.

Done when:

- API tests cover command creation, decision approval/rejection, and event feed.
- No endpoint mutates state without an event.
