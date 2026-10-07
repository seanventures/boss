/**
 * What Organization's writes (./org-actions.ts) need — the catalogue permission each API route
 * checks: PUT /v1/kit/{ability}/holders and POST /v1/kit/recommended are `agent_ability:assign`,
 * PATCH /v1/agents/{role_key} is `agent:update`. The buttons that press them gate on the same
 * keys (components/gated.tsx; tests/every-call-action-button-is-pre-greyed.test.ts).
 */
import type { Permission } from "../../lib/identity/permissions.gen";

export const ORG_GIVE_PERMISSION: Permission = "agent_ability:assign";
export const ORG_MOVE_PERMISSION: Permission = "agent:update";
