import { API_URL, apiUrlProblem, authHeaders, resolveProjectId } from "./client";

export type FactoryFlowDiagnosis = {
  klass: string;
  severity: string;
  state: string;
  title: string;
  detail: string;
  recommendedFix?: string;
};

export type FactoryFlowStep = {
  id: string;
  lane: string;
  name: string;
  status?: string | null;
  role?: string | null;
  ms?: number | null;
};

export type FactoryFlowItem = {
  id: string;
  title?: string | null;
  status?: string | null;
  phase?: string | null;
  official?: boolean;
  processRunId?: string | null;
  processStatus?: string | null;
  diagnosis: FactoryFlowDiagnosis;
  steps: FactoryFlowStep[];
  verifyTail?: string;
  error?: string | null;
};

export type FactoryFlowFinding = {
  id: string;
  runId?: string | null;
  klass: string;
  severity: string;
  title: string;
  detail: string;
  recommendedFix: string;
  status: string;
};

export type FactoryFlowBoard = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: FactoryFlowItem[];
  findings: FactoryFlowFinding[];
  stalled: number;
  inFlight: number;
};

export function emptyFactoryFlowBoard(reason: string, projectId = ""): FactoryFlowBoard {
  return {
    projectId,
    apiReachable: false,
    blockedReason: reason,
    items: [],
    findings: [],
    stalled: 0,
    inFlight: 0,
  };
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

export function mapFactoryFlowBoard(payload: unknown, projectId: string): FactoryFlowBoard {
  const body = asRecord(payload);
  const items = Array.isArray(body.items) ? body.items : [];
  const findings = Array.isArray(body.findings) ? body.findings : [];
  return {
    projectId: String(body.project_id || projectId),
    apiReachable: true,
    items: items.map((raw) => {
      const item = asRecord(raw);
      const diagnosis = asRecord(item.diagnosis);
      return {
        id: String(item.id || ""),
        title: item.title == null ? null : String(item.title),
        status: item.status == null ? null : String(item.status),
        phase: item.phase == null ? null : String(item.phase),
        official: item.official === true,
        processRunId: item.process_run_id == null ? null : String(item.process_run_id),
        processStatus: item.process_status == null ? null : String(item.process_status),
        diagnosis: {
          klass: String(diagnosis.klass || "idle"),
          severity: String(diagnosis.severity || "info"),
          state: String(diagnosis.state || "healthy"),
          title: String(diagnosis.title || ""),
          detail: String(diagnosis.detail || ""),
          recommendedFix: diagnosis.recommended_fix ? String(diagnosis.recommended_fix) : "",
        },
        steps: Array.isArray(item.steps)
          ? item.steps.map((stepRaw) => {
              const step = asRecord(stepRaw);
              return {
                id: String(step.id || ""),
                lane: String(step.lane || ""),
                name: String(step.name || ""),
                status: step.status == null ? null : String(step.status),
                role: step.role == null ? null : String(step.role),
                ms: typeof step.ms === "number" ? step.ms : null,
              };
            })
          : [],
        verifyTail: item.verify_tail == null ? "" : String(item.verify_tail),
        error: item.error == null ? null : String(item.error),
      };
    }),
    findings: findings.map((raw) => {
      const finding = asRecord(raw);
      return {
        id: String(finding.id || ""),
        runId: finding.run_id == null ? null : String(finding.run_id),
        klass: String(finding.klass || ""),
        severity: String(finding.severity || ""),
        title: String(finding.title || ""),
        detail: String(finding.detail || ""),
        recommendedFix: String(finding.recommended_fix || ""),
        status: String(finding.status || "open"),
      };
    }),
    stalled: Number(body.stalled || 0),
    inFlight: Number(body.in_flight || 0),
  };
}

export async function fetchFactoryFlowBoard(projectId: string): Promise<FactoryFlowBoard> {
  const project = resolveProjectId(projectId);
  const misconfigured = apiUrlProblem();
  if (misconfigured) {
    return emptyFactoryFlowBoard(misconfigured, project);
  }
  try {
    const response = await fetch(
      `${API_URL}/v1/factory/flow?project_id=${encodeURIComponent(project)}`,
      { headers: authHeaders({ "Content-Type": "application/json" }), cache: "no-store" },
    );
    if (!response.ok) {
      return emptyFactoryFlowBoard(`API returned ${response.status}`, project);
    }
    return mapFactoryFlowBoard(await response.json(), project);
  } catch (error) {
    return emptyFactoryFlowBoard(
      error instanceof Error ? error.message : "Unknown API error",
      project,
    );
  }
}
