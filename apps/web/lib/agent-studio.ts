import { API_URL, authHeaders, resolveProjectId } from "./client";

export type StudioAgent = {
  id: string;
  agent_key: string;
  name: string;
  description?: string;
  owner: string;
  status: string;
  active_version_id?: string;
  active_run_count: number;
  active_version?: {
    id: string;
    version: number;
    role_key: string;
    model_policy: Record<string, unknown>;
    requested_skills: string[];
    scopes: Record<string, unknown>;
    validation: { valid?: boolean; errors?: string[]; warnings?: string[] };
  };
};

export type StudioPanel = {
  id: string;
  panel_key: string;
  name: string;
  description?: string;
  purpose: string;
  status: string;
  active_run_count: number;
  active_version?: {
    id: string;
    version: number;
    topology: string;
    members: Array<{
      member_key: string;
      agent_key: string;
      agent_name: string;
      role_key: string;
      weight: number;
      required: boolean;
      perspective?: string;
    }>;
    evidence_policy: Record<string, unknown>;
    consolidation_policy: Record<string, unknown>;
    dissent_policy: Record<string, unknown>;
  };
};

export type StudioRun = {
  id: string;
  status: string;
  agent_key?: string;
  agent_name?: string;
  panel_key?: string;
  panel_name?: string;
  member_key?: string;
  job_id?: string;
  job_status?: string;
  created_at: string;
  completed_at?: string;
  output?: Record<string, unknown>;
  evidence?: unknown[];
  blocked_reason?: string;
};

export type A2AMessage = {
  id: string;
  channel_key: string;
  message_kind: string;
  sender_kind: string;
  sender_id: string;
  status: string;
  trace_id: string;
  correlation_id: string;
  delivery_count: number;
  acknowledged_count: number;
  created_at: string;
  payload: Record<string, unknown>;
};

export type AgentStudioFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  agents: StudioAgent[];
  panels: StudioPanel[];
  agentRuns: StudioRun[];
  panelRuns: StudioRun[];
  tests: Array<{
    id: string;
    status: string;
    agent_name: string;
    created_at: string;
    result: { summary?: string; checks?: Array<{ kind: string; passed: boolean }> };
  }>;
  a2a: {
    channels: Array<{
      id: string;
      channel_key: string;
      name: string;
      status: string;
      subscriber_count: number;
      message_count: number;
    }>;
    messages: A2AMessage[];
    health: {
      status: string;
      transport?: { status?: string; configured?: boolean; latency_ms?: number };
      ledger?: Record<string, number>;
    };
  };
};

export async function fetchAgentStudioFeed(
  projectId?: string,
): Promise<AgentStudioFeed> {
  const resolved = resolveProjectId(projectId);
  try {
    const response = await fetch(
      `${API_URL}/v1/agent-studio/catalog?project_id=${encodeURIComponent(resolved)}`,
      { headers: authHeaders(), cache: "no-store" },
    );
    if (!response.ok) {
      return emptyFeed(resolved, `Agent Studio API returned ${response.status}.`);
    }
    const payload = (await response.json()) as Record<string, unknown>;
    const a2a = (payload.a2a ?? {}) as Record<string, unknown>;
    return {
      projectId: resolved,
      apiReachable: true,
      agents: (payload.agents as StudioAgent[]) ?? [],
      panels: (payload.panels as StudioPanel[]) ?? [],
      agentRuns: (payload.agent_runs as StudioRun[]) ?? [],
      panelRuns: (payload.panel_runs as StudioRun[]) ?? [],
      tests: (payload.tests as AgentStudioFeed["tests"]) ?? [],
      a2a: {
        channels: (a2a.channels as AgentStudioFeed["a2a"]["channels"]) ?? [],
        messages: (a2a.messages as A2AMessage[]) ?? [],
        health:
          (a2a.health as AgentStudioFeed["a2a"]["health"]) ?? {
            status: "unknown",
          },
      },
    };
  } catch (error) {
    return emptyFeed(
      resolved,
      error instanceof Error ? error.message : "Agent Studio API is unavailable.",
    );
  }
}

function emptyFeed(projectId: string, blockedReason: string): AgentStudioFeed {
  return {
    projectId,
    apiReachable: false,
    blockedReason,
    agents: [],
    panels: [],
    agentRuns: [],
    panelRuns: [],
    tests: [],
    a2a: {
      channels: [],
      messages: [],
      health: { status: "unavailable" },
    },
  };
}
