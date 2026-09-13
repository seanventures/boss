import { API_URL, PROJECT_ID, authHeaders, resolveProjectId } from "./client";

export type OssComponent = {
  id: string;
  name: string;
  purpose: string;
  role: string;
  boss_integration: string;
  deployed_by_boss_stack: boolean;
  stars_note: string;
  default_ports?: number[];
  homepage?: string;
};

export type WorkloadProfile = {
  id: string;
  title: string;
  description: string;
  component_ids: string[];
};

export type SuiteConnector = {
  id: string;
  kind: string;
  name: string;
  enabled: boolean;
  base_url?: string | null;
};

export type SuiteGap = {
  id: string;
  severity: string;
  message: string;
};

export type ObservabilitySuiteStatus = {
  project_id: string;
  generated_at?: string;
  connectors: {
    registered: SuiteConnector[];
    count: number;
    enabled_count: number;
    kinds_present: string[];
    catalog_kinds: string[];
  };
  stack: {
    deployed: boolean;
    scrape_app_count: number;
    apps: { app?: string; environment?: string; port?: number }[];
    host_status: { running?: boolean | null; probed?: boolean; error?: string };
    components_covered_when_deployed: string[];
  };
  recommended_oss: {
    items: OssComponent[];
    count: number;
    profiles: string[];
  };
  gaps: SuiteGap[];
};

export type ObservabilitySuiteCatalog = {
  items: OssComponent[];
  count: number;
  profiles: WorkloadProfile[];
};

export type RecommendResult = {
  workload_profile: string;
  title: string;
  description: string;
  component_ids: string[];
  components: OssComponent[];
  boss_stack_covers: string[];
  add_alongside_boss_stack: string[];
  next_steps: string[];
};

export type HealthCheckItem = {
  id?: string;
  name?: string;
  kind?: string;
  status: string;
  reachable?: boolean | null;
  config_ready?: boolean;
  config_error?: string;
  note?: string;
};

export type HealthCheckResult = {
  project_id: string;
  dry_run: boolean;
  count: number;
  ok_count: number;
  items: HealthCheckItem[];
};

export type ObservabilitySuiteFeed = {
  projectId: string;
  apiReachable: boolean;
  catalog: ObservabilitySuiteCatalog | null;
  status: ObservabilitySuiteStatus | null;
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

async function postJson<T>(path: string, body: unknown): Promise<T | null> {
  try {
    const r = await fetch(`${API_URL}${path}`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
      cache: "no-store",
    });
    if (!r.ok) return null;
    return (await r.json()) as T;
  } catch {
    return null;
  }
}

export async function fetchObservabilitySuiteCatalog(): Promise<ObservabilitySuiteCatalog | null> {
  return getJson<ObservabilitySuiteCatalog>("/v1/observability-suite/catalog");
}

export async function fetchObservabilitySuiteStatus(
  projectId = PROJECT_ID,
  hostTargetId?: string,
): Promise<ObservabilitySuiteStatus | null> {
  const resolved = resolveProjectId(projectId);
  const q = new URLSearchParams({ project_id: resolved });
  if (hostTargetId) q.set("host_target_id", hostTargetId);
  return getJson<ObservabilitySuiteStatus>(`/v1/observability-suite/status?${q}`);
}

export async function fetchObservabilitySuiteFeed(
  projectId = PROJECT_ID,
  hostTargetId?: string,
  active = true,
): Promise<ObservabilitySuiteFeed> {
  const resolved = resolveProjectId(projectId);
  // Not rendered this request. Same shape the unreachable path returns, no round trip.
  if (!active) {
    return {
      projectId: resolved,
      apiReachable: false,
      catalog: null,
      status: null,
    };
  }
  const [catalog, status] = await Promise.all([
    fetchObservabilitySuiteCatalog(),
    fetchObservabilitySuiteStatus(resolved, hostTargetId),
  ]);
  return {
    projectId: resolved,
    apiReachable: catalog !== null || status !== null,
    catalog,
    status,
  };
}

export async function recommendObservabilityStack(
  workloadProfile: string,
): Promise<RecommendResult | null> {
  return postJson<RecommendResult>("/v1/observability-suite/recommend", {
    workload_profile: workloadProfile,
  });
}

export async function healthCheckObservabilitySources(
  projectId = PROJECT_ID,
  opts?: { dryRun?: boolean; sourceIds?: string[] },
): Promise<HealthCheckResult | null> {
  return postJson<HealthCheckResult>("/v1/observability-suite/health-check", {
    project_id: resolveProjectId(projectId),
    actor: "executive_console",
    dry_run: opts?.dryRun ?? true,
    source_ids: opts?.sourceIds,
  });
}
