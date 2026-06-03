# BOSS Codex Shortcut Prompts

## Plan first

```text
Read AGENTS.md and the relevant docs/specpack files for this task. Do not code yet. Produce a short implementation plan, expected files changed, tests to add, and risks. Wait for approval.
```

## Implement small slice

```text
Implement the approved slice only. Follow AGENTS.md. Keep changes small. Add or update tests. Run the relevant checks. Before finishing, review your diff for missing receipts, reservations, events, tenant scope, and tests.
```

## Review against BOSS invariants

```text
Review the current diff against BOSS invariants: no external effect without receipt, no spendy action without reservation, no job without lease, no silent drops, no self-modification bypass. List issues by severity and propose fixes.
```

## Add tests for state machine

```text
Add tests for the state machine transitions touched by this change. Include invalid transitions and failure/deadletter paths. Do not add unrelated features.
```
