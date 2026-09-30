import { API_URL, PROJECT_ID, authHeaders, resolveProjectId } from "./client";

export type AiSreCapability = {
  id: string;
  awesome: string;
  status: "shipped" | "partial" | "missing" | string;
  surfaces: string[];
  gap: string;
};

export type AiSreLedger = {
  product: string;
  version?: string;
  source?: string;
  public_ga: boolean;
  counts: {
    shipped: number;
    partial: number;
    missing: number;
  };
  capabilities: AiSreCapability[];
  loop?: string[];
  rules?: string[];
};

export type AiSreFeed = {
  projectId: string;
  apiReachable: boolean;
  ledger: AiSreLedger | null;
};

async function getJson<T>(path: string): Promise<T | null> {
  try {
    const response = await fetch(`${API_URL}${path}`, {
      headers: authHeaders(),
      cache: "no-store",
    });
    if (!response.ok) return null;
    return (await response.json()) as T;
  } catch {
    return null;
  }
}

export async function fetchAiSreCapabilities(
  projectId = PROJECT_ID,
): Promise<AiSreFeed> {
  const resolved = resolveProjectId(projectId);
  const ledger = await getJson<AiSreLedger>("/v1/ai-sre/capabilities");
  return {
    projectId: resolved,
    apiReachable: ledger !== null,
    ledger,
  };
}
