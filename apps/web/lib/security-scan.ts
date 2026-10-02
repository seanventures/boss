import { API_URL, PROJECT_ID, resolveProjectId, authHeaders } from "./client";
import { apiWrite } from "./api-write";

export type SecurityScanner = {
  id: string;
  name: string;
  kind: string;
  binary: string | null;
  website: string | null;
  repo: string | null;
  stars_note: string;
  description: string;
  status: string;
  available: boolean;
};

export type SecurityFinding = {
  severity: string;
  rule_id: string;
  path: string;
  line?: number | null;
  message: string;
  scanner: string;
  cve?: string | null;
  fix?: string | null;
};

export type SecurityScanSummary = {
  total: number;
  by_severity: Record<string, number>;
  by_scanner: Record<string, number>;
  critical: number;
  high: number;
  has_critical: boolean;
};

export type SecurityScanGate = {
  decision: "go" | "no-go" | string;
  go: boolean;
  fail_on: string[];
  blocking_counts: Record<string, number>;
  reason: string;
};

export type SecurityScanRun = {
  id: string;
  project_id: string;
  actor: string;
  path: string;
  scanners: string[];
  simulated: boolean;
  status: string;
  summary: SecurityScanSummary;
  gate: SecurityScanGate;
  findings: SecurityFinding[];
  duration_ms: number;
  receipt_id?: string | null;
  dedupe_key?: string;
  created_at: string;
} | null;

export async function fetchSecurityScanners(): Promise<SecurityScanner[]> {
  try {
    const r = await fetch(`${API_URL}/v1/security-scan/scanners`, {
      cache: "no-store",
      headers: authHeaders(),
    });
    if (!r.ok) return [];
    const body = await r.json();
    return (body.items ?? []) as SecurityScanner[];
  } catch {
    return [];
  }
}

export async function fetchLatestSecurityScan(
  projectId = PROJECT_ID,
): Promise<SecurityScanRun> {
  try {
    const r = await fetch(
      `${API_URL}/v1/security-scan/runs?project_id=${encodeURIComponent(resolveProjectId(projectId))}&limit=1`,
      { cache: "no-store", headers: authHeaders() },
    );
    if (!r.ok) return null;
    const items = (await r.json()).items ?? [];
    if (!items.length) return null;
    return items[0] as SecurityScanRun;
  } catch {
    return null;
  }
}

export async function runSecurityScan(opts: {
  projectId?: string;
  actor?: string;
  scanners?: string[];
  simulated?: boolean;
  path?: string;
}): Promise<SecurityScanRun> {
  try {
    const r = await apiWrite(`${API_URL}/v1/security-scan/run`, {
      method: "POST",
      cache: "no-store",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        project_id: resolveProjectId(opts.projectId),
        actor: opts.actor ?? "console",
        scanners: opts.scanners,
        simulated: opts.simulated ?? true,
        path: opts.path,
      }),
    });
    if (!r.ok) return null;
    return (await r.json()) as SecurityScanRun;
  } catch {
    return null;
  }
}

export async function evaluateSecurityGate(
  runId: string,
  failOn?: string[],
): Promise<SecurityScanGate | null> {
  try {
    const r = await apiWrite(`${API_URL}/v1/security-scan/runs/${encodeURIComponent(runId)}/gate`, {
      method: "POST",
      cache: "no-store",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ fail_on: failOn }),
    });
    if (!r.ok) return null;
    const body = await r.json();
    return (body.gate ?? null) as SecurityScanGate | null;
  } catch {
    return null;
  }
}
