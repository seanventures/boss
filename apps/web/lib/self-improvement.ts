import {
  BossApiError,
  type SelfImprovementProposalSummary,
} from "@boss/api-client";

import {
  API_URL,
  authHeaders,
  client,
  PROJECT_ID,
  resolveProjectId,
} from "./client";

export type SelfImprovementFeedItem = {
  id: string;
  taskId: string;
  title: string;
  summary: string;
  changeRef: string;
  diffSummary: string;
  status: string;
  riskTier: number;
  statusNote?: string;
  currentDecisionId?: string;
  proposalArtifactId?: string;
  canaryArtifactId?: string;
  rolloutArtifactId?: string;
  rollbackArtifactId?: string;
  rolloutState?: Record<string, unknown>;
  createdBy: string;
  createdAt: string;
  updatedAt: string;
};

/** Community-class improvement program row (trial ledger; not enterprise SIM proposals). */
export type ImprovementProgramItem = {
  id: string;
  name: string;
  objective: string;
  status: string;
  baselineEvaluationId?: string;
  updatedAt?: string;
};

export type SelfImprovementFeed = {
  projectId: string;
  /** True when `/v1/self-improvement/proposals` answered successfully. */
  apiReachable: boolean;
  /**
   * True only when proposal create/list/canary/rollout CTAs should be shown.
   * False on community (enterprise-gated routes 404) or any unreachable SIM API.
   */
  surfaceAvailable: boolean;
  editionId?: string;
  blockedReason?: string;
  /** True when the control plane edition does not mount enterprise SIM routes. */
  enterpriseOnly?: boolean;
  items: SelfImprovementFeedItem[];
  /** Community-class `/v1/improvement-programs` list (optional honest fallback). */
  improvementPrograms: ImprovementProgramItem[];
  programsApiReachable: boolean;
  programsBlockedReason?: string;
};

/** Paid editions that mount `/v1/self-improvement/*` (see edition routers). */
const PAID_EDITION_IDS = new Set([
  "cloud",
  "enterprise",
  "enterprise_cloud",
  "enterprise_self_hosted",
]);

export function isPaidSelfImprovementEdition(editionId?: string | null): boolean {
  if (!editionId) return false;
  return PAID_EDITION_IDS.has(editionId) || editionId.startsWith("enterprise");
}

/**
 * Build operator-facing copy when SIM proposals are not usable.
 * Prefer edition honesty over a bare "API returned 404".
 */
export function selfImprovementUnavailableReason(input: {
  editionId?: string;
  status?: number;
  errorMessage?: string;
}): { reason: string; enterpriseOnly: boolean } {
  const editionId = input.editionId?.trim() || undefined;
  const community =
    !isPaidSelfImprovementEdition(editionId) &&
    (editionId === "community" ||
      editionId === undefined ||
      input.status === 404);

  if (community && (input.status === 404 || editionId === "community")) {
    const label = editionId ?? "community";
    return {
      enterpriseOnly: true,
      reason:
        `Self-improvement proposals (create, canary, staged rollout) are an Enterprise ` +
        `feature. This instance runs the ${label} edition, where /v1/self-improvement/* is ` +
        `not mounted — proposal CTAs are hidden so the console does not look broken.`,
    };
  }

  if (input.status === 404) {
    return {
      enterpriseOnly: true,
      reason:
        "Self-improvement proposal API is not mounted on this control plane " +
        "(/v1/self-improvement/* returned 404). Proposal CTAs are hidden.",
    };
  }

  if (input.status != null) {
    return {
      enterpriseOnly: false,
      reason: `Self-improvement API returned ${input.status}.`,
    };
  }

  return {
    enterpriseOnly: false,
    reason:
      input.errorMessage ??
      "Self-improvement proposal API is unavailable.",
  };
}

function mapProposal(
  payload: SelfImprovementProposalSummary,
): SelfImprovementFeedItem {
  return {
    id: payload.id,
    taskId: payload.task_id,
    title: payload.title,
    summary: payload.summary,
    changeRef: payload.change_ref,
    diffSummary: payload.diff_summary,
    status: payload.status,
    riskTier: payload.risk_tier,
    statusNote: payload.status_note,
    currentDecisionId: payload.current_decision_id,
    proposalArtifactId: payload.proposal_artifact_id,
    canaryArtifactId: payload.canary_artifact_id,
    rolloutArtifactId: payload.rollout_artifact_id,
    rollbackArtifactId: payload.rollback_artifact_id,
    rolloutState:
      (payload as SelfImprovementProposalSummary & { rollout_state?: Record<string, unknown> })
        .rollout_state ?? undefined,
    createdBy: payload.created_by,
    createdAt: payload.created_at,
    updatedAt: payload.updated_at,
  };
}

function mapProgram(row: Record<string, unknown>): ImprovementProgramItem {
  return {
    id: String(row.id ?? ""),
    name: String(row.name ?? ""),
    objective: String(row.objective ?? ""),
    status: String(row.status ?? "unknown"),
    baselineEvaluationId:
      row.baseline_evaluation_id != null
        ? String(row.baseline_evaluation_id)
        : undefined,
    updatedAt: row.updated_at != null ? String(row.updated_at) : undefined,
  };
}

export function emptySelfImprovementFeed(
  reason?: string,
  projectId = PROJECT_ID,
  extras?: Partial<
    Pick<
      SelfImprovementFeed,
      | "editionId"
      | "enterpriseOnly"
      | "surfaceAvailable"
      | "improvementPrograms"
      | "programsApiReachable"
      | "programsBlockedReason"
    >
  >,
): SelfImprovementFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    surfaceAvailable: extras?.surfaceAvailable ?? false,
    editionId: extras?.editionId,
    blockedReason: reason,
    enterpriseOnly: extras?.enterpriseOnly,
    items: [],
    improvementPrograms: extras?.improvementPrograms ?? [],
    programsApiReachable: extras?.programsApiReachable ?? false,
    programsBlockedReason: extras?.programsBlockedReason,
  };
}

async function resolveEditionId(projectId: string): Promise<string | undefined> {
  try {
    const edition = await client.getEdition(projectId);
    return String(edition.edition_id ?? "").trim() || undefined;
  } catch {
    // Edition is best-effort; SIM 404 still drives honesty.
    return undefined;
  }
}

async function fetchImprovementPrograms(
  projectId: string,
): Promise<{
  items: ImprovementProgramItem[];
  apiReachable: boolean;
  blockedReason?: string;
}> {
  if (!API_URL) {
    return {
      items: [],
      apiReachable: false,
      blockedReason: "Control-plane API URL is not configured.",
    };
  }

  try {
    const url = new URL(`${API_URL}/v1/improvement-programs`);
    url.searchParams.set("project_id", projectId);
    url.searchParams.set("limit", "50");
    const response = await fetch(url.toString(), {
      headers: authHeaders(),
      cache: "no-store",
    });
    if (!response.ok) {
      return {
        items: [],
        apiReachable: false,
        blockedReason: `Improvement programs API returned ${response.status}.`,
      };
    }
    const payload = (await response.json()) as {
      items?: Record<string, unknown>[];
    };
    return {
      items: (payload.items ?? []).map(mapProgram).filter((p) => p.id),
      apiReachable: true,
    };
  } catch (error) {
    return {
      items: [],
      apiReachable: false,
      blockedReason:
        error instanceof Error
          ? error.message
          : "Improvement programs API is unavailable.",
    };
  }
}

export async function fetchSelfImprovementFeed(
  projectId = PROJECT_ID,
): Promise<SelfImprovementFeed> {
  const resolved = resolveProjectId(projectId);
  const editionId = await resolveEditionId(resolved);
  const programs = await fetchImprovementPrograms(resolved);

  try {
    const payload = await client.listSelfImprovementProposals(resolved);
    return {
      projectId: payload.project_id,
      apiReachable: true,
      surfaceAvailable: true,
      editionId,
      items: payload.items.map(mapProposal),
      improvementPrograms: programs.items,
      programsApiReachable: programs.apiReachable,
      programsBlockedReason: programs.blockedReason,
    };
  } catch (error) {
    const status = error instanceof BossApiError ? error.status : undefined;
    const { reason, enterpriseOnly } = selfImprovementUnavailableReason({
      editionId,
      status,
      errorMessage:
        error instanceof Error ? error.message : "Unknown API error",
    });

    // Community edition: treat SIM as enterprise-only even before a probe fails,
    // so we never advertise create/canary/rollout CTAs that 404.
    const forcedEnterpriseOnly =
      enterpriseOnly ||
      (!isPaidSelfImprovementEdition(editionId) && editionId === "community");

    return emptySelfImprovementFeed(reason, resolved, {
      editionId,
      enterpriseOnly: forcedEnterpriseOnly,
      surfaceAvailable: false,
      improvementPrograms: programs.items,
      programsApiReachable: programs.apiReachable,
      programsBlockedReason: programs.blockedReason,
    });
  }
}
