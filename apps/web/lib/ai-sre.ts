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

export type AiSreNoiseSignature = {
  signature: string;
  kind: string;
  signal_rows: number;
  event_count?: number;
};

export type AiSreNoiseReport = {
  raw_signal_count: number;
  clustered_count: number;
  reduction?: { ratio?: number | null } | null;
  top_signatures?: AiSreNoiseSignature[];
};

/** Shown when GET /v1/ai-sre/noise fails. Not a stand-in for a count. */
export const NOISE_REPORT_NOT_LOADED = "The noise report did not load.";

export type NoiseCard = {
  raw: number;
  clustered: number;
  showRatio: boolean;
  ratio: number | null;
  signatures: { signature: string; kind: string; signal_rows: number }[];
};

export type AiSreFeed = {
  projectId: string;
  apiReachable: boolean;
  ledger: AiSreLedger | null;
  /** Null when the noise request failed. Omitted only on the page's unloaded fallback. */
  noise?: AiSreNoiseReport | null;
  /** Set when `noise` did not load. Never used to invent a zero report. */
  noiseError?: string | null;
};

const NOISE_SIGNATURE_LIMIT = 10;

/** The card model for a noise report. Null means the report did not load. Never computes a ratio. */
export function noiseCard(report: AiSreNoiseReport | null): NoiseCard | null {
  if (report == null) return null;
  const raw = report.raw_signal_count;
  const clustered = report.clustered_count;
  const apiRatio = report.reduction?.ratio;
  const ratio = typeof apiRatio === "number" && Number.isFinite(apiRatio) ? apiRatio : null;
  const showRatio = raw > 0 && ratio !== null;
  const signatures = (report.top_signatures ?? []).slice(0, NOISE_SIGNATURE_LIMIT).map((row) => ({
    signature: row.signature,
    kind: row.kind,
    signal_rows: row.signal_rows,
  }));
  return {
    raw,
    clustered,
    showRatio,
    ratio: showRatio ? ratio : null,
    signatures,
  };
}

async function getJson<T>(
  apiUrl: string,
  headers: Record<string, string>,
  path: string,
): Promise<T | null> {
  try {
    const response = await fetch(`${apiUrl}${path}`, {
      headers,
      cache: "no-store",
    });
    if (!response.ok) return null;
    return (await response.json()) as T;
  } catch {
    return null;
  }
}

export async function fetchAiSreCapabilities(
  projectId?: string,
): Promise<AiSreFeed> {
  // The card renders this module. Load the API client only when the feed is fetched.
  const { API_URL, authHeaders, resolveProjectId } = await import("./client");
  const resolved = resolveProjectId(projectId);
  const headers = authHeaders();
  const [ledger, noise] = await Promise.all([
    getJson<AiSreLedger>(API_URL, headers, "/v1/ai-sre/capabilities"),
    getJson<AiSreNoiseReport>(
      API_URL,
      headers,
      `/v1/ai-sre/noise?project_id=${encodeURIComponent(resolved)}`,
    ),
  ]);
  return {
    projectId: resolved,
    apiReachable: ledger !== null,
    ledger,
    noise,
    noiseError: noise ? null : NOISE_REPORT_NOT_LOADED,
  };
}
