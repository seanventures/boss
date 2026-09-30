/**
 * Focused always-on agents. Mapper never invents a running agent when the API
 * is missing.
 */

export const FOCUS_KIND_OPTIONS = [
  "preview_keeper",
  "queue_helper",
  "factory_driver",
  "reviewer",
  "custom",
] as const;

export const FOCUS_MODE_OPTIONS = ["always_on", "hybrid", "on_demand"] as const;

export type FocusedAgent = {
  id: string;
  name: string;
  focus: string;
  kind: string;
  status: string;
  mode: string;
  intervalSeconds: number;
  allowedActions: string[];
  lastStatus?: string;
  lastTickAt?: string;
};

export type FocusedAgentState = {
  apiReachable: boolean;
  blockedReason?: string;
  agents: FocusedAgent[];
};

export function emptyFocusedAgents(reason?: string): FocusedAgentState {
  return {
    apiReachable: false,
    blockedReason: reason,
    agents: [],
  };
}

export function mapFocusedAgentList(
  payload: Record<string, unknown>,
): FocusedAgentState {
  const raw = Array.isArray(payload.agents) ? payload.agents : [];
  const agents = raw.flatMap((item, index) => {
    if (!item || typeof item !== "object") return [];
    const row = item as Record<string, unknown>;
    const name = typeof row.name === "string" ? row.name : "";
    if (!name) return [];
    const actions = Array.isArray(row.allowed_actions)
      ? row.allowed_actions.filter((value): value is string => typeof value === "string")
      : [];
    return [
      {
        id: typeof row.id === "string" && row.id ? row.id : `agent-${index}`,
        name,
        focus: typeof row.focus === "string" ? row.focus : "",
        kind: typeof row.kind === "string" ? row.kind : "custom",
        status: typeof row.status === "string" ? row.status : "draft",
        mode: typeof row.mode === "string" ? row.mode : "on_demand",
        intervalSeconds:
          typeof row.interval_seconds === "number" ? row.interval_seconds : 120,
        allowedActions: actions,
        lastStatus: typeof row.last_status === "string" ? row.last_status : undefined,
        lastTickAt:
          typeof row.last_tick_at === "string" ? row.last_tick_at : undefined,
      },
    ];
  });
  return { apiReachable: true, agents };
}
