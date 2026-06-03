# Codex Prompt 03 — Durable job leases and worker loop

Goal: Implement Postgres-backed job leasing, worker heartbeat, completion, retries, and deadletters.

Context to read first:

- AGENTS.md
- specpack/objects/job.yaml
- specpack/workflows/budget_gated_job.yaml
- docs/TECHNICAL_SPEC.md section Job state machine

Constraints:

- Use `SELECT ... FOR UPDATE SKIP LOCKED` or equivalent safe claim logic.
- Two workers must never claim the same job.
- Jobs must have bounded retries.
- Failures must become retryable/final/deadlettered; no silent drops.

Implement:

1. Job claim function:
   - filter by capabilities/kind
   - claim one queued job
   - set lease owner/token/expires/attempt/status
2. Heartbeat function.
3. Complete function.
4. Fail function with retry/backoff/deadletter.
5. Worker process loop that polls and executes mock handlers.
6. Mock handler for `plan.generate`:
   - creates a plan artifact
   - creates a decision card
   - emits events

Done when:

- Tests prove two workers cannot claim same job.
- Tests prove expired lease reclaim.
- Tests prove retry then deadletter.
- Smoke command creates job and worker completes it.
