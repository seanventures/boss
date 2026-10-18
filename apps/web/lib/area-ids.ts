/**
 * EVERY CONSOLE AREA ID, once, as a value AND a type.
 *
 * The ids lived only as a type union inside ConsoleShell, so nothing outside it could check a
 * link target at run time, and a string prop took anything: Command Center's "Open captain"
 * linked to area="metatron" and "See your apps" to area="apps" after both ids had left the nav,
 * and the shell drops a navigate event for an unknown id, so each click did nothing (review of
 * milestone/console-b, 2026-10-02). AreaLink takes an AreaId; a value from the API goes through
 * toAreaId first.
 */
export const AREA_IDS = [
  "workforce",
  "agent",
  "dashboard",
  "overview",
  "decisions",
  "self_fixes",
  "work",
  "activity",
  "delivery_workflows",
  "portfolio",
  "discovery",
  "meetings",
  "processes",
  "code_tasks",
  "shepherd",
  "delivery",
  "monitoring",
  "observability",
  "reliability",
  "ai-sre",
  "reports",
  "budget",
  "comms",
  "github",
  "infra",
  "capabilities",
  "sandboxes",
  "foundations",
  "components",
  "ops",
  "assurance",
  "org",
  "skills",
  "setup",
  "enterprise",
] as const;

export type AreaId = (typeof AREA_IDS)[number];

const KNOWN = new Set<string>(AREA_IDS);

export function isAreaId(value: unknown): value is AreaId {
  return typeof value === "string" && KNOWN.has(value);
}

/** An area id from outside the console's own code (an API payload), or the fallback. */
export function toAreaId(value: unknown, fallback: AreaId): AreaId {
  return isAreaId(value) ? value : fallback;
}
