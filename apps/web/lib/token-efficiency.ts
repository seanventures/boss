import { API_URL, PROJECT_ID, resolveProjectId, authHeaders } from "./client";

/** Token efficiency client — scorecard, policies, consumption, recommendations. */

export type ScorecardGrade = "good" | "fair" | "poor" | "n/a" | string;

export type StageScore = {
  stage: string;
  calls: number;
  okCalls: number;
  totalTokens: number;
  avgTokensPerCall: number;
  cacheRatio: number | null;
  costUsdMicros: number;
  failedCostUsdMicros: number;
};

export type TokenScorecard = {
  projectId: string;
  windowHours: number | null;
  calls: number;
  okCalls: number;
  failedCalls: number;
  totalTokens: number;
  promptTokens: number;
  completionTokens: number;
  cacheReadTokens: number;
  cacheRatio: number | null;
  failedBurnUsdMicros: number;
  failedBurnRatio: number;
  avgTokensPerCall: number;
  costUsdMicros: number;
  costPerOkCallUsdMicros: number | null;
  grades: Record<string, ScorecardGrade>;
  byStage: StageScore[];
  apiReachable: boolean;
};

export type TokenPolicy = {
  stage: string;
  maxPromptTokens: number | null;
  maxCompletionTokens: number | null;
  maxTotalTokensPerCall: number | null;
  maxTokensPerDay: number | null;
  maxCostUsdMicrosPerDay: number | null;
  alertAtPct: number;
  preferredModelClass: string | null;
  enabled: boolean;
  source: string;
  version: number;
};

export type TokenPoliciesFeed = {
  projectId: string;
  policies: TokenPolicy[];
  apiReachable: boolean;
};

export type TokenRecommendation = {
  kind: string;
  severity: string;
  finding: string;
  action: string;
  potentialSavingUsdMicros: number;
  potentialTokenSaving: number;
  source: string;
  /** Which stage a per-stage rule fired for.
   *
   *  Rules like `stage_over_token_cap` loop over stages and emit ONE recommendation PER
   *  STAGE — same kind, same source, different stage. The API sends this discriminator and
   *  the mapper used to drop it, which left the card with no way to tell two findings apart.
   *  Absent on project-wide rules. */
  stage?: string;
};

export type TokenRecommendationsFeed = {
  projectId: string;
  totalSpendUsdMicros: number;
  profile: string;
  recommendations: TokenRecommendation[];
  scorecardGrades: Record<string, ScorecardGrade>;
  apiReachable: boolean;
};

export type TokenConsumption = {
  projectId: string;
  windowHours: number;
  calls: number;
  totalTokens: number;
  costUsdMicros: number;
  cacheRatio: number | null;
  tokenUtilization: number | null;
  costUtilization: number | null;
  alerts: { kind: string; severity: string; finding: string; action: string }[];
  apiReachable: boolean;
};

export type TokenEstimate = {
  totalEstimatedTokens: number;
  messageCount: number;
  overBudget: boolean;
  compressSuggestions: {
    kind: string;
    severity: string;
    finding: string;
    action: string;
    potentialTokenSaving: number;
  }[];
  apiReachable: boolean;
};

const emptyScorecard = (projectId: string): TokenScorecard => ({
  projectId,
  windowHours: null,
  calls: 0,
  okCalls: 0,
  failedCalls: 0,
  totalTokens: 0,
  promptTokens: 0,
  completionTokens: 0,
  cacheReadTokens: 0,
  cacheRatio: null,
  failedBurnUsdMicros: 0,
  failedBurnRatio: 0,
  avgTokensPerCall: 0,
  costUsdMicros: 0,
  costPerOkCallUsdMicros: null,
  grades: {},
  byStage: [],
  apiReachable: false,
});

function mapStage(s: Record<string, unknown>): StageScore {
  return {
    stage: String(s.stage ?? ""),
    calls: Number(s.calls ?? 0),
    okCalls: Number(s.ok_calls ?? 0),
    totalTokens: Number(s.total_tokens ?? 0),
    avgTokensPerCall: Number(s.avg_tokens_per_call ?? 0),
    cacheRatio: s.cache_ratio == null ? null : Number(s.cache_ratio),
    costUsdMicros: Number(s.cost_usd_micros ?? 0),
    failedCostUsdMicros: Number(s.failed_cost_usd_micros ?? 0),
  };
}

export async function fetchTokenScorecard(
  projectId = PROJECT_ID,
  windowHours?: number,
  active = true,
): Promise<TokenScorecard> {
  const resolved = resolveProjectId(projectId);
  // Not rendered this request. Same shape the unreachable path returns, no round trip.
  if (!active) return emptyScorecard(resolved);
  try {
    const qs = new URLSearchParams({ project_id: resolved });
    if (windowHours != null) qs.set("window_hours", String(windowHours));
    const r = await fetch(`${API_URL}/v1/token-efficiency/scorecard?${qs}`, {
      cache: "no-store",
      headers: authHeaders(),
    });
    if (!r.ok) return emptyScorecard(resolved);
    const d = await r.json();
    return {
      projectId: String(d.project_id ?? resolved),
      windowHours: d.window_hours == null ? null : Number(d.window_hours),
      calls: Number(d.calls ?? 0),
      okCalls: Number(d.ok_calls ?? 0),
      failedCalls: Number(d.failed_calls ?? 0),
      totalTokens: Number(d.total_tokens ?? 0),
      promptTokens: Number(d.prompt_tokens ?? 0),
      completionTokens: Number(d.completion_tokens ?? 0),
      cacheReadTokens: Number(d.cache_read_tokens ?? 0),
      cacheRatio: d.cache_ratio == null ? null : Number(d.cache_ratio),
      failedBurnUsdMicros: Number(d.failed_burn_usd_micros ?? 0),
      failedBurnRatio: Number(d.failed_burn_ratio ?? 0),
      avgTokensPerCall: Number(d.avg_tokens_per_call ?? 0),
      costUsdMicros: Number(d.cost_usd_micros ?? 0),
      costPerOkCallUsdMicros:
        d.cost_per_ok_call_usd_micros == null
          ? null
          : Number(d.cost_per_ok_call_usd_micros),
      grades: (d.grades ?? {}) as Record<string, ScorecardGrade>,
      byStage: (d.by_stage ?? []).map(mapStage),
      apiReachable: true,
    };
  } catch {
    return emptyScorecard(resolved);
  }
}

export async function fetchTokenPolicies(
  projectId = PROJECT_ID,
  active = true,
): Promise<TokenPoliciesFeed> {
  const resolved = resolveProjectId(projectId);
  // Not rendered this request. Same shape the unreachable path returns, no round trip.
  if (!active) return { projectId: resolved, policies: [], apiReachable: false };
  try {
    const r = await fetch(
      `${API_URL}/v1/token-efficiency/policies?project_id=${encodeURIComponent(resolved)}`,
      { cache: "no-store", headers: authHeaders() },
    );
    if (!r.ok) return { projectId: resolved, policies: [], apiReachable: false };
    const d = await r.json();
    return {
      projectId: String(d.project_id ?? resolved),
      policies: (d.policies ?? []).map((p: Record<string, unknown>) => ({
        stage: String(p.stage ?? "*"),
        maxPromptTokens: p.max_prompt_tokens == null ? null : Number(p.max_prompt_tokens),
        maxCompletionTokens:
          p.max_completion_tokens == null ? null : Number(p.max_completion_tokens),
        maxTotalTokensPerCall:
          p.max_total_tokens_per_call == null
            ? null
            : Number(p.max_total_tokens_per_call),
        maxTokensPerDay:
          p.max_tokens_per_day == null ? null : Number(p.max_tokens_per_day),
        maxCostUsdMicrosPerDay:
          p.max_cost_usd_micros_per_day == null
            ? null
            : Number(p.max_cost_usd_micros_per_day),
        alertAtPct: Number(p.alert_at_pct ?? 0.8),
        preferredModelClass:
          p.preferred_model_class == null ? null : String(p.preferred_model_class),
        enabled: Boolean(p.enabled ?? true),
        source: String(p.source ?? "default"),
        version: Number(p.version ?? 0),
      })),
      apiReachable: true,
    };
  } catch {
    return { projectId: resolved, policies: [], apiReachable: false };
  }
}

export async function fetchTokenConsumption(
  projectId = PROJECT_ID,
  windowHours = 24,
  active = true,
): Promise<TokenConsumption> {
  const resolved = resolveProjectId(projectId);
  // Not rendered this request. Same shape the unreachable path returns, no round trip.
  if (!active) return unfetchedTokenConsumption(resolved, windowHours);
  try {
    const qs = new URLSearchParams({
      project_id: resolved,
      window_hours: String(windowHours),
    });
    const r = await fetch(`${API_URL}/v1/token-efficiency/consumption?${qs}`, {
      cache: "no-store",
      headers: authHeaders(),
    });
    if (!r.ok) {
      return unfetchedTokenConsumption(resolved, windowHours);
    }
    const d = await r.json();
    const pol = d.policy ?? {};
    return {
      projectId: String(d.project_id ?? resolved),
      windowHours: Number(d.window_hours ?? windowHours),
      calls: Number(d.calls ?? 0),
      totalTokens: Number(d.total_tokens ?? 0),
      costUsdMicros: Number(d.cost_usd_micros ?? 0),
      cacheRatio: d.cache_ratio == null ? null : Number(d.cache_ratio),
      tokenUtilization:
        pol.token_utilization == null ? null : Number(pol.token_utilization),
      costUtilization:
        pol.cost_utilization == null ? null : Number(pol.cost_utilization),
      alerts: (d.alerts ?? []).map((a: Record<string, unknown>) => ({
        kind: String(a.kind ?? ""),
        severity: String(a.severity ?? "medium"),
        finding: String(a.finding ?? ""),
        action: String(a.action ?? ""),
      })),
      apiReachable: true,
    };
  } catch {
    return unfetchedTokenConsumption(resolved, windowHours);
  }
}

function unfetchedTokenConsumption(
  projectId: string,
  windowHours: number,
): TokenConsumption {
  return {
    projectId,
    windowHours,
    calls: 0,
    totalTokens: 0,
    costUsdMicros: 0,
    cacheRatio: null,
    tokenUtilization: null,
    costUtilization: null,
    alerts: [],
    apiReachable: false,
  };
}

export async function fetchTokenRecommendations(
  projectId = PROJECT_ID,
  profileName?: string,
  active = true,
): Promise<TokenRecommendationsFeed> {
  const resolved = resolveProjectId(projectId);
  // Not rendered this request. Same shape the unreachable path returns, no round trip.
  if (!active) return unfetchedTokenRecommendations(resolved, profileName);
  try {
    const r = await fetch(`${API_URL}/v1/token-efficiency/recommendations`, {
      method: "POST",
      cache: "no-store",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({
        project_id: resolved,
        profile_name: profileName ?? null,
      }),
    });
    if (!r.ok) {
      return {
        projectId: resolved,
        totalSpendUsdMicros: 0,
        profile: profileName ?? "balanced",
        recommendations: [],
        scorecardGrades: {},
        apiReachable: false,
      };
    }
    const d = await r.json();
    return {
      projectId: String(d.project_id ?? resolved),
      totalSpendUsdMicros: Number(d.total_spend_usd_micros ?? 0),
      profile: String(d.profile ?? "balanced"),
      scorecardGrades: (d.scorecard_grades ?? {}) as Record<string, ScorecardGrade>,
      recommendations: (d.recommendations ?? []).map((rec: Record<string, unknown>) => ({
        kind: String(rec.kind ?? ""),
        severity: String(rec.severity ?? "medium"),
        finding: String(rec.finding ?? ""),
        action: String(rec.action ?? ""),
        potentialSavingUsdMicros: Number(rec.potential_saving_usd_micros ?? 0),
        potentialTokenSaving: Number(rec.potential_token_saving ?? 0),
        source: String(rec.source ?? "token_efficiency"),
        stage: rec.stage == null ? undefined : String(rec.stage),
      })),
      apiReachable: true,
    };
  } catch {
    return unfetchedTokenRecommendations(resolved, profileName);
  }
}

function unfetchedTokenRecommendations(
  projectId: string,
  profileName?: string,
): TokenRecommendationsFeed {
  return {
    projectId,
    totalSpendUsdMicros: 0,
    profile: profileName ?? "balanced",
    recommendations: [],
    scorecardGrades: {},
    apiReachable: false,
  };
}

export async function estimatePromptTokens(
  messages: { role: string; content: string }[],
  maxContextTokens?: number,
): Promise<TokenEstimate> {
  try {
    const r = await fetch(`${API_URL}/v1/token-efficiency/estimate`, {
      method: "POST",
      cache: "no-store",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({
        messages,
        max_context_tokens: maxContextTokens ?? null,
      }),
    });
    if (!r.ok) {
      return {
        totalEstimatedTokens: 0,
        messageCount: 0,
        overBudget: false,
        compressSuggestions: [],
        apiReachable: false,
      };
    }
    const d = await r.json();
    return {
      totalEstimatedTokens: Number(d.total_estimated_tokens ?? 0),
      messageCount: Number(d.message_count ?? 0),
      overBudget: Boolean(d.over_budget),
      compressSuggestions: (d.compress_suggestions ?? []).map(
        (s: Record<string, unknown>) => ({
          kind: String(s.kind ?? ""),
          severity: String(s.severity ?? "low"),
          finding: String(s.finding ?? ""),
          action: String(s.action ?? ""),
          potentialTokenSaving: Number(s.potential_token_saving ?? 0),
        }),
      ),
      apiReachable: true,
    };
  } catch {
    return {
      totalEstimatedTokens: 0,
      messageCount: 0,
      overBudget: false,
      compressSuggestions: [],
      apiReachable: false,
    };
  }
}
