# Codex Prompt 05 — Decision Inbox UI and Executive Console

Goal: Build the first BOSS web UI: executive console, budget widget, event feed, and decision inbox.

Context to read first:

- AGENTS.md
- docs/PRD.md MVP user flows
- specpack/workflows/decision_inbox.yaml
- api/openapi.yaml

Constraints:

- Keep UI simple and functional.
- No auth yet; use seeded demo project selector/config.
- All approve/reject actions must call API and show result.

Implement:

1. Executive console page:
   - what changed/event feed
   - open task count
   - pending decisions
   - job queue/deadletter counts
   - budget state widget
2. Decision inbox page:
   - cards with recommendation, options, risk, cost, deadline/default
   - approve/reject buttons
3. Task detail or simple task list.
4. Basic loading/error states.
5. Frontend tests for decision card rendering.

Done when:

- User can create command via API or simple form.
- Pending decision appears.
- User can approve/reject.
- Event feed updates after action.
