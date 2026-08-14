import { API_URL, PROJECT_ID, resolveProjectId, authHeaders } from "./client";

export type CICDVendor = {
  id: string;
  name: string;
  config_path: string;
  generates_config: boolean;
  live_adapter: boolean;
  adapter_kind: string | null;
  description: string;
  languages: string[];
};

export type CICDCatalog = {
  vendors: CICDVendor[];
  count: number;
  supported_generate_targets: string[];
  notes: string[];
  apiReachable: boolean;
};

export type CICDGenerateResult = {
  target: string;
  vendor: string;
  path: string;
  content: string;
  checksum: string;
  commands: Record<string, string>;
};

export type CICDValidateResult = {
  valid: boolean;
  vendor: string | null;
  errors: string[];
  warnings: string[];
  summary: {
    keys: string[];
    kind: string | null;
    top_level_count?: number;
    has_stages?: boolean;
  };
};

export type CICDRun = {
  id: string;
  project_id: string;
  vendor: string;
  status: string;
  external_id?: string | null;
  repository?: string | null;
  ref?: string | null;
  workflow?: string | null;
  commit_sha?: string | null;
  url?: string | null;
  dedupe_key?: string | null;
  delivery_run_id?: string | null;
  deployment_run_id?: string | null;
  metadata?: Record<string, unknown>;
  actor?: string | null;
  recorded_at: string;
  source: string;
};

export type CICDStatus = {
  project_id: string;
  vendors: CICDVendor[];
  supported_generate_targets: string[];
  recent_runs: CICDRun[];
  run_counts: {
    total_listed: number;
    by_vendor: Record<string, number>;
    by_status: Record<string, number>;
  };
  links: Record<string, string>;
  notes: string[];
  apiReachable: boolean;
};

export function emptyCatalog(): CICDCatalog {
  return {
    vendors: [],
    count: 0,
    supported_generate_targets: [],
    notes: [],
    apiReachable: false,
  };
}

export function emptyStatus(projectId = PROJECT_ID): CICDStatus {
  return {
    project_id: resolveProjectId(projectId),
    vendors: [],
    supported_generate_targets: [],
    recent_runs: [],
    run_counts: { total_listed: 0, by_vendor: {}, by_status: {} },
    links: {},
    notes: [],
    apiReachable: false,
  };
}

export async function fetchCICDCatalog(): Promise<CICDCatalog> {
  if (!API_URL) return emptyCatalog();
  try {
    const res = await fetch(`${API_URL}/v1/cicd/catalog`, {
      cache: "no-store",
      headers: authHeaders(),
    });
    if (!res.ok) return emptyCatalog();
    const data = await res.json();
    return {
      vendors: data.vendors ?? [],
      count: data.count ?? 0,
      supported_generate_targets: data.supported_generate_targets ?? [],
      notes: data.notes ?? [],
      apiReachable: true,
    };
  } catch {
    return emptyCatalog();
  }
}

export async function fetchCICDStatus(projectId = PROJECT_ID): Promise<CICDStatus> {
  const resolved = resolveProjectId(projectId);
  if (!API_URL) return emptyStatus(resolved);
  try {
    const res = await fetch(
      `${API_URL}/v1/cicd/status?project_id=${encodeURIComponent(resolved)}`,
      { cache: "no-store", headers: authHeaders() },
    );
    if (!res.ok) return emptyStatus(resolved);
    const data = await res.json();
    return {
      project_id: data.project_id ?? resolved,
      vendors: data.vendors ?? [],
      supported_generate_targets: data.supported_generate_targets ?? [],
      recent_runs: data.recent_runs ?? [],
      run_counts: data.run_counts ?? { total_listed: 0, by_vendor: {}, by_status: {} },
      links: data.links ?? {},
      notes: data.notes ?? [],
      apiReachable: true,
    };
  } catch {
    return emptyStatus(resolved);
  }
}

export async function generateCICDConfig(input: {
  vendor: string;
  language: string;
  framework?: string;
}): Promise<{ ok: true; result: CICDGenerateResult } | { ok: false; error: string }> {
  if (!API_URL) return { ok: false, error: "API URL not configured" };
  try {
    const res = await fetch(`${API_URL}/v1/cicd/generate`, {
      method: "POST",
      cache: "no-store",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify(input),
    });
    if (!res.ok) {
      const text = await res.text();
      return { ok: false, error: text || `HTTP ${res.status}` };
    }
    return { ok: true, result: await res.json() };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : String(err) };
  }
}

export async function validateCICDYaml(input: {
  yaml: string;
  vendor?: string;
}): Promise<{ ok: true; result: CICDValidateResult } | { ok: false; error: string }> {
  if (!API_URL) return { ok: false, error: "API URL not configured" };
  try {
    const res = await fetch(`${API_URL}/v1/cicd/validate`, {
      method: "POST",
      cache: "no-store",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify(input),
    });
    if (!res.ok) {
      const text = await res.text();
      return { ok: false, error: text || `HTTP ${res.status}` };
    }
    return { ok: true, result: await res.json() };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : String(err) };
  }
}

export async function listCICDRuns(
  projectId = PROJECT_ID,
  limit = 25,
): Promise<CICDRun[]> {
  const resolved = resolveProjectId(projectId);
  if (!API_URL) return [];
  try {
    const res = await fetch(
      `${API_URL}/v1/cicd/runs?project_id=${encodeURIComponent(resolved)}&limit=${limit}`,
      { cache: "no-store", headers: authHeaders() },
    );
    if (!res.ok) return [];
    const data = await res.json();
    return data.items ?? [];
  } catch {
    return [];
  }
}
