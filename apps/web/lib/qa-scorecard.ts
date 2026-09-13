import { BossApiError } from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type QaHonesty = {
  honest: boolean;
  scored: string[];
  missing_evidence: Array<{ id?: string; reason?: string }>;
  invented_scores: unknown[];
};

export type QaScorecardApp = {
  id: string;
  name: string;
  builtRunId?: string;
  reportable?: boolean;
  runStatus?: string;
  passed?: boolean | null;
  honest?: boolean | null;
  scored: string[];
  invented: unknown[];
  missing: Array<{ id?: string; reason?: string }>;
  ui?: string | null;
  ux?: string | null;
  reportMissing: boolean;
  error?: string;
};

export type QaScorecardFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  apps: QaScorecardApp[];
};

function asString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function dimStatus(report: Record<string, unknown>, key: string): string | null {
  const raw = report[key];
  if (raw && typeof raw === "object") {
    const status = (raw as { status?: unknown }).status;
    return asString(status) ?? null;
  }
  return asString(raw) ?? null;
}

export function emptyQaScorecardFeed(
  reason?: string,
  projectId = PROJECT_ID,
): QaScorecardFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    apps: [],
  };
}

export async function fetchQaScorecardFeed(
  projectId = PROJECT_ID,
  active = true,
): Promise<QaScorecardFeed> {
  const pid = resolveProjectId(projectId);
  // Not rendered this request. Same shape the unreachable path returns, no round trip.
  if (!active) return emptyQaScorecardFeed("not requested", pid);
  try {
    const catalog = await client.getQaBenchmarkApps(pid);
    const rows = catalog.benchmark_apps ?? [];
    const apps: QaScorecardApp[] = [];
    for (const raw of rows) {
      const id = raw.id || "unknown";
      const name = raw.name || id;
      const built = raw.built;
      const runId = built?.run_id;
      const reportable = built?.reportable === true;
      const entry: QaScorecardApp = {
        id,
        name,
        builtRunId: runId,
        reportable,
        runStatus: asString(built?.run_status),
        scored: [],
        invented: [],
        missing: [],
        reportMissing: !runId || !reportable,
      };
      if (runId && reportable) {
        try {
          const report = await client.getQaBenchmarkReport(runId);
          const honesty = report.honesty || {};
          entry.passed = typeof report.passed === "boolean" ? report.passed : null;
          entry.honest = typeof honesty.honest === "boolean" ? honesty.honest : null;
          entry.scored = Array.isArray(honesty.scored) ? honesty.scored.map(String) : [];
          entry.invented = Array.isArray(honesty.invented_scores)
            ? honesty.invented_scores
            : [];
          entry.missing = Array.isArray(honesty.missing_evidence)
            ? honesty.missing_evidence
            : [];
          entry.ui = dimStatus(report as Record<string, unknown>, "ui");
          entry.ux = dimStatus(report as Record<string, unknown>, "ux");
          entry.reportMissing = false;
        } catch (error) {
          entry.reportMissing = true;
          entry.error =
            error instanceof BossApiError
              ? `report HTTP ${error.status}`
              : error instanceof Error
                ? error.message
                : "report unavailable";
        }
      }
      apps.push(entry);
    }
    return { projectId: pid, apiReachable: true, apps };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyQaScorecardFeed(`API returned ${error.status}`, projectId);
    }
    return emptyQaScorecardFeed(
      error instanceof Error ? error.message : "Unknown API error",
      projectId,
    );
  }
}
