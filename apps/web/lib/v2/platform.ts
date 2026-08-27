import { API_URL, authHeaders } from "../client";

/**
 * Workflows, Infra & Models, Settings — what this company is CONFIGURED with.
 *
 * Several things the reference offers do not exist on this deployment: workflow versions,
 * fleet load, SSO, human seats, listable API keys. Each is reported as absent WITH a reason
 * rather than rendered as an empty form, because an empty SSO form implies SSO is configurable
 * here and merely unconfigured. That is a different and false claim.
 */

export type V2Stage = {
  key: string;
  action: string;
  role: string;
  /** PANEL / PARALLEL / JOIN / SEQUENTIAL, derived from the spec's own kind, next and
   *  wait_for. The spec is a DAG and was being read as a flat list. */
  kind: "PANEL" | "PARALLEL" | "JOIN" | "SEQUENTIAL";
  next: string[];
  waits_for: string[];
  /** The entry point cannot reach this stage, so it will never run. */
  unreachable: boolean;
  /** null when the spec has no gating vocabulary at all — "does not stop" and "does not say"
   *  are different facts, and the live template records neither field on any of its steps. */
  gated: boolean | null;
};

export type V2Workflow = {
  id: string;
  key: string;
  name: string;
  description: string;
  status: string;
  created_by: string | null;
  runs_30d: number;
  running_now: number;
  last_run_at: string | null;
  stages: V2Stage[];
  /** Counted from the stages, never estimated. */
  panels: number;
  parallel_points: number;
  roles: string[];
};

export type WorkflowsView = {
  project_id: string;
  workflows: V2Workflow[];
  versioned: boolean;
  version_note: string;
  gates_recorded: boolean;
  gates_note: string;
  /** False: BOSS has no template-editing API, so the reference's + Add stage / Deploy version /
   *  remove-stage controls would be three buttons that cannot act. */
  editable: boolean;
  apiReachable: boolean;
  blockedReason?: string;
};

export type V2Host = {
  id: string; name: string; address: string | null; arch: string | null;
  status: string; verified: boolean;
};
export type V2Target = {
  id: string; app_name: string; repo: string | null; environment: string | null;
  kind: string | null; ref: string | null; status: string; retired: boolean;
  /** Where the app answers. null when the target records no address — not a link. */
  domains: string[];
  url: string | null;
  /** The registered host this lands on, or null rather than a guess: an app filed under the
   *  wrong machine is worse than one filed under none. */
  host: string | null;
  last_deploy: {
    status: string; commit: string | null; finished_at: string | null;
    failure_reason: string | null;
  } | null;
};
export type V2Model = {
  model: string; calls: number; tokens: number; spend_usd: number; estimated: boolean;
};
export type V2Integration = {
  id: string; name: string; url: string | null; transport: string | null;
  enabled: boolean; risk_tier: number; data_boundary: string;
};

export type InfraView = {
  project_id: string;
  hosts: V2Host[];
  targets: V2Target[];
  models: V2Model[];
  integrations: V2Integration[];
  load_recorded: boolean;
  load_note: string;
  apiReachable: boolean;
  blockedReason?: string;
};

export type SettingsView = {
  project_id: string;
  roles: { key: string; name: string; autonomy_level: number; trust_score: number }[];
  repos: { name: string; runs: number; prs: number; last_run_at: string | null }[];
  unavailable: { id: string; label: string; reason: string }[];
  apiReachable: boolean;
  blockedReason?: string;
};

async function get<T>(path: string, projectId: string, empty: T): Promise<T> {
  try {
    const r = await fetch(
      `${API_URL}/v1/console/v2/${path}?project_id=${encodeURIComponent(projectId)}`,
      { headers: authHeaders({}), cache: "no-store" },
    );
    if (!r.ok) {
      return { ...empty, apiReachable: false, blockedReason: `API returned ${r.status}` };
    }
    return { ...(await r.json()), apiReachable: true };
  } catch (e) {
    return { ...empty, apiReachable: false, blockedReason: String((e as Error)?.message ?? e) };
  }
}

export function fetchWorkflows(projectId: string): Promise<WorkflowsView> {
  return get("workflows", projectId, {
    project_id: projectId, workflows: [], versioned: false, version_note: "",
    gates_recorded: false, gates_note: "", editable: false, apiReachable: false,
  });
}

export function fetchInfra(projectId: string): Promise<InfraView> {
  return get("infra", projectId, {
    project_id: projectId, hosts: [], targets: [], models: [], integrations: [],
    load_recorded: false, load_note: "", apiReachable: false,
  });
}

export function fetchSettings(projectId: string): Promise<SettingsView> {
  return get("settings", projectId, {
    project_id: projectId, roles: [], repos: [], unavailable: [], apiReachable: false,
  });
}
