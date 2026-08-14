import { API_URL, PROJECT_ID, authHeaders, resolveProjectId } from "./client";

export type OncallParticipant = {
  user_id: string;
  contact?: string;
  display_name?: string;
};

export type OncallLayer = {
  name: string;
  rotation_type?: string;
  handoff_seconds?: number;
  start_at?: string;
  participants: OncallParticipant[];
};

export type OncallSchedule = {
  id: string;
  project_id: string;
  name: string;
  timezone: string;
  layers: OncallLayer[];
  enabled: boolean;
  created_at?: string;
  updated_at?: string;
};

export type OncallPolicyStep = {
  wait_minutes: number;
  channels: { adapter: string; target: string }[];
  targets?: string[];
};

export type OncallPolicy = {
  id: string;
  project_id: string;
  name: string;
  schedule_id?: string | null;
  steps: OncallPolicyStep[];
  created_at?: string;
  updated_at?: string;
};

export type OncallPage = {
  id: string;
  project_id: string;
  reliability_incident_id?: string | null;
  dedupe_key: string;
  title: string;
  severity: string;
  status: string;
  schedule_id?: string | null;
  policy_id?: string | null;
  assignee?: string;
  service?: string;
  kg_node_id?: string | null;
  kg_node_key?: string | null;
  adapter?: string;
  notify_result?: Record<string, unknown>;
  created_at?: string;
  acked_at?: string | null;
  resolved_at?: string | null;
};

export type OncallHolder = {
  schedule_id: string;
  schedule_name: string;
  layer?: string;
  user_id?: string;
  contact?: string;
  display_name?: string;
};

export type OncallAdapter = {
  adapter: string;
  live_capable: boolean;
  token_env: string;
};

export type OncallSreStatus = {
  project_id: string;
  open_pages: number;
  acked_pages: number;
  pages_open: OncallPage[];
  pages_acked: OncallPage[];
  schedules: OncallSchedule[];
  policies: OncallPolicy[];
  current_oncall: OncallHolder[];
  adapters: OncallAdapter[];
  as_of?: string;
  apiReachable: boolean;
};

async function getJson<T>(path: string): Promise<T | null> {
  try {
    const r = await fetch(`${API_URL}${path}`, {
      headers: authHeaders(),
      cache: "no-store",
    });
    if (!r.ok) return null;
    return (await r.json()) as T;
  } catch {
    return null;
  }
}

export async function fetchOncallSreStatus(
  projectId = PROJECT_ID,
): Promise<OncallSreStatus> {
  const resolved = resolveProjectId(projectId);
  const q = `project_id=${encodeURIComponent(resolved)}`;
  const status = await getJson<Omit<OncallSreStatus, "apiReachable">>(
    `/v1/oncall-sre/status?${q}`,
  );
  if (!status) {
    return {
      project_id: resolved,
      open_pages: 0,
      acked_pages: 0,
      pages_open: [],
      pages_acked: [],
      schedules: [],
      policies: [],
      current_oncall: [],
      adapters: [],
      apiReachable: false,
    };
  }
  return { ...status, apiReachable: true };
}

export async function fetchOncallPages(
  projectId = PROJECT_ID,
  status?: string,
): Promise<OncallPage[]> {
  const resolved = resolveProjectId(projectId);
  const params = new URLSearchParams({ project_id: resolved, limit: "50" });
  if (status) params.set("status", status);
  const data = await getJson<{ items: OncallPage[] }>(
    `/v1/oncall-sre/pages?${params.toString()}`,
  );
  return data?.items ?? [];
}
