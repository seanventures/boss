/**
 * WHAT OPENING EACH CONSOLE AREA NEEDS — the console's reading of it.
 *
 * The authority is S2's AREA_ACCESS (areas.ts). Until S2 declares every area, the draft table of
 * IDENTITY_PERMISSIONS.md §8 stands in for the undeclared ones, so the rail and the direct-URL
 * guard mirror access from day one. INTEGRATION SEAM: once AREA_ACCESS is total (the shrinking
 * list tests/identity/undeclared_console_areas.txt is deleted), DRAFT_AREA_ACCESS is deleted
 * with it; tests/identity-mirroring.test.ts fails if the two ever disagree on a declared area.
 */
import type { AreaId } from "../area-ids";
import { AREA_ACCESS, type AreaAccess } from "./areas";

export const DRAFT_AREA_ACCESS: Readonly<Record<AreaId, AreaAccess>> = {
  overview: { permission: "project:read" },
  code_tasks: { permission: "code_task:read" },
  delivery_workflows: { permission: "workflow:read" },
  workforce: { permission: "agent:read" },
  decisions: { permission: "decision:read" },
  infra: { permission: "infra:read" },
  observability: { permission: "observability:read" },
  activity: { permission: "activity:read" },
  discovery: { permission: "discovery:read" },
  meetings: { permission: "meeting:read" },
  agent: { permission: "assistant:chat" },
  dashboard: { permission: "work_item:read" },
  work: { permission: "work_item:read" },
  portfolio: { permission: "work_item:read" },
  processes: { permission: "autopilot:read" },
  shepherd: { permission: "autopilot:read" },
  reliability: { permission: "reliability:read" },
  "ai-sre": { permission: "reliability:read" },
  reports: { permission: "activity:read" },
  setup: { permission: "setup:read" },
  skills: { permission: "skill:read" },
  budget: { permission: "budget:read" },
  pipeline: { permission: "pipeline:read" },
  comms: { permission: "comms:read" },
  ops: { permission: "system:read" },
  assurance: { permission: "assurance:read" },
  org: { permission: "pack:read" },
  github: { permission: "github:read" },
  capabilities: { permission: "skill:read" },
  sandboxes: { permission: "sandbox:read" },
  foundations: { permission: "system:read" },
  components: { permission: "system:read" },
  enterprise: { permission: "directory:read" },
  // Retired ids that still resolve (app/legacy-areas.ts) keep their new home's read permission.
  delivery: { permission: "infra:read" },
  monitoring: { permission: "observability:read" },
};

/** S2's declaration when there is one, else the draft. */
export function accessForArea(id: AreaId): AreaAccess {
  return AREA_ACCESS[id] ?? DRAFT_AREA_ACCESS[id];
}
