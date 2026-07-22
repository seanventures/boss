import { API_URL, PROJECT_ID, authHeaders, resolveProjectId } from "./client";

export type ReliabilityIncident = {
  id: string;
  title: string;
  service: string;
  severity: "info" | "warning" | "critical";
  status: string;
  confidence: number;
  signal_count: number;
  first_seen: string;
  last_seen: string;
};

export type ReliabilityLearning = {
  evaluation: {
    total: number;
    false_alarm_rate: number | null;
    fix_success_rate: number | null;
    by_outcome: Record<string, number>;
  };
  raise_threshold: { signature: string; dismissals: number }[];
  few_shot_count: number;
};

export type ReliabilityPolicy = {
  auto_remediate: boolean;
  min_confidence: number;
  max_blast_radius: number;
  max_auto_severity: string;
};

export type ReliabilityFeed = {
  projectId: string;
  apiReachable: boolean;
  incidents: ReliabilityIncident[];
  learning: ReliabilityLearning | null;
  policy: ReliabilityPolicy | null;
};

async function getJson<T>(path: string): Promise<T | null> {
  try {
    const r = await fetch(`${API_URL}${path}`, { headers: authHeaders(), cache: "no-store" });
    if (!r.ok) return null;
    return (await r.json()) as T;
  } catch {
    return null;
  }
}

export async function fetchReliabilityFeed(projectId = PROJECT_ID): Promise<ReliabilityFeed> {
  const resolved = resolveProjectId(projectId);
  const q = `project_id=${encodeURIComponent(resolved)}`;
  const [incidents, learning, policy] = await Promise.all([
    getJson<{ items: ReliabilityIncident[] }>(`/v1/reliability/incidents?${q}&limit=30`),
    getJson<ReliabilityLearning>(`/v1/reliability/learning?${q}`),
    getJson<ReliabilityPolicy>(`/v1/reliability/policy?${q}`),
  ]);
  return {
    projectId: resolved,
    apiReachable: incidents !== null,
    incidents: incidents?.items ?? [],
    learning,
    policy,
  };
}
