"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import { BossApiError, type DataBoundary, type LlmCallRow } from "@boss/api-client";
import { API_URL, client, PROJECT_ID, resolveProjectId, authHeaders} from "../lib/client";
import { buildCommandRequest } from "../lib/commands";

export async function setPipelineDefaultAction(formData: FormData): Promise<void> {
  const projectId = getProjectId(formData);
  const name = String(formData.get("name") ?? "").trim();
  if (!name) return;
  await fetch(`${API_URL}/v1/pipeline-profiles/default`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ project_id: projectId, name, actor: getActor(formData) }),
  });
  revalidatePath("/");
}

export async function runCanariesAction(formData: FormData): Promise<void> {
  const projectId = getProjectId(formData);
  await fetch(`${API_URL}/v1/canaries/run`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ project_id: projectId, actor: getActor(formData) }),
  });
  revalidatePath("/");
}

export async function scanAnomaliesAction(formData: FormData): Promise<void> {
  const projectId = getProjectId(formData);
  await fetch(`${API_URL}/v1/anomalies/scan`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ project_id: projectId, auto_lite: true }),
  });
  revalidatePath("/");
}

// ---- Phase 2: self-improvement runtime, evals, autonomy, router, skills ----

async function postJson(path: string, body: Record<string, unknown>): Promise<void> {
  await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(body),
  });
  revalidatePath("/");
}

export async function runSelfImprovementCanaryAction(formData: FormData): Promise<void> {
  const id = String(formData.get("proposal_id") ?? "").trim();
  if (!id) return;
  await postJson(`/v1/self-improvement/proposals/${encodeURIComponent(id)}/run-canary`, {
    actor: getActor(formData),
  });
}

export async function startSelfImprovementRolloutAction(formData: FormData): Promise<void> {
  const id = String(formData.get("proposal_id") ?? "").trim();
  if (!id) return;
  await postJson(`/v1/self-improvement/proposals/${encodeURIComponent(id)}/rollout/start`, {
    actor: getActor(formData),
  });
}

export async function advanceSelfImprovementRolloutAction(formData: FormData): Promise<void> {
  const id = String(formData.get("proposal_id") ?? "").trim();
  if (!id) return;
  const health = String(formData.get("health") ?? "healthy").trim();
  const observation: Record<string, unknown> = { health };
  const errorRate = String(formData.get("error_rate") ?? "").trim();
  if (errorRate) observation.error_rate = Number(errorRate);
  await postJson(`/v1/self-improvement/proposals/${encodeURIComponent(id)}/rollout/advance`, {
    actor: getActor(formData),
    observation,
  });
}

export async function runEvalsAction(formData: FormData): Promise<void> {
  await postJson(`/v1/evals/run`, { project_id: getProjectId(formData), actor: getActor(formData) });
}

export async function evaluateAutonomyAction(formData: FormData): Promise<void> {
  const roleKey = String(formData.get("role_key") ?? "").trim();
  if (!roleKey) return;
  await postJson(`/v1/roles/${encodeURIComponent(roleKey)}/autonomy/evaluate`, {
    project_id: getProjectId(formData),
    actor: getActor(formData),
  });
}

export async function pollProviderHealthAction(formData: FormData): Promise<void> {
  await postJson(`/v1/provider-health/poll`, { project_id: getProjectId(formData), actor: getActor(formData) });
}

export async function recordRouterRationaleAction(formData: FormData): Promise<void> {
  await postJson(`/v1/router/rationale`, { project_id: getProjectId(formData), actor: getActor(formData) });
}

export async function invokeSkillAction(formData: FormData): Promise<void> {
  const skillId = String(formData.get("skill_id") ?? "").trim();
  const caller = String(formData.get("caller") ?? "").trim();
  if (!skillId || !caller) return;
  await postJson(`/v1/skills/${encodeURIComponent(skillId)}/invoke`, {
    project_id: getProjectId(formData),
    caller,
    inputs: {},
    mock: true,
  });
}

// ---- Phase 3: result-returning actions (useActionState) for compute/preview endpoints ----

export type ActionResult = { ok: boolean; data?: Record<string, unknown>; error?: string };

async function postJsonReturn(path: string, body: Record<string, unknown>): Promise<ActionResult> {
  try {
    const r = await fetch(`${API_URL}${path}`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      let detail = "";
      try {
        detail = String((await r.json()).detail ?? "");
      } catch {}
      return { ok: false, error: detail || `HTTP ${r.status}` };
    }
    return { ok: true, data: await r.json() };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : "request failed" };
  }
}

/**
 * Run one action the operator accepted by clicking its button.
 *
 * Goes straight to the tool with the arguments they were shown — no model in between. Re-asking
 * a model to interpret a click would let it drift into doing something adjacent, which is the
 * opposite of what a confirmation button is for.
 */
export async function runAgentActionAction(
  projectId: string,
  actor: string,
  tool: string,
  args: Record<string, unknown>,
  threadId?: string,
): Promise<ActionResult> {
  const result = await postJsonReturn("/v1/engine/agent/action", {
    project_id: projectId,
    actor,
    tool,
    args: args ?? {},
    thread_id: threadId || undefined,
  });
  revalidatePath("/");
  return result;
}

export async function agentChatAction(
  _prev: ActionResult | null,
  formData: FormData,
): Promise<ActionResult> {
  const message = String(formData.get("message") ?? "").trim();
  const confirmPlanId = String(formData.get("confirm_plan_id") ?? "").trim();
  if (!message && !confirmPlanId) {
    return { ok: false, error: "Enter a message or confirm the pending plan." };
  }
  const result = await postJsonReturn("/v1/engine/agent/chat", {
    project_id: getProjectId(formData),
    actor: getActor(formData),
    thread_id: String(formData.get("thread_id") ?? "").trim() || undefined,
    message,
    confirm_plan_id: confirmPlanId || undefined,
    surface: "chat",
    // Scope the turn to a task: external_ref = task_id -> the backend find-or-creates ONE thread
    // per task (agent_service.start_or_load_thread), so a task's chat stays with that task.
    external_ref: String(formData.get("external_ref") ?? "").trim() || undefined,
  });
  revalidatePath("/");
  return result;
}

// ---- Agent workspace: task/thread list, per-task history, task context (task-scoped chat) ----
async function getJsonReturn(path: string): Promise<ActionResult> {
  try {
    const r = await fetch(`${API_URL}${path}`, {
      headers: authHeaders({ "Content-Type": "application/json" }),
      cache: "no-store",
    });
    if (!r.ok) {
      let detail = "";
      try { detail = String((await r.json()).detail ?? ""); } catch {}
      return { ok: false, error: detail || `HTTP ${r.status}` };
    }
    return { ok: true, data: await r.json() };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : "request failed" };
  }
}

export async function loadAgentWorkspaceAction(projectId: string, actor: string): Promise<ActionResult> {
  const p = encodeURIComponent(projectId), a = encodeURIComponent(actor);
  const [tasks, threads] = await Promise.all([
    getJsonReturn(`/v1/tasks?project_id=${p}&limit=60`),
    getJsonReturn(`/v1/agent/threads?project_id=${p}&actor=${a}&since_days=10`),
  ]);
  const td = tasks.data as Record<string, unknown> | undefined;
  const thd = threads.data as Record<string, unknown> | undefined;
  return { ok: true, data: {
    tasks: (td?.tasks ?? td?.items ?? []) as unknown[],
    threads: (thd?.threads ?? []) as unknown[],
  } };
}

export async function loadThreadMessagesAction(threadId: string, projectId: string, actor: string): Promise<ActionResult> {
  return getJsonReturn(
    `/v1/agent/threads/${encodeURIComponent(threadId)}/messages` +
    `?project_id=${encodeURIComponent(projectId)}&actor=${encodeURIComponent(actor)}`);
}

export async function loadTaskContextAction(taskId: string, projectId: string): Promise<ActionResult> {
  const p = encodeURIComponent(projectId);
  const [task, codeTasks] = await Promise.all([
    getJsonReturn(`/v1/tasks/${encodeURIComponent(taskId)}?project_id=${p}`),
    getJsonReturn(`/v1/code-tasks?project_id=${p}&limit=200`),
  ]);
  const ct = ((codeTasks.data as Record<string, unknown> | undefined)?.code_tasks ?? []) as Array<Record<string, unknown>>;
  return { ok: true, data: {
    task: task.data ?? null,
    code_tasks: ct.filter((c) => c.task_id === taskId),
  } };
}

export async function tallyVotesAction(_prev: ActionResult | null, formData: FormData): Promise<ActionResult> {
  const meetingId = String(formData.get("meeting_id") ?? "").trim();
  if (!meetingId) {
    return { ok: false, error: "requires meeting_id — open a meeting or paste a run/meeting id." };
  }
  const approve = Math.max(0, Number(formData.get("approve") ?? 0));
  const reject = Math.max(0, Number(formData.get("reject") ?? 0));
  const abstain = Math.max(0, Number(formData.get("abstain") ?? 0));
  const eligibleRaw = String(formData.get("eligible") ?? "").trim();
  const quorumRaw = String(formData.get("quorum") ?? "").trim();
  const votes = [
    ...Array.from({ length: approve }, (_v, i) => ({ voter: `a${i}`, vote: "approve" })),
    ...Array.from({ length: reject }, (_v, i) => ({ voter: `r${i}`, vote: "reject" })),
    ...Array.from({ length: abstain }, (_v, i) => ({ voter: `x${i}`, vote: "abstain" })),
  ];
  if (!votes.length) return { ok: false, error: "Enter at least one vote." };
  return postJsonReturn(`/v1/meetings/${encodeURIComponent(meetingId)}/tally`, {
    actor: getActor(formData),
    votes,
    eligible_count: eligibleRaw ? Number(eligibleRaw) : undefined,
    quorum: quorumRaw ? Number(quorumRaw) : undefined,
    vote_threshold: 0.5,
  });
}

export async function sweepStalledMeetingsAction(_prev: ActionResult | null, formData: FormData): Promise<ActionResult> {
  const res = await postJsonReturn(`/v1/meetings/sweep-stalled`, { actor: getActor(formData) });
  revalidatePath("/");
  return res;
}

export async function mergeCheckAction(_prev: ActionResult | null, formData: FormData): Promise<ActionResult> {
  const checks = {
    tests_passed: formData.get("tests_passed") === "on",
    lint_passed: formData.get("lint_passed") === "on",
    ci_green: formData.get("ci_green") === "on",
    reviewer_independent: formData.get("reviewer_independent") === "on",
    adversarial_verdict: formData.get("adversarial") === "on" ? "survived" : "not_run",
  };
  const minRaw = String(formData.get("min_confidence") ?? "").trim();
  return postJsonReturn(`/v1/factory/merge-check`, {
    checks,
    min_confidence: minRaw ? Number(minRaw) : 0.8,
  });
}

export async function chunkPlanAction(_prev: ActionResult | null, formData: FormData): Promise<ActionResult> {
  const title = String(formData.get("title") ?? "change").trim() || "change";
  const files = parseLines(formData.get("files"));
  return postJsonReturn(`/v1/factory/chunk-plan`, {
    spec: { title, files },
    max_files_per_chunk: 3,
  });
}

export async function slackRenderAction(_prev: ActionResult | null, formData: FormData): Promise<ActionResult> {
  const persona = String(formData.get("persona") ?? "").trim();
  const body = String(formData.get("body") ?? "").trim();
  if (!persona || !body) return { ok: false, error: "Persona and body are required." };
  return postJsonReturn(`/v1/comms/slack/render`, {
    persona,
    role: String(formData.get("role") ?? "").trim() || undefined,
    subject: String(formData.get("subject") ?? "").trim() || undefined,
    body,
    department: String(formData.get("department") ?? "").trim() || undefined,
  });
}

export async function slackDigestAction(_prev: ActionResult | null, formData: FormData): Promise<ActionResult> {
  const res = await postJsonReturn(`/v1/comms/slack/digest`, {
    project_id: getProjectId(formData),
    actor: "system",
  });
  revalidatePath("/");
  return res;
}

// ---- Phase 4: chaos, finops reconciler, SOC2 ----

export async function runChaosAction(formData: FormData): Promise<void> {
  const scenario = String(formData.get("scenario") ?? "").trim();
  if (!scenario) return;
  await postJson(`/v1/chaos/run`, {
    project_id: getProjectId(formData),
    scenario,
    actor: "system",
  });
}

export async function reconcileCommitmentsAction(formData: FormData): Promise<void> {
  await postJson(`/v1/finops/reconcile-commitments`, {
    project_id: getProjectId(formData),
    actor: "system",
  });
}

export async function requestInitiativeBudgetAction(
  _prev: ActionResult | null,
  formData: FormData,
): Promise<ActionResult> {
  const title = String(formData.get("title") ?? "").trim();
  if (!title) return { ok: false, error: "Title is required." };
  const res = await postJsonReturn(`/v1/finops/initiative-budget`, {
    project_id: getProjectId(formData),
    title,
    tier: Number(formData.get("tier") ?? 2),
    amount_usd_micros: Number(formData.get("amount_usd") ?? 0) * 1_000_000,
    actor: "system",
  });
  revalidatePath("/");
  return res;
}

export async function collectSoc2EvidenceAction(
  _prev: ActionResult | null,
  formData: FormData,
): Promise<ActionResult> {
  const res = await postJsonReturn(`/v1/enterprise/soc2-evidence`, {
    project_id: getProjectId(formData),
    actor: "system",
  });
  revalidatePath("/");
  return res;
}

export async function dispatchPlanAction(formData: FormData): Promise<void> {
  const projectId = getProjectId(formData);
  const planId = String(formData.get("plan_id") ?? "").trim();
  if (!planId) return;
  await fetch(`${API_URL}/v1/plans/${encodeURIComponent(planId)}/dispatch`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ project_id: projectId, actor: getActor(formData) }),
  });
  revalidatePath("/");
}

export async function setBudgetIntensityAction(formData: FormData): Promise<void> {
  const projectId = getProjectId(formData);
  const intensity = Number(formData.get("intensity") ?? 50);
  await fetch(`${API_URL}/v1/budget/set_intensity`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ scope_id: projectId, intensity, actor: getActor(formData) }),
  });
  revalidatePath("/");
}

export async function createPipelineProfileAction(formData: FormData): Promise<void> {
  const projectId = getProjectId(formData);
  const name = String(formData.get("name") ?? "").trim().toLowerCase();
  if (!name) return;
  const tierFloor = Number(formData.get("tier_floor") ?? 0);
  const tierCap = Number(formData.get("tier_cap") ?? 3);
  const defaultModel = String(formData.get("default_model") ?? "low_cost");
  const strongHard = formData.get("strong_hard") === "on";
  const model: Record<string, string> = { default: defaultModel };
  if (strongHard) {
    model.implement = "strong";
    model.review_code = "strong";
    model.security_check = "strong";
  }
  await fetch(`${API_URL}/v1/pipeline-profiles`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({
      project_id: projectId,
      name,
      label: String(formData.get("label") ?? "").trim() || name,
      tier_floor: tierFloor,
      tier_cap: tierCap,
      model,
      actor: getActor(formData),
    }),
  });
  revalidatePath("/");
}

function getProjectId(formData: FormData): string {
  const value = formData.get("project_id");
  return typeof value === "string" && value.trim()
    ? value.trim()
    : resolveProjectId(PROJECT_ID);
}

function getActor(formData: FormData): string {
  const value = formData.get("actor");
  return typeof value === "string" && value.trim()
    ? value.trim()
    : "executive_console";
}

function parseLines(value: FormDataEntryValue | null): string[] {
  if (typeof value !== "string") {
    return [];
  }
  return value
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
}

function parseDataBoundaries(value: FormDataEntryValue | null): DataBoundary[] {
  const allowed = new Set<DataBoundary>([
    "public",
    "internal",
    "confidential",
    "restricted",
    "local-only",
  ]);
  return parseLines(value).filter((item): item is DataBoundary =>
    allowed.has(item as DataBoundary),
  );
}

function parseInteger(value: FormDataEntryValue | null, fallback: number): number {
  if (typeof value !== "string" || !value.trim()) {
    return fallback;
  }
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function parseNumber(value: FormDataEntryValue | null, fallback: number): number {
  if (typeof value !== "string" || !value.trim()) {
    return fallback;
  }
  const parsed = Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function parseRequiredMetrics(
  value: FormDataEntryValue | null,
): Record<string, unknown> {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error("metrics_json is required");
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new Error("metrics_json must be valid JSON");
  }
  if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
    throw new Error("metrics_json must be a JSON object");
  }
  if (Object.keys(parsed as Record<string, unknown>).length === 0) {
    throw new Error("metrics_json must contain at least one entry");
  }
  return parsed as Record<string, unknown>;
}

function parseOptionalJsonObject(
  value: FormDataEntryValue | null,
): Record<string, unknown> {
  if (typeof value !== "string" || !value.trim()) {
    return {};
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new Error("scope_json must be valid JSON");
  }
  if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
    throw new Error("scope_json must be a JSON object");
  }
  return parsed as Record<string, unknown>;
}

function parseOptionalJsonObjectField(
  value: FormDataEntryValue | null,
  fieldName: string,
): Record<string, unknown> {
  if (typeof value !== "string" || !value.trim()) {
    return {};
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new Error(`${fieldName} must be valid JSON`);
  }
  if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
    throw new Error(`${fieldName} must be a JSON object`);
  }
  return parsed as Record<string, unknown>;
}

function parseOptionalJsonArray(
  value: FormDataEntryValue | null,
  fieldName: string,
): Array<Record<string, unknown>> {
  if (typeof value !== "string" || !value.trim()) {
    return [];
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new Error(`${fieldName} must be valid JSON`);
  }
  if (!Array.isArray(parsed)) {
    throw new Error(`${fieldName} must be a JSON array`);
  }
  if (parsed.some((item) => !item || Array.isArray(item) || typeof item !== "object")) {
    throw new Error(`${fieldName} must contain only JSON objects`);
  }
  return parsed as Array<Record<string, unknown>>;
}

function parseRequiredJsonArrayField(
  value: FormDataEntryValue | null,
  fieldName: string,
): Array<Record<string, unknown>> {
  const parsed = parseOptionalJsonArray(value, fieldName);
  if (parsed.length === 0) {
    throw new Error(`${fieldName} must contain at least one JSON object`);
  }
  return parsed;
}

function parseProviderOpsCheckbox(
  formData: FormData,
  name: string,
  defaultValue: boolean,
): boolean {
  const values = formData.getAll(name).filter((value) => typeof value === "string");
  if (values.length === 0) {
    return defaultValue;
  }
  return values.some((value) => value === "true" || value === "on");
}

function getOptionalString(formData: FormData, name: string): string | undefined {
  const value = formData.get(name);
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function parseProviderCredentialRefs(formData: FormData): Record<string, string> {
  const refs: Record<string, string> = {};
  for (const [name, value] of formData.entries()) {
    if (
      !name.startsWith("credential_ref__") ||
      typeof value !== "string" ||
      !value.trim()
    ) {
      continue;
    }
    const slot = name.slice("credential_ref__".length).trim();
    if (!slot) {
      continue;
    }
    refs[slot] = value.trim();
  }
  return refs;
}

export async function resolveDecisionAction(formData: FormData): Promise<void> {
  const decisionId = formData.get("decision_id");
  const action = formData.get("action");
  const note = formData.get("note");
  const delegateTo = formData.get("delegate_to");
  const externalRef = formData.get("external_ref");

  if (typeof decisionId != "string" || !decisionId) {
    throw new Error("decision_id is required");
  }
  if (
    action !== "approve" &&
    action !== "reject" &&
    action !== "revise" &&
    action !== "delegate" &&
    action !== "execute"
  ) {
    throw new Error("Unsupported decision action");
  }

  const payload = {
    actor: getActor(formData),
    note:
      typeof note === "string" && note.trim()
        ? note.trim()
        : "Resolved from the BOSS executive console shell.",
    delegate_to:
      typeof delegateTo === "string" && delegateTo.trim()
        ? delegateTo.trim()
        : undefined,
    external_ref:
      typeof externalRef === "string" && externalRef.trim()
        ? externalRef.trim()
        : undefined,
  };
  try {
    if (action === "approve") {
      await client.approveDecision(decisionId, payload);
    } else if (action === "reject") {
      await client.rejectDecision(decisionId, payload);
    } else if (action === "revise") {
      await client.reviseDecision(decisionId, payload);
    } else if (action === "delegate") {
      await client.delegateDecision(decisionId, payload);
    } else {
      await client.executeDecision(decisionId, payload);
    }
  } catch (e) {
    // Don't crash the whole page on an API error — surface it and move on.
    console.error("decision action failed", e);
  }
  revalidatePath("/");
}

export async function submitCommandAction(formData: FormData): Promise<void> {
  const projectId = getProjectId(formData);
  const text = String(formData.get("command") ?? "").trim();

  // Semantic outcome intent: budget intensity (e.g. "set intensity to 70", "budget intensity 40").
  // Recognized ops intents act immediately; everything else flows to the governed command pipeline.
  const m = text.match(/\b(?:operational\s+)?intensity\b[^0-9]*(\d{1,3})/i);
  if (m) {
    const intensity = Math.max(0, Math.min(100, Number(m[1])));
    await fetch(`${API_URL}/v1/budget/set_intensity`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ scope_id: projectId, intensity, actor: getActor(formData) }),
    });
    revalidatePath("/");
    return;
  }

  const payload = buildCommandRequest(
    formData.get("command"),
    formData.get("deadline"),
    projectId,
    getActor(formData),
  );
  await client.proposeCommand(payload);
  revalidatePath("/");
}

export async function createKpiAction(formData: FormData): Promise<void> {
  const targetValue = parseNumber(formData.get("target_value"), 0);
  if (targetValue <= 0) {
    throw new Error("target_value must be greater than zero");
  }
  const currentValue = parseNumber(formData.get("current_value"), 0);
  if (currentValue < 0) {
    throw new Error("current_value must be greater than or equal to zero");
  }
  await client.createKpi({
    project_id: getProjectId(formData),
    kpi_key: String(formData.get("kpi_key") ?? ""),
    name: String(formData.get("name") ?? ""),
    description: String(formData.get("description") ?? "") || undefined,
    target_value: targetValue,
    current_value: currentValue,
    unit: String(formData.get("unit") ?? "count") || "count",
    owner_role_id: String(formData.get("owner_role_id") ?? "") || undefined,
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function refreshHealthSnapshotAction(formData: FormData): Promise<void> {
  await client.refreshHealthSnapshot({
    project_id: getProjectId(formData),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function createBreakGlassGrantAction(
  formData: FormData,
): Promise<void> {
  const targetMemberId = formData.get("target_member_id");
  const reason = formData.get("reason");
  const ttlMinutes = Number(formData.get("ttl_minutes") ?? 60);
  if (typeof targetMemberId !== "string" || !targetMemberId.trim()) {
    throw new Error("target_member_id is required");
  }
  if (typeof reason !== "string" || !reason.trim()) {
    throw new Error("reason is required");
  }
  await client.createBreakGlassGrant({
    project_id: getProjectId(formData),
    target_member_id: targetMemberId.trim(),
    reason: reason.trim(),
    scope: parseOptionalJsonObject(formData.get("scope_json")),
    granted_permissions: parseLines(formData.get("granted_permissions")),
    ttl_minutes: Number.isFinite(ttlMinutes) ? ttlMinutes : 60,
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function revokeBreakGlassGrantAction(
  formData: FormData,
): Promise<void> {
  const grantId = formData.get("grant_id");
  const reason = formData.get("reason");
  if (typeof grantId !== "string" || !grantId.trim()) {
    throw new Error("grant_id is required");
  }
  if (typeof reason !== "string" || !reason.trim()) {
    throw new Error("reason is required");
  }
  await client.revokeBreakGlassGrant(grantId.trim(), {
    actor: getActor(formData),
    reason: reason.trim(),
  });
  revalidatePath("/");
}

export async function upsertOrgMemberAction(formData: FormData): Promise<void> {
  const orgId = formData.get("org_id");
  const memberId = formData.get("member_id");
  const displayName = formData.get("display_name");
  if (typeof orgId !== "string" || !orgId.trim()) {
    throw new Error("org_id is required");
  }
  if (typeof memberId !== "string" || !memberId.trim()) {
    throw new Error("member_id is required");
  }
  if (typeof displayName !== "string" || !displayName.trim()) {
    throw new Error("display_name is required");
  }
  await client.upsertOrgMember({
    org_id: orgId.trim(),
    project_id: getProjectId(formData),
    member_id: memberId.trim(),
    display_name: displayName.trim(),
    member_type: getOptionalString(formData, "member_type") ?? "human",
    role_keys: parseLines(formData.get("role_keys")),
    permissions: parseLines(formData.get("permissions")),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function installPackAction(formData: FormData): Promise<void> {
  const packId = formData.get("pack_id");
  if (typeof packId !== "string" || !packId) {
    throw new Error("pack_id is required");
  }
  await client.installPack({
    project_id: getProjectId(formData),
    pack_id: packId,
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function importPackAction(formData: FormData): Promise<void> {
  const yamlContent = await readPackYaml(formData);
  try {
    await client.importPack({
      project_id: getProjectId(formData),
      yaml_content: yamlContent,
      actor: getActor(formData),
    });
  } catch (error) {
    throw packYamlApiError(error, "Pack import failed.");
  }
  revalidatePath("/");
}

export async function exportPackYaml(projectId: string, packId: string): Promise<string> {
  try {
    const yaml = await client.exportPack(projectId, packId);
    if (typeof yaml !== "string" || !yaml) {
      throw new Error("Pack export returned empty YAML.");
    }
    return yaml;
  } catch (error) {
    throw packYamlApiError(error, "Pack export failed.");
  }
}

export async function exportPackAction(
  formData: FormData,
): Promise<{ yaml: string; packId: string }> {
  const packId = formData.get("pack_id");
  if (typeof packId !== "string" || !packId.trim()) {
    throw new Error("pack_id is required");
  }
  const yaml = await exportPackYaml(getProjectId(formData), packId.trim());
  return { yaml, packId: packId.trim() };
}

export async function importSkillAction(formData: FormData): Promise<void> {
  const yamlContent = await readSkillYaml(formData);
  try {
    await client.importSkill({
      project_id: getProjectId(formData),
      bundle: yamlContent,
      allow_untrusted: true,
      actor: getActor(formData),
    });
  } catch (error) {
    throw packYamlApiError(error, "Skill import failed.");
  }
  revalidatePath("/");
}

export async function exportSkillYaml(
  projectId: string,
  skillId: string,
  actor = "ceo",
): Promise<string> {
  try {
    const yaml = await client.exportSkill(projectId, skillId, actor);
    if (typeof yaml !== "string" || !yaml) {
      throw new Error("Skill export returned empty YAML.");
    }
    return yaml;
  } catch (error) {
    throw packYamlApiError(error, "Skill export failed.");
  }
}

export async function exportSkillAction(
  formData: FormData,
): Promise<{ yaml: string; skillId: string }> {
  const skillId = formData.get("skill_id");
  if (typeof skillId !== "string" || !skillId.trim()) {
    throw new Error("skill_id is required");
  }
  const yaml = await exportSkillYaml(
    getProjectId(formData),
    skillId.trim(),
    getActor(formData),
  );
  return { yaml, skillId: skillId.trim() };
}

async function readPackYaml(formData: FormData): Promise<string> {
  const pasted = formData.get("yaml");
  if (typeof pasted === "string" && pasted.trim()) {
    return pasted;
  }
  const uploaded = formData.get("yaml_file");
  if (uploaded instanceof Blob) {
    const text = await uploaded.text();
    if (text.trim()) {
      return text;
    }
  }
  throw new Error("Pack YAML is required");
}

async function readSkillYaml(formData: FormData): Promise<string> {
  const pasted = formData.get("yaml");
  if (typeof pasted === "string" && pasted.trim()) {
    return pasted;
  }
  const uploaded = formData.get("yaml_file");
  if (uploaded instanceof Blob) {
    const text = await uploaded.text();
    if (text.trim()) {
      return text;
    }
  }
  throw new Error("Skill YAML is required");
}

function packYamlApiError(error: unknown, fallback: string): Error {
  if (error instanceof BossApiError) {
    const detail = extractBossDetail(error.body);
    return new Error(detail || error.message || fallback);
  }
  if (error instanceof Error) {
    return error;
  }
  return new Error(fallback);
}

function extractBossDetail(body: unknown): string | undefined {
  if (typeof body === "string" && body.trim()) {
    return body;
  }
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) {
      return detail;
    }
  }
  return undefined;
}

export async function installAgentModuleAction(formData: FormData): Promise<void> {
  const moduleId = formData.get("module_id");
  if (typeof moduleId !== "string" || !moduleId) {
    throw new Error("module_id is required");
  }
  await client.installAgentModule({
    project_id: getProjectId(formData),
    module_id: moduleId,
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function syncHostedMarketplaceAction(
  formData: FormData,
): Promise<void> {
  await client.syncHostedMarketplace({
    project_id: getProjectId(formData),
    hosted_org_id: getOptionalString(formData, "hosted_org_id"),
    metadata: parseOptionalJsonObjectField(
      formData.get("metadata_json"),
      "metadata_json",
    ),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function uninstallAgentModuleAction(
  formData: FormData,
): Promise<void> {
  const moduleId = formData.get("module_id");
  if (typeof moduleId !== "string" || !moduleId) {
    throw new Error("module_id is required");
  }
  await client.uninstallAgentModule(moduleId, {
    project_id: getProjectId(formData),
    actor: getActor(formData),
    reason: getOptionalString(formData, "reason"),
  });
  revalidatePath("/");
}

export async function rollbackAgentModuleAction(
  formData: FormData,
): Promise<void> {
  const moduleId = formData.get("module_id");
  if (typeof moduleId !== "string" || !moduleId) {
    throw new Error("module_id is required");
  }
  await client.rollbackAgentModule(moduleId, {
    project_id: getProjectId(formData),
    actor: getActor(formData),
    reason: getOptionalString(formData, "reason"),
  });
  revalidatePath("/");
}

export async function startCloudEasyConnectAction(
  formData: FormData,
): Promise<void> {
  const method = String(formData.get("method") ?? "device").trim() || "device";
  const projectId = getProjectId(formData);
  const actor = getActor(formData);
  // Browser return lands on /cloud/callback which finishes the link without pasting secrets.
  let returnUrl = getOptionalString(formData, "return_url");
  if (!returnUrl && method === "browser") {
    const { headers } = await import("next/headers");
    const h = await headers();
    const host = h.get("x-forwarded-host") || h.get("host");
    const proto = h.get("x-forwarded-proto") || "https";
    if (host) {
      returnUrl = `${proto}://${host}/cloud/callback?project_id=${encodeURIComponent(projectId)}&actor=${encodeURIComponent(actor)}`;
    }
  }
  const result = await client.startCloudEasyConnect({
    project_id: projectId,
    method: method === "browser" ? "browser" : "device",
    return_url: returnUrl,
    actor,
  });
  // Browser flow: open hosted authorization when available.
  if (
    method === "browser" &&
    typeof result.authorization_url === "string" &&
    result.authorization_url.startsWith("http") &&
    result.status !== "blocked"
  ) {
    redirect(result.authorization_url);
  }
  revalidatePath("/");
}

export async function unlinkCloudAction(formData: FormData): Promise<void> {
  await client.unlinkCloud({
    project_id: getProjectId(formData),
    actor: getActor(formData),
    reason:
      getOptionalString(formData, "reason") ??
      "Operator disconnected BOSS Cloud from Mission Control.",
  });
  revalidatePath("/");
}

export async function pollCloudEasyConnectAction(
  formData: FormData,
): Promise<void> {
  const sessionId = String(formData.get("session_id") ?? "").trim();
  if (!sessionId) return;
  await client.pollCloudEasyConnect(
    sessionId,
    getProjectId(formData),
    getActor(formData),
  );
  revalidatePath("/");
}

export async function linkAccountConnectAction(
  formData: FormData,
): Promise<void> {
  const hostedOrgId = formData.get("hosted_org_id");
  if (typeof hostedOrgId !== "string" || !hostedOrgId.trim()) {
    throw new Error("hosted_org_id is required");
  }
  await client.linkAccountConnect({
    project_id: getProjectId(formData),
    hosted_org_id: hostedOrgId.trim(),
    connection_ref: getOptionalString(formData, "connection_ref"),
    marketplace_identity: parseOptionalJsonObjectField(
      formData.get("marketplace_identity_json"),
      "marketplace_identity_json",
    ),
    billing_portal_ref: getOptionalString(formData, "billing_portal_ref"),
    connector_authorization_refs: parseLines(
      formData.get("connector_authorization_refs"),
    ),
    optional_telemetry_enabled: parseProviderOpsCheckbox(
      formData,
      "optional_telemetry_enabled",
      true,
    ),
    metadata: parseOptionalJsonObjectField(
      formData.get("metadata_json"),
      "metadata_json",
    ),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function unlinkAccountConnectAction(
  formData: FormData,
): Promise<void> {
  await client.unlinkAccountConnect({
    project_id: getProjectId(formData),
    hosted_org_id: getOptionalString(formData, "hosted_org_id"),
    reason:
      getOptionalString(formData, "reason") ??
      "Operator unlinked Account Connect from Mission Control.",
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function syncAccountConnectAction(formData: FormData): Promise<void> {
  await client.syncAccountConnect({
    project_id: getProjectId(formData),
    hosted_org_id: getOptionalString(formData, "hosted_org_id"),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

/** Post-link Cloud card: prefer dedicated cloud sync if present, else Account Connect sync. */
export async function syncCloudAction(formData: FormData): Promise<void> {
  const projectId = getProjectId(formData);
  const actor = getActor(formData);
  const hostedOrgId = getOptionalString(formData, "hosted_org_id");
  const maybeCloudSync = (
    client as {
      syncCloud?: (payload: {
        project_id: string;
        hosted_org_id?: string;
        actor?: string;
      }) => Promise<unknown>;
    }
  ).syncCloud;
  if (typeof maybeCloudSync === "function") {
    await maybeCloudSync({
      project_id: projectId,
      hosted_org_id: hostedOrgId,
      actor,
    });
  } else {
    await client.syncAccountConnect({
      project_id: projectId,
      hosted_org_id: hostedOrgId,
      actor,
    });
  }
  revalidatePath("/");
}

/** Mini team invite from the Cloud Easy Connect card when linked. */
export async function inviteCloudTeamAction(formData: FormData): Promise<void> {
  const email = formData.get("email");
  if (typeof email !== "string" || !email.trim()) {
    throw new Error("email is required");
  }
  const maybeInvite = (
    client as {
      createAccountConnectTeamInvite?: typeof client.createAccountConnectTeamInvite;
      inviteCloudTeam?: (payload: {
        project_id: string;
        email: string;
        actor?: string;
      }) => Promise<unknown>;
    }
  ).inviteCloudTeam;
  if (typeof maybeInvite === "function") {
    await maybeInvite({
      project_id: getProjectId(formData),
      email: email.trim(),
      actor: getActor(formData),
    });
  } else if (typeof client.createAccountConnectTeamInvite === "function") {
    await client.createAccountConnectTeamInvite({
      project_id: getProjectId(formData),
      email: email.trim(),
      display_name: getOptionalString(formData, "display_name"),
      role_keys: parseLines(formData.get("role_keys")),
      permissions: parseLines(formData.get("permissions")),
      actor: getActor(formData),
    });
  } else {
    throw new Error("Cloud team invite client method is not available on this control plane.");
  }
  revalidatePath("/");
}

/** Save Cloud-linked evidence retention policy when /v1/cloud/retention is available. */
export async function saveCloudRetentionAction(
  formData: FormData,
): Promise<void> {
  const retentionRaw = formData.get("retention_days");
  const days =
    typeof retentionRaw === "string" ? Number.parseInt(retentionRaw, 10) : NaN;
  if (!Number.isFinite(days)) {
    throw new Error("retention_days is required");
  }
  const putRetention = (
    client as {
      putCloudRetention?: typeof client.putCloudRetention;
    }
  ).putCloudRetention;
  if (typeof putRetention !== "function") {
    throw new Error(
      "Cloud retention API is not available on this control plane yet.",
    );
  }
  await putRetention({
    project_id: getProjectId(formData),
    retention_days: days,
    reason:
      getOptionalString(formData, "reason") ??
      "Operator updated evidence retention from Mission Control.",
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function runAccountConnectHealthCheckAction(
  formData: FormData,
): Promise<void> {
  await client.runAccountConnectHealthCheck({
    project_id: getProjectId(formData),
    hosted_org_id: getOptionalString(formData, "hosted_org_id"),
    check_kind: getOptionalString(formData, "check_kind") ?? "hosted_service",
    metadata: parseOptionalJsonObjectField(
      formData.get("metadata_json"),
      "metadata_json",
    ),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function createAccountConnectAuthSessionAction(
  formData: FormData,
): Promise<void> {
  await client.createAccountConnectAuthSession({
    project_id: getProjectId(formData),
    hosted_org_id: getOptionalString(formData, "hosted_org_id"),
    return_url: getOptionalString(formData, "return_url"),
    metadata: parseOptionalJsonObjectField(
      formData.get("metadata_json"),
      "metadata_json",
    ),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function completeAccountConnectAuthSessionAction(
  formData: FormData,
): Promise<void> {
  const sessionId = formData.get("session_id");
  const code = formData.get("code");
  const state = formData.get("state");
  if (typeof sessionId !== "string" || !sessionId.trim()) {
    throw new Error("session_id is required");
  }
  if (typeof code !== "string" || !code.trim()) {
    throw new Error("code is required");
  }
  if (typeof state !== "string" || !state.trim()) {
    throw new Error("state is required");
  }
  await client.completeAccountConnectAuthSession(sessionId.trim(), {
    project_id: getProjectId(formData),
    code: code.trim(),
    state: state.trim(),
    metadata: parseOptionalJsonObjectField(
      formData.get("metadata_json"),
      "metadata_json",
    ),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function createAccountConnectTeamInviteAction(
  formData: FormData,
): Promise<void> {
  const email = formData.get("email");
  if (typeof email !== "string" || !email.trim()) {
    throw new Error("email is required");
  }
  await client.createAccountConnectTeamInvite({
    project_id: getProjectId(formData),
    email: email.trim(),
    display_name: getOptionalString(formData, "display_name"),
    role_keys: parseLines(formData.get("role_keys")),
    permissions: parseLines(formData.get("permissions")),
    metadata: parseOptionalJsonObjectField(
      formData.get("metadata_json"),
      "metadata_json",
    ),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function revokeAccountConnectTeamInviteAction(
  formData: FormData,
): Promise<void> {
  const inviteId = formData.get("invite_id");
  if (typeof inviteId !== "string" || !inviteId.trim()) {
    throw new Error("invite_id is required");
  }
  await client.revokeAccountConnectTeamInvite(inviteId.trim(), {
    project_id: getProjectId(formData),
    reason: getOptionalString(formData, "reason"),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function createAccountConnectBillingPortalSessionAction(
  formData: FormData,
): Promise<void> {
  await client.createAccountConnectBillingPortalSession({
    project_id: getProjectId(formData),
    return_url: getOptionalString(formData, "return_url"),
    metadata: parseOptionalJsonObjectField(
      formData.get("metadata_json"),
      "metadata_json",
    ),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function upsertEnterpriseClusterAction(
  formData: FormData,
): Promise<void> {
  await client.upsertEnterpriseCluster({
    project_id: getProjectId(formData),
    deployment_mode:
      getOptionalString(formData, "deployment_mode") ?? "enterprise_cloud",
    account_connect_mode:
      getOptionalString(formData, "account_connect_mode") ?? "attached",
    worker_pool_strategy:
      getOptionalString(formData, "worker_pool_strategy") ??
      "dedicated_managed_workers",
    ha_profile: getOptionalString(formData, "ha_profile") ?? "regional_ha",
    data_residency: getOptionalString(formData, "data_residency") ?? "us",
    identity_controls: parseOptionalJsonObjectField(
      formData.get("identity_controls_json"),
      "identity_controls_json",
    ),
    governance_controls: parseOptionalJsonObjectField(
      formData.get("governance_controls_json"),
      "governance_controls_json",
    ),
    audit_controls: parseOptionalJsonObjectField(
      formData.get("audit_controls_json"),
      "audit_controls_json",
    ),
    private_worker_pools: parseOptionalJsonArray(
      formData.get("private_worker_pools_json"),
      "private_worker_pools_json",
    ),
    compliance_packs: parseLines(formData.get("compliance_packs")),
    support_tier: getOptionalString(formData, "support_tier") ?? "enterprise",
    support_sla: parseOptionalJsonObjectField(
      formData.get("support_sla_json"),
      "support_sla_json",
    ),
    status: getOptionalString(formData, "status") ?? "planned",
    metadata: parseOptionalJsonObjectField(
      formData.get("metadata_json"),
      "metadata_json",
    ),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function createEnterpriseClusterValidationRunAction(
  formData: FormData,
): Promise<void> {
  const clusterId = formData.get("enterprise_cluster_id");
  if (typeof clusterId !== "string" || !clusterId.trim()) {
    throw new Error("enterprise_cluster_id is required");
  }
  await client.createEnterpriseClusterValidationRun({
    project_id: getProjectId(formData),
    enterprise_cluster_id: clusterId.trim(),
    validation_kind:
      getOptionalString(formData, "validation_kind") ?? "multi_node_readiness",
    status: getOptionalString(formData, "status") ?? "passed",
    checks: parseRequiredJsonArrayField(formData.get("checks_json"), "checks_json"),
    evidence_refs: parseLines(formData.get("evidence_refs")),
    metadata: parseOptionalJsonObjectField(
      formData.get("metadata_json"),
      "metadata_json",
    ),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function createEnterpriseSupportCaseAction(
  formData: FormData,
): Promise<void> {
  const title = formData.get("title");
  const description = formData.get("description");
  if (typeof title !== "string" || !title.trim()) {
    throw new Error("title is required");
  }
  if (typeof description !== "string" || !description.trim()) {
    throw new Error("description is required");
  }
  await client.createEnterpriseSupportCase({
    project_id: getProjectId(formData),
    title: title.trim(),
    description: description.trim(),
    severity: getOptionalString(formData, "severity") ?? "normal",
    category: getOptionalString(formData, "category") ?? "operational_trust",
    enterprise_cluster_id: getOptionalString(formData, "enterprise_cluster_id"),
    escalate_to_hosted: parseProviderOpsCheckbox(
      formData,
      "escalate_to_hosted",
      false,
    ),
    metadata: parseOptionalJsonObjectField(
      formData.get("metadata_json"),
      "metadata_json",
    ),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function updateEnterpriseSupportCaseAction(
  formData: FormData,
): Promise<void> {
  const caseId = formData.get("case_id");
  if (typeof caseId !== "string" || !caseId.trim()) {
    throw new Error("case_id is required");
  }
  await client.updateEnterpriseSupportCase(caseId.trim(), {
    project_id: getProjectId(formData),
    status: getOptionalString(formData, "status") ?? "in_progress",
    assigned_to: getOptionalString(formData, "assigned_to"),
    metadata: parseOptionalJsonObjectField(
      formData.get("metadata_json"),
      "metadata_json",
    ),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function upsertEnterpriseIdentityProviderAction(
  formData: FormData,
): Promise<void> {
  const providerKey = formData.get("provider_key");
  const providerType = formData.get("provider_type");
  const displayName = formData.get("display_name");
  if (typeof providerKey !== "string" || !providerKey.trim()) {
    throw new Error("provider_key is required");
  }
  if (typeof providerType !== "string" || !providerType.trim()) {
    throw new Error("provider_type is required");
  }
  if (typeof displayName !== "string" || !displayName.trim()) {
    throw new Error("display_name is required");
  }
  await client.upsertEnterpriseIdentityProvider({
    project_id: getProjectId(formData),
    provider_key: providerKey.trim(),
    provider_type: providerType.trim(),
    display_name: displayName.trim(),
    issuer: getOptionalString(formData, "issuer"),
    audience: getOptionalString(formData, "audience"),
    entity_id: getOptionalString(formData, "entity_id"),
    sso_url: getOptionalString(formData, "sso_url"),
    jwks_url: getOptionalString(formData, "jwks_url"),
    certificate_fingerprint: getOptionalString(
      formData,
      "certificate_fingerprint",
    ),
    scim_base_url: getOptionalString(formData, "scim_base_url"),
    secret_ref: getOptionalString(formData, "secret_ref"),
    status: getOptionalString(formData, "status") ?? "planned",
    test_status: getOptionalString(formData, "test_status") ?? "not_tested",
    metadata: parseOptionalJsonObjectField(
      formData.get("metadata_json"),
      "metadata_json",
    ),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function upsertEnterpriseIdentityRolloutPolicyAction(
  formData: FormData,
): Promise<void> {
  const providerType = formData.get("provider_type");
  const runtimeMode = formData.get("runtime_mode");
  if (typeof providerType !== "string" || !providerType.trim()) {
    throw new Error("provider_type is required");
  }
  await client.upsertEnterpriseIdentityRolloutPolicy({
    project_id: getProjectId(formData),
    provider_type: providerType.trim(),
    runtime_mode:
      runtimeMode === "metadata_only" ||
      runtimeMode === "auth_allowed" ||
      runtimeMode === "provisioning_allowed" ||
      runtimeMode === "disabled"
        ? runtimeMode
        : "disabled",
    allowed_domains: parseLines(formData.get("allowed_domains")),
    rollout_notes: getOptionalString(formData, "rollout_notes"),
    metadata: parseOptionalJsonObjectField(
      formData.get("metadata_json"),
      "metadata_json",
    ),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function upsertEnterpriseScimGroupAction(
  formData: FormData,
): Promise<void> {
  const displayName = formData.get("display_name");
  if (typeof displayName !== "string" || !displayName.trim()) {
    throw new Error("display_name is required");
  }
  await client.upsertEnterpriseScimGroup({
    project_id: getProjectId(formData),
    external_id: getOptionalString(formData, "external_id"),
    display_name: displayName.trim(),
    role_keys: parseLines(formData.get("role_keys")),
    permissions: parseLines(formData.get("permissions")),
    raw_group: parseOptionalJsonObjectField(
      formData.get("raw_group_json"),
      "raw_group_json",
    ),
    status: getOptionalString(formData, "status") ?? "active",
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function upsertEnterpriseScimUserAction(
  formData: FormData,
): Promise<void> {
  const userName = formData.get("user_name");
  const displayName = formData.get("display_name");
  if (typeof userName !== "string" || !userName.trim()) {
    throw new Error("user_name is required");
  }
  if (typeof displayName !== "string" || !displayName.trim()) {
    throw new Error("display_name is required");
  }
  await client.upsertEnterpriseScimUser({
    project_id: getProjectId(formData),
    external_id: getOptionalString(formData, "external_id"),
    user_name: userName.trim(),
    display_name: displayName.trim(),
    active: parseProviderOpsCheckbox(formData, "active", true),
    role_keys: parseLines(formData.get("role_keys")),
    permissions: parseLines(formData.get("permissions")),
    groups: parseLines(formData.get("groups")),
    raw_profile: parseOptionalJsonObjectField(
      formData.get("raw_profile_json"),
      "raw_profile_json",
    ),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function connectEntitlementSourceAction(
  formData: FormData,
): Promise<void> {
  const sourceId = formData.get("source_id");
  const connectionRef = formData.get("connection_ref");
  if (typeof sourceId !== "string" || !sourceId.trim()) {
    throw new Error("source_id is required");
  }
  if (typeof connectionRef !== "string" || !connectionRef.trim()) {
    throw new Error("connection_ref is required");
  }
  await client.connectEntitlementSource(sourceId.trim(), {
    project_id: getProjectId(formData),
    connection_ref: connectionRef.trim(),
    auth_method: getOptionalString(formData, "auth_method"),
    oauth_scopes: parseLines(formData.get("oauth_scopes")),
    connection_metadata: parseOptionalJsonObjectField(
      formData.get("connection_metadata_json"),
      "connection_metadata_json",
    ),
    observed_capacity: parseOptionalJsonObjectField(
      formData.get("observed_capacity_json"),
      "observed_capacity_json",
    ),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function uploadModelCredentialAction(
  formData: FormData,
): Promise<void> {
  const sourceId = formData.get("source_id");
  const value = formData.get("credential_value");
  const backend = getOptionalString(formData, "backend") ?? "platform_secret_store";
  if (typeof sourceId !== "string" || !sourceId.trim()) {
    throw new Error("source_id is required");
  }
  if (typeof value !== "string" || !value.trim()) {
    throw new Error("credential_value is required");
  }
  if (!["platform_secret_store", "vault", "local_runner"].includes(backend)) {
    throw new Error("unsupported model credential backend");
  }
  await client.uploadModelCredential(sourceId.trim(), {
    project_id: getProjectId(formData),
    value: value.trim(),
    backend: backend as "platform_secret_store" | "vault" | "local_runner",
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function createEntitlementConnectionSessionAction(
  formData: FormData,
): Promise<void> {
  const sourceId = formData.get("source_id");
  if (typeof sourceId !== "string" || !sourceId.trim()) {
    throw new Error("source_id is required");
  }
  await client.createEntitlementConnectionSession(sourceId.trim(), {
    project_id: getProjectId(formData),
    connection_mode: getOptionalString(formData, "connection_mode"),
    requested_scopes: parseLines(formData.get("requested_scopes")),
    metadata: parseOptionalJsonObjectField(
      formData.get("metadata_json"),
      "metadata_json",
    ),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function completeEntitlementConnectionSessionAction(
  formData: FormData,
): Promise<void> {
  const sessionId = formData.get("session_id");
  const connectionRef = formData.get("connection_ref");
  if (typeof sessionId !== "string" || !sessionId.trim()) {
    throw new Error("session_id is required");
  }
  if (typeof connectionRef !== "string" || !connectionRef.trim()) {
    throw new Error("connection_ref is required");
  }
  await client.completeEntitlementConnectionSession(sessionId.trim(), {
    project_id: getProjectId(formData),
    connection_ref: connectionRef.trim(),
    oauth_scopes: parseLines(formData.get("oauth_scopes")),
    connection_metadata: parseOptionalJsonObjectField(
      formData.get("connection_metadata_json"),
      "connection_metadata_json",
    ),
    observed_capacity: parseOptionalJsonObjectField(
      formData.get("observed_capacity_json"),
      "observed_capacity_json",
    ),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function disconnectEntitlementSourceAction(
  formData: FormData,
): Promise<void> {
  const sourceId = formData.get("source_id");
  if (typeof sourceId !== "string" || !sourceId.trim()) {
    throw new Error("source_id is required");
  }
  await client.disconnectEntitlementSource(sourceId.trim(), {
    project_id: getProjectId(formData),
    reason: getOptionalString(formData, "reason") ?? "Disconnected from UI.",
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function createReplayBundleAction(formData: FormData): Promise<void> {
  await client.createReplayBundle({
    project_id: getProjectId(formData),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function createOpsDigestAction(formData: FormData): Promise<void> {
  await client.createOpsDigest({
    project_id: getProjectId(formData),
    actor: getActor(formData),
    limit: parseInteger(formData.get("limit"), 25),
  });
  revalidatePath("/");
}

export async function createDeadletterIncidentAction(
  formData: FormData,
): Promise<void> {
  const deadletterId = formData.get("deadletter_id");
  if (typeof deadletterId !== "string" || !deadletterId.trim()) {
    throw new Error("deadletter_id is required");
  }
  await client.createDeadletterIncident(deadletterId.trim(), {
    actor: getActor(formData),
    severity:
      (getOptionalString(formData, "severity") as
        | "low"
        | "medium"
        | "high"
        | "critical"
        | undefined) ?? "high",
    summary: getOptionalString(formData, "summary"),
  });
  revalidatePath("/");
}

export async function requeueDeadletterJobAction(
  formData: FormData,
): Promise<void> {
  const deadletterId = formData.get("deadletter_id");
  if (typeof deadletterId !== "string" || !deadletterId.trim()) {
    throw new Error("deadletter_id is required");
  }
  const action = getOptionalString(formData, "remediation_action") ?? "retry";
  const reason =
    getOptionalString(formData, "reason") ?? "operator_requested_requeue";
  if (action === "retry") {
    await client.retryDeadletter(deadletterId.trim(), {
      actor: getActor(formData),
      reason,
    });
  } else if (action === "requeue" || action === "escalate" || action === "hold") {
    await client.createDeadletterRemediation(deadletterId.trim(), {
      actor: getActor(formData),
      action,
      reason,
    });
  } else {
    throw new Error("remediation_action must be retry, requeue, escalate, or hold");
  }
  revalidatePath("/");
}

export async function createDeadletterRemediationAction(
  formData: FormData,
): Promise<void> {
  return requeueDeadletterJobAction(formData);
}

export async function createComplianceEvidenceBundleAction(
  formData: FormData,
): Promise<void> {
  await client.createComplianceEvidenceBundle({
    project_id: getProjectId(formData),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function upsertCommsChannelPolicyAction(
  formData: FormData,
): Promise<void> {
  const provider = formData.get("provider");
  const deliveryMode = formData.get("delivery_mode");
  if (typeof provider !== "string" || !provider.trim()) {
    throw new Error("provider is required");
  }
  if (typeof deliveryMode !== "string" || !deliveryMode.trim()) {
    throw new Error("delivery_mode is required");
  }
  await client.upsertCommsChannelPolicy({
    project_id: getProjectId(formData),
    provider: provider.trim(),
    delivery_mode:
      deliveryMode === "disabled" ||
      deliveryMode === "draft_only" ||
      deliveryMode === "real_send_allowed"
        ? deliveryMode
        : "draft_only",
    allowed_data_boundaries: parseDataBoundaries(
      formData.get("allowed_data_boundaries"),
    ),
    rollout_notes: getOptionalString(formData, "rollout_notes"),
    metadata: parseOptionalJsonObjectField(
      formData.get("metadata_json"),
      "metadata_json",
    ),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function submitOutboxDraftAction(formData: FormData): Promise<void> {
  const channel = formData.get("channel");
  const subject = formData.get("subject");
  const body = formData.get("body");

  if (typeof channel !== "string" || !channel.trim()) {
    throw new Error("channel is required");
  }
  if (typeof body !== "string" || !body.trim()) {
    throw new Error("body is required");
  }

  const dedupeKey = `outbox-${Date.now()}`;
  await client.createOutboxMessage({
    project_id: getProjectId(formData),
    channel: channel.trim(),
    subject: typeof subject === "string" && subject.trim() ? subject.trim() : undefined,
    body: body.trim(),
    dedupe_key: dedupeKey,
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function queueOutboxMessageAction(formData: FormData): Promise<void> {
  const messageId = formData.get("message_id");
  const urgency = formData.get("urgency");
  if (typeof messageId !== "string" || !messageId) {
    throw new Error("message_id is required");
  }

  const projectId = getProjectId(formData);
  const reservation = await client.reserveBudget({
    project_id: projectId,
    category: "comms",
    amount_usd_micros: 50_000,
    reason: "Queue outbound communication from the executive console.",
  });
  if (reservation.status !== "reserved" || !reservation.reservation_id) {
    throw new Error(reservation.message);
  }

  await client.queueOutboxDelivery(messageId, {
    project_id: projectId,
    reservation_id: reservation.reservation_id,
    dedupe_key: `outbox-queue-${messageId}-${Date.now()}`,
    actor: getActor(formData),
    urgency:
      urgency === "low" || urgency === "normal" || urgency === "urgent"
        ? urgency
        : "normal",
  });
  revalidatePath("/");
}

export async function dryRunOutboxMessageAction(formData: FormData): Promise<void> {
  const messageId = formData.get("message_id");
  if (typeof messageId !== "string" || !messageId) {
    throw new Error("message_id is required");
  }

  await client.dryRunOutboxMessage(messageId, {
    project_id: getProjectId(formData),
    actor: getActor(formData),
    dedupe_key: `outbox-dry-run-${messageId}-${Date.now()}`,
  });
  revalidatePath("/");
}

export async function createDecisionDigestAction(
  formData: FormData,
): Promise<void> {
  const channel = formData.get("channel");
  const maxItems = Number(formData.get("max_items") ?? 20);
  await client.createDecisionDigest({
    project_id: getProjectId(formData),
    actor: getActor(formData),
    channel:
      typeof channel === "string" && channel.trim()
        ? channel.trim()
        : undefined,
    max_items: Number.isFinite(maxItems) ? maxItems : 20,
    low_risk_max: 1,
    urgent_risk_min: 2,
    dedupe_key: `decision-digest-${Date.now()}`,
  });
  revalidatePath("/");
}

// ---- Project GitHub control plane ------------------------------------------

export async function createGitHubConnectionAction(
  formData: FormData,
): Promise<void> {
  const label = String(formData.get("connection_label") ?? "").trim();
  const credentialRef = String(formData.get("credential_ref") ?? "").trim();
  const authMethod = String(
    formData.get("auth_method") ?? "github_app",
  ).trim();
  if (!label || !credentialRef) {
    throw new Error("connection_label and credential_ref are required");
  }
  if (authMethod !== "github_app" && authMethod !== "personal_access_token") {
    throw new Error("auth_method must be github_app or personal_access_token");
  }
  await client.createGitHubConnection({
    project_id: getProjectId(formData),
    connection_label: label,
    auth_method: authMethod,
    credential_ref: credentialRef,
    app_id: getOptionalString(formData, "app_id"),
    installation_id: getOptionalString(formData, "installation_id"),
    api_base_url: getOptionalString(formData, "api_base_url"),
    required_permissions: parseLines(formData.get("required_permissions")),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function easyConnectGitHubAction(
  formData: FormData,
): Promise<void> {
  const label = String(
    formData.get("connection_label") ?? "Project GitHub",
  ).trim();
  const envKey = String(
    formData.get("env_credential_key") ?? "BOSS_GITHUB_TOKEN",
  )
    .trim()
    .toUpperCase();
  const authMethod = String(
    formData.get("auth_method") ?? "personal_access_token",
  ).trim();
  if (!label || !envKey) {
    throw new Error("connection_label and env_credential_key are required");
  }
  if (authMethod !== "github_app" && authMethod !== "personal_access_token") {
    throw new Error("auth_method must be github_app or personal_access_token");
  }
  const repository = String(formData.get("repository") ?? "").trim();
  const role = String(formData.get("role") ?? "primary").trim();
  if (role && !["primary", "source", "upstream", "mirror"].includes(role)) {
    throw new Error("role is invalid");
  }
  await client.easyConnectGitHub({
    project_id: getProjectId(formData),
    connection_label: label,
    auth_method: authMethod,
    env_credential_key: envKey,
    app_id: getOptionalString(formData, "app_id"),
    installation_id: getOptionalString(formData, "installation_id"),
    repository: repository || undefined,
    role: (role || "primary") as "primary" | "source" | "upstream" | "mirror",
    test_after_link: String(formData.get("test_after_link") ?? "true") !== "false",
    required_permissions: ["pull", "push", "triage"],
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function setGitHubConnectionStateAction(
  formData: FormData,
): Promise<void> {
  const connectionId = String(formData.get("connection_id") ?? "").trim();
  const operation = String(formData.get("operation") ?? "").trim();
  if (!connectionId) {
    throw new Error("connection_id is required");
  }
  const payload = {
    project_id: getProjectId(formData),
    actor: getActor(formData),
  };
  if (operation === "enable") {
    await client.enableGitHubConnection(connectionId, payload);
  } else if (operation === "disable") {
    await client.disableGitHubConnection(connectionId, payload);
  } else {
    throw new Error("operation must be enable or disable");
  }
  revalidatePath("/");
}

export async function createGitHubRepositoryBindingAction(
  formData: FormData,
): Promise<void> {
  const connectionId = String(formData.get("connection_id") ?? "").trim();
  const repository = String(formData.get("repository") ?? "").trim();
  const role = String(formData.get("role") ?? "primary").trim();
  if (!connectionId || !repository) {
    throw new Error("connection_id and repository are required");
  }
  if (!["primary", "source", "upstream", "mirror"].includes(role)) {
    throw new Error("role is invalid");
  }
  await client.createGitHubRepositoryBinding({
    project_id: getProjectId(formData),
    connection_id: connectionId,
    repository,
    role: role as "primary" | "source" | "upstream" | "mirror",
    default_branch: getOptionalString(formData, "default_branch"),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function testGitHubConnectionAction(
  formData: FormData,
): Promise<void> {
  const connectionId = String(formData.get("connection_id") ?? "").trim();
  const bindingId = String(
    formData.get("repository_binding_id") ?? "",
  ).trim();
  if (!connectionId || !bindingId) {
    throw new Error("connection_id and repository_binding_id are required");
  }
  await client.testGitHubConnection(connectionId, {
    project_id: getProjectId(formData),
    repository_binding_id: bindingId,
    dedupe_key: `mission-control-test-${connectionId}-${bindingId}-${Date.now()}`,
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function createGitHubBranchPolicyAction(
  formData: FormData,
): Promise<void> {
  const bindingId = String(
    formData.get("repository_binding_id") ?? "",
  ).trim();
  if (!bindingId) {
    throw new Error("repository_binding_id is required");
  }
  await client.createGitHubBranchPolicy({
    project_id: getProjectId(formData),
    repository_binding_id: bindingId,
    protected_ref_patterns: parseLines(formData.get("protected_ref_patterns")),
    allowed_branch_prefixes: parseLines(
      formData.get("allowed_branch_prefixes"),
    ),
    required_checks: parseLines(formData.get("required_checks")),
    required_approvals: parseInteger(
      formData.get("required_approvals"),
      1,
    ),
    require_pull_request: true,
    automation_updates_enabled: true,
    force_push_allowed: false,
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function captureGitHubBranchSnapshotAction(
  formData: FormData,
): Promise<void> {
  const bindingId = String(
    formData.get("repository_binding_id") ?? "",
  ).trim();
  const sourceRef = String(formData.get("source_ref") ?? "").trim();
  const targetRef = String(formData.get("target_ref") ?? "").trim();
  if (!bindingId || !sourceRef || !targetRef) {
    throw new Error(
      "repository_binding_id, source_ref, and target_ref are required",
    );
  }
  await client.captureGitHubBranchSnapshot({
    project_id: getProjectId(formData),
    repository_binding_id: bindingId,
    source_ref: sourceRef,
    target_ref: targetRef,
    dedupe_key: `mission-control-snapshot-${bindingId}-${sourceRef}-${targetRef}-${Date.now()}`,
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function planGitHubCodeSyncAction(
  formData: FormData,
): Promise<void> {
  const bindingId = String(
    formData.get("repository_binding_id") ?? "",
  ).trim();
  const snapshotId = String(formData.get("snapshot_id") ?? "").trim();
  if (!bindingId || !snapshotId) {
    throw new Error("repository_binding_id and snapshot_id are required");
  }
  await client.planGitHubCodeSync({
    project_id: getProjectId(formData),
    repository_binding_id: bindingId,
    snapshot_id: snapshotId,
    resolution_branch: getOptionalString(formData, "resolution_branch"),
    dedupe_key: `mission-control-sync-${bindingId}-${snapshotId}-${Date.now()}`,
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function scheduleGitHubReconciliationAction(
  formData: FormData,
): Promise<void> {
  const bindingId = getOptionalString(formData, "repository_binding_id");
  await client.scheduleGitHubReconciliationPoll({
    project_id: getProjectId(formData),
    repository_binding_id: bindingId,
    refs: parseLines(formData.get("refs")),
    dedupe_key: `mission-control-reconcile-${bindingId ?? "all"}-${Date.now()}`,
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function repairGitHubReconciliationAction(
  formData: FormData,
): Promise<void> {
  const runId = String(formData.get("reconciliation_run_id") ?? "").trim();
  if (!runId) {
    throw new Error("reconciliation_run_id is required");
  }
  await client.repairGitHubReconciliationRun(runId, {
    project_id: getProjectId(formData),
    dedupe_key: `mission-control-repair-${runId}-${Date.now()}`,
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function approveGitHubResolutionPlanAction(
  formData: FormData,
): Promise<void> {
  const planId = String(formData.get("resolution_plan_id") ?? "").trim();
  const summary = String(formData.get("review_summary") ?? "").trim();
  if (!planId || !summary) {
    throw new Error("resolution_plan_id and review_summary are required");
  }
  await client.approveGitHubResolutionPlan(planId, {
    project_id: getProjectId(formData),
    actor: getActor(formData),
    review_evidence: {
      verdict: "approved",
      summary,
      source: "github_mission_control",
    },
  });
  revalidatePath("/");
}

export async function authorizeGitHubMergeAction(
  formData: FormData,
): Promise<void> {
  const authorizationId = String(
    formData.get("merge_authorization_id") ?? "",
  ).trim();
  const summary = String(formData.get("review_summary") ?? "").trim();
  if (!authorizationId || !summary) {
    throw new Error("merge_authorization_id and review_summary are required");
  }
  await client.authorizeGitHubMerge(authorizationId, {
    project_id: getProjectId(formData),
    actor: getActor(formData),
    review_evidence: {
      verdict: "approved",
      summary,
      source: "github_mission_control",
    },
  });
  revalidatePath("/");
}

export async function enqueueGitHubMergeAction(
  formData: FormData,
): Promise<void> {
  const authorizationId = String(
    formData.get("merge_authorization_id") ?? "",
  ).trim();
  if (!authorizationId) {
    throw new Error("merge_authorization_id is required");
  }
  await client.enqueueGitHubMerge(authorizationId, {
    project_id: getProjectId(formData),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

const MIN_EASY_CONNECT_SECRET_LENGTH = 8;

export type EasyConnectProviderResult = {
  secret_ref_id: string;
};

export async function easyConnectProviderAction(
  formData: FormData,
): Promise<EasyConnectProviderResult> {
  const provider = formData.get("provider");
  const accountLabel = formData.get("account_label");
  const secretValue = formData.get("secret_value");
  if (typeof provider !== "string" || !provider.trim()) {
    throw new Error("provider is required");
  }
  if (typeof accountLabel !== "string" || !accountLabel.trim()) {
    throw new Error("account_label is required");
  }
  if (typeof secretValue !== "string" || !secretValue.trim()) {
    throw new Error("secret_value is required");
  }
  if (secretValue.trim().length < MIN_EASY_CONNECT_SECRET_LENGTH) {
    throw new Error(
      `secret_value is too short (minimum ${MIN_EASY_CONNECT_SECRET_LENGTH} characters)`,
    );
  }
  const body: Record<string, unknown> = {
    project_id: getProjectId(formData),
    actor: getActor(formData),
    provider: provider.trim(),
    account_label: accountLabel.trim(),
    secret_value: secretValue.trim(),
    backend: "local_vault",
    read_only: true,
  };
  const baseUrl = getOptionalString(formData, "base_url");
  if (baseUrl) {
    body.base_url = baseUrl;
  }
  const response = await fetch(`${API_URL}/v1/providers/easy-connect`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(body),
  });
  const out = (await response.json().catch(() => ({}))) as Record<
    string,
    unknown
  >;
  if (!response.ok) {
    const message = failureText(out?.detail, response.status).replaceAll(
      secretValue,
      "[redacted]",
    );
    throw new Error(message);
  }
  const secret = (out.secret ?? {}) as Record<string, unknown>;
  const secretRefId =
    (typeof out.secret_ref_id === "string" && out.secret_ref_id) ||
    (typeof secret.secret_ref_id === "string" && secret.secret_ref_id) ||
    "";
  if (!secretRefId) {
    throw new Error("Provider connected but no secret_ref was returned.");
  }
  revalidatePath("/");
  return { secret_ref_id: secretRefId };
}

export async function createProviderAccountAction(
  formData: FormData,
): Promise<void> {
  const provider = formData.get("provider");
  const accountLabel = formData.get("account_label");
  if (typeof provider !== "string" || !provider.trim()) {
    throw new Error("provider is required");
  }
  if (typeof accountLabel !== "string" || !accountLabel.trim()) {
    throw new Error("account_label is required");
  }
  const credentialRefs = parseProviderCredentialRefs(formData);
  await client.createProviderAccount({
    project_id: getProjectId(formData),
    provider: provider.trim(),
    account_label: accountLabel.trim(),
    auth_method: getOptionalString(formData, "auth_method"),
    credential_ref:
      Object.keys(credentialRefs).length === 0
        ? getOptionalString(formData, "credential_ref")
        : undefined,
    credential_refs:
      Object.keys(credentialRefs).length > 0 ? credentialRefs : undefined,
    scopes: parseLines(formData.get("scopes")),
    read_only: parseProviderOpsCheckbox(formData, "read_only", true),
    deploy_capable: parseProviderOpsCheckbox(formData, "deploy_capable", false),
    billing_readable: parseProviderOpsCheckbox(
      formData,
      "billing_readable",
      false,
    ),
    destructive_actions_disabled: parseProviderOpsCheckbox(
      formData,
      "destructive_actions_disabled",
      true,
    ),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

function getProviderAccountId(formData: FormData): string {
  const providerAccountId = formData.get("provider_account_id");
  if (typeof providerAccountId !== "string" || !providerAccountId.trim()) {
    throw new Error("provider_account_id is required");
  }
  return providerAccountId.trim();
}

export async function testProviderAccountAction(formData: FormData): Promise<void> {
  const providerAccountId = getProviderAccountId(formData);
  await client.testProviderAccount(
    providerAccountId,
    getProjectId(formData),
    getActor(formData),
  );
  revalidatePath("/");
}

export async function updateProviderAccountAction(
  formData: FormData,
): Promise<void> {
  const providerAccountId = getProviderAccountId(formData);
  await client.updateProviderAccount(providerAccountId, {
    project_id: getProjectId(formData),
    account_label: getOptionalString(formData, "account_label"),
    owner_actor: getOptionalString(formData, "owner_actor"),
    scopes: parseLines(formData.get("scopes")),
    read_only: parseProviderOpsCheckbox(formData, "read_only", true),
    deploy_capable: parseProviderOpsCheckbox(
      formData,
      "deploy_capable",
      false,
    ),
    billing_readable: parseProviderOpsCheckbox(
      formData,
      "billing_readable",
      false,
    ),
    destructive_actions_disabled: parseProviderOpsCheckbox(
      formData,
      "destructive_actions_disabled",
      true,
    ),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function enableProviderAccountAction(
  formData: FormData,
): Promise<void> {
  await client.enableProviderAccount(getProviderAccountId(formData), {
    project_id: getProjectId(formData),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function disableProviderAccountAction(
  formData: FormData,
): Promise<void> {
  await client.disableProviderAccount(getProviderAccountId(formData), {
    project_id: getProjectId(formData),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function rotateProviderCredentialsAction(
  formData: FormData,
): Promise<void> {
  const credentialRefs = parseProviderCredentialRefs(formData);
  if (Object.keys(credentialRefs).length === 0) {
    throw new Error("At least one replacement SecretRef is required.");
  }
  const overlapMinutes = Number(formData.get("overlap_minutes") ?? 30);
  await client.rotateProviderCredentials(getProviderAccountId(formData), {
    project_id: getProjectId(formData),
    credential_refs: credentialRefs,
    overlap_minutes: Number.isFinite(overlapMinutes) ? overlapMinutes : 30,
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function revokeProviderAccountAction(
  formData: FormData,
): Promise<void> {
  const accountLabel = getOptionalString(formData, "account_label");
  const confirmation = getOptionalString(formData, "confirmation");
  if (!accountLabel || confirmation !== accountLabel) {
    throw new Error("Type the account label exactly to confirm permanent revocation.");
  }
  await client.revokeProviderAccount(getProviderAccountId(formData), {
    project_id: getProjectId(formData),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function syncProviderResourcesAction(
  formData: FormData,
): Promise<void> {
  await client.syncExternalResources({
    project_id: getProjectId(formData),
    provider_account_id: getOptionalString(formData, "provider_account_id"),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function runResourceJanitorAction(formData: FormData): Promise<void> {
  const maxCandidates = Number(formData.get("max_candidates") ?? 10);
  await client.runResourceJanitor({
    project_id: getProjectId(formData),
    provider_account_id: getOptionalString(formData, "provider_account_id"),
    max_candidates: Number.isFinite(maxCandidates) ? maxCandidates : 10,
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function upsertWorkerPoolAction(formData: FormData): Promise<void> {
  const poolKey = formData.get("pool_key");
  const kind = formData.get("kind");
  if (typeof poolKey !== "string" || !poolKey.trim()) {
    throw new Error("pool_key is required");
  }
  if (typeof kind !== "string" || !kind.trim()) {
    throw new Error("kind is required");
  }
  const currentWorkers = Number(formData.get("current_workers") ?? 0);
  const desiredWorkers = Number(formData.get("desired_workers") ?? 0);
  const minWorkers = Number(formData.get("min_workers") ?? 0);
  const maxWorkers = Number(formData.get("max_workers") ?? 1);
  const maxQueueDepthPerWorker = Number(
    formData.get("max_queue_depth_per_worker") ?? 5,
  );
  const costPerWorkerHourUsdMicros = Number(
    formData.get("cost_per_worker_hour_usd_micros") ?? 0,
  );
  await client.upsertWorkerPool({
    project_id: getProjectId(formData),
    pool_key: poolKey.trim(),
    kind: kind.trim(),
    provider_account_id: getOptionalString(formData, "provider_account_id"),
    current_workers: Number.isFinite(currentWorkers) ? currentWorkers : 0,
    desired_workers: Number.isFinite(desiredWorkers) ? desiredWorkers : 0,
    min_workers: Number.isFinite(minWorkers) ? minWorkers : 0,
    max_workers: Number.isFinite(maxWorkers) ? maxWorkers : 1,
    max_queue_depth_per_worker: Number.isFinite(maxQueueDepthPerWorker)
      ? maxQueueDepthPerWorker
      : 5,
    cost_per_worker_hour_usd_micros: Number.isFinite(
      costPerWorkerHourUsdMicros,
    )
      ? costPerWorkerHourUsdMicros
      : 0,
    capabilities: parseLines(formData.get("capabilities")),
    metadata: parseOptionalJsonObject(formData.get("metadata_json")),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function runAutoscalerPlanAction(formData: FormData): Promise<void> {
  await client.runAutoscalerPlan({
    project_id: getProjectId(formData),
    worker_pool_id: getOptionalString(formData, "worker_pool_id"),
    cloud_spillover_enabled: parseProviderOpsCheckbox(
      formData,
      "cloud_spillover_enabled",
      false,
    ),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function createChangeSetAction(formData: FormData): Promise<void> {
  const title = formData.get("title");
  const summary = formData.get("summary");
  if (typeof title !== "string" || !title.trim()) {
    throw new Error("title is required");
  }
  if (typeof summary !== "string" || !summary.trim()) {
    throw new Error("summary is required");
  }
  const expectedCostUsdMicros = Number(
    formData.get("expected_cost_usd_micros") ?? 0,
  );
  const riskTier = Number(formData.get("risk_tier") ?? 2);
  const operations = parseOptionalJsonArray(
    formData.get("operations_json"),
    "operations_json",
  );
  if (operations.length === 0) {
    throw new Error("operations_json must contain at least one operation");
  }
  await client.createChangeSet({
    project_id: getProjectId(formData),
    provider_account_id: getOptionalString(formData, "provider_account_id"),
    title: title.trim(),
    summary: summary.trim(),
    operations: operations.map((operation) => ({
      skill_id: String(operation.skill_id ?? ""),
      action: String(operation.action ?? ""),
      resource_type: String(operation.resource_type ?? ""),
      resource_ref:
        typeof operation.resource_ref === "string"
          ? operation.resource_ref
          : undefined,
      desired_state:
        operation.desired_state &&
        !Array.isArray(operation.desired_state) &&
        typeof operation.desired_state === "object"
          ? (operation.desired_state as Record<string, unknown>)
          : {},
      risk_tier:
        typeof operation.risk_tier === "number" ? operation.risk_tier : riskTier,
      destructive: Boolean(operation.destructive),
      production_affecting: Boolean(operation.production_affecting),
    })),
    expected_cost_usd_micros: Number.isFinite(expectedCostUsdMicros)
      ? expectedCostUsdMicros
      : 0,
    rollback_plan: parseLines(formData.get("rollback_plan")),
    risk_tier: Number.isFinite(riskTier) ? riskTier : 2,
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function approveChangeSetAction(formData: FormData): Promise<void> {
  const changeSetId = formData.get("changeset_id");
  if (typeof changeSetId !== "string" || !changeSetId.trim()) {
    throw new Error("changeset_id is required");
  }
  await client.approveChangeSet(changeSetId.trim(), { actor: getActor(formData) });
  revalidatePath("/");
}

export async function applyChangeSetAction(formData: FormData): Promise<void> {
  const changeSetId = formData.get("changeset_id");
  if (typeof changeSetId !== "string" || !changeSetId.trim()) {
    throw new Error("changeset_id is required");
  }
  await client.applyChangeSet(changeSetId.trim(), { actor: getActor(formData) });
  revalidatePath("/");
}

export async function createDeploymentTargetAction(
  formData: FormData,
): Promise<void> {
  const appName = formData.get("app_name");
  const repo = formData.get("repo");
  const environment = formData.get("environment");
  const targetKind = formData.get("target_kind");
  const targetRef = formData.get("target_ref");
  if (typeof appName !== "string" || !appName.trim()) {
    throw new Error("app_name is required");
  }
  if (typeof repo !== "string" || !repo.trim()) {
    throw new Error("repo is required");
  }
  if (typeof environment !== "string" || !environment.trim()) {
    throw new Error("environment is required");
  }
  if (typeof targetKind !== "string" || !targetKind.trim()) {
    throw new Error("target_kind is required");
  }
  if (typeof targetRef !== "string" || !targetRef.trim()) {
    throw new Error("target_ref is required");
  }
  await client.createDeploymentTarget({
    project_id: getProjectId(formData),
    app_name: appName.trim(),
    repo: repo.trim(),
    environment: environment.trim(),
    provider_account_id: getOptionalString(formData, "provider_account_id"),
    target_kind: targetKind.trim(),
    target_ref: targetRef.trim(),
    domains: parseLines(formData.get("domains")),
    secret_refs: parseLines(formData.get("secret_refs")),
    required_resources: parseOptionalJsonArray(
      formData.get("required_resources_json"),
      "required_resources_json",
    ),
    health_checks: parseOptionalJsonArray(
      formData.get("health_checks_json"),
      "health_checks_json",
    ),
    rollback_points: parseOptionalJsonArray(
      formData.get("rollback_points_json"),
      "rollback_points_json",
    ),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function createDeploymentRunAction(formData: FormData): Promise<void> {
  const deploymentTargetId = formData.get("deployment_target_id");
  if (typeof deploymentTargetId !== "string" || !deploymentTargetId.trim()) {
    throw new Error("deployment_target_id is required");
  }
  const expectedCostUsdMicros = Number(
    formData.get("expected_cost_usd_micros") ?? 0,
  );
  await client.createDeploymentRun({
    project_id: getProjectId(formData),
    deployment_target_id: deploymentTargetId.trim(),
    commit_ref: getOptionalString(formData, "commit_ref"),
    release_packet: parseOptionalJsonObject(formData.get("release_packet_json")),
    expected_cost_usd_micros: Number.isFinite(expectedCostUsdMicros)
      ? expectedCostUsdMicros
      : 0,
    reservation_id: getOptionalString(formData, "reservation_id"),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function upsertDeploymentEnvironmentPolicyAction(
  formData: FormData,
): Promise<void> {
  const environment = formData.get("environment");
  const runtimeMode = formData.get("runtime_mode");
  if (typeof environment !== "string" || !environment.trim()) {
    throw new Error("environment is required");
  }
  if (typeof runtimeMode !== "string" || !runtimeMode.trim()) {
    throw new Error("runtime_mode is required");
  }
  await client.upsertDeploymentEnvironmentPolicy({
    project_id: getProjectId(formData),
    environment: environment.trim(),
    runtime_mode: runtimeMode.trim(),
    allowed_target_kinds: parseLines(formData.get("allowed_target_kinds")),
    requires_change_set: formData.get("requires_change_set") === "on",
    requires_budget_reservation:
      formData.get("requires_budget_reservation") === "on",
    requires_approval: formData.get("requires_approval") === "on",
    requires_health_check: formData.get("requires_health_check") === "on",
    requires_rollback_plan: formData.get("requires_rollback_plan") === "on",
    rollout_notes: getOptionalString(formData, "rollout_notes"),
    metadata: parseOptionalJsonObject(formData.get("metadata_json")),
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function runDeploymentHealthCheckAction(
  formData: FormData,
): Promise<void> {
  const deploymentRunId = formData.get("deployment_run_id");
  if (typeof deploymentRunId !== "string" || !deploymentRunId.trim()) {
    throw new Error("deployment_run_id is required");
  }
  await client.runDeploymentHealthCheck(deploymentRunId.trim(), {
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function createImprovementProgramAction(
  formData: FormData,
): Promise<void> {
  const name = String(formData.get("name") ?? "").trim();
  const objective = String(formData.get("objective") ?? "").trim();
  const baselineEvaluationId = String(
    formData.get("baseline_evaluation_id") ?? "",
  ).trim();
  if (!name) {
    throw new Error("name is required");
  }
  if (!objective) {
    throw new Error("objective is required");
  }
  if (!baselineEvaluationId) {
    throw new Error("baseline_evaluation_id is required");
  }

  const response = await fetch(`${API_URL}/v1/improvement-programs`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({
      project_id: getProjectId(formData),
      name,
      objective,
      baseline_evaluation_id: baselineEvaluationId,
      actor: getActor(formData),
    }),
  });
  if (!response.ok) {
    let detail = "";
    try {
      detail = String((await response.json()).detail ?? "");
    } catch {
      detail = "";
    }
    throw new Error(
      detail || `Improvement program create failed (${response.status})`,
    );
  }
  revalidatePath("/");
}

export async function submitSelfImprovementProposalAction(
  formData: FormData,
): Promise<void> {
  const projectId = getProjectId(formData);
  const title = formData.get("title");
  const summary = formData.get("summary");
  const changeRef = formData.get("change_ref");
  const diffSummary = formData.get("diff_summary");

  if (typeof title !== "string" || !title.trim()) {
    throw new Error("title is required");
  }
  if (typeof summary !== "string" || !summary.trim()) {
    throw new Error("summary is required");
  }
  if (typeof changeRef !== "string" || !changeRef.trim()) {
    throw new Error("change_ref is required");
  }
  if (typeof diffSummary !== "string" || !diffSummary.trim()) {
    throw new Error("diff_summary is required");
  }

  await client.createSelfImprovementProposal({
    project_id: projectId,
    title: title.trim(),
    summary: summary.trim(),
    change_ref: changeRef.trim(),
    diff_summary: diffSummary.trim(),
    expected_impact: parseLines(formData.get("expected_impact")),
    test_plan: parseLines(formData.get("test_plan")),
    canary_scope: parseLines(formData.get("canary_scope")),
    rollback_plan: parseLines(formData.get("rollback_plan")),
    success_metrics: parseLines(formData.get("success_metrics")),
    actor: getActor(formData),
    risk_tier: 2,
  });
  revalidatePath("/");
}

export async function recordSelfImprovementCanaryAction(
  formData: FormData,
): Promise<void> {
  const proposalId = formData.get("proposal_id");
  const outcome = formData.get("outcome");
  const summary = formData.get("summary");
  if (typeof proposalId !== "string" || !proposalId) {
    throw new Error("proposal_id is required");
  }
  if (outcome !== "succeeded" && outcome !== "failed") {
    throw new Error("outcome must be succeeded or failed");
  }
  if (typeof summary !== "string" || !summary.trim()) {
    throw new Error("summary is required");
  }
  await client.recordSelfImprovementCanary(proposalId, {
    actor: getActor(formData),
    outcome,
    summary: summary.trim(),
    metrics: parseRequiredMetrics(formData.get("metrics_json")),
    evidence_refs: parseLines(formData.get("evidence_refs")),
  });
  revalidatePath("/");
}

export async function recordSelfImprovementRolloutAction(
  formData: FormData,
): Promise<void> {
  const proposalId = formData.get("proposal_id");
  const summary = formData.get("summary");
  if (typeof proposalId !== "string" || !proposalId) {
    throw new Error("proposal_id is required");
  }
  if (typeof summary !== "string" || !summary.trim()) {
    throw new Error("summary is required");
  }
  await client.recordSelfImprovementRollout(proposalId, {
    actor: getActor(formData),
    summary: summary.trim(),
    external_ref:
      typeof formData.get("external_ref") === "string" &&
      String(formData.get("external_ref")).trim()
        ? String(formData.get("external_ref")).trim()
        : undefined,
    metrics: parseRequiredMetrics(formData.get("metrics_json")),
    evidence_refs: parseLines(formData.get("evidence_refs")),
  });
  revalidatePath("/");
}

export async function rollbackSelfImprovementProposalAction(
  formData: FormData,
): Promise<void> {
  const proposalId = formData.get("proposal_id");
  const reason = formData.get("reason");
  if (typeof proposalId !== "string" || !proposalId) {
    throw new Error("proposal_id is required");
  }
  if (typeof reason !== "string" || !reason.trim()) {
    throw new Error("reason is required");
  }
  await client.rollbackSelfImprovementProposal(proposalId, {
    actor: getActor(formData),
    reason: reason.trim(),
    external_ref:
      typeof formData.get("external_ref") === "string" &&
      String(formData.get("external_ref")).trim()
        ? String(formData.get("external_ref")).trim()
        : undefined,
    metrics: parseRequiredMetrics(formData.get("metrics_json")),
    evidence_refs: parseLines(formData.get("evidence_refs")),
  });
  revalidatePath("/");
}

// ---- Capabilities: MCP server management ----

export async function registerMcpServerAction(formData: FormData): Promise<void> {
  const projectId = getProjectId(formData);
  const name = String(formData.get("name") ?? "").trim();
  const url = String(formData.get("url") ?? "").trim();
  if (!name || !url) {
    return;
  }
  const riskRaw = String(formData.get("risk_tier") ?? "1").trim();
  const risk = Number.isFinite(Number(riskRaw)) ? Number(riskRaw) : 1;
  const boundary = String(formData.get("data_boundary") ?? "internal").trim() as DataBoundary;
  const description = String(formData.get("description") ?? "").trim();
  await client.registerMcpServer({
    project_id: projectId,
    name,
    url,
    risk_tier: risk,
    data_boundary: boundary || "internal",
    description: description || undefined,
    actor: getActor(formData),
  });
  revalidatePath("/");
}

export async function setMcpServerEnabledAction(formData: FormData): Promise<void> {
  const projectId = getProjectId(formData);
  const serverId = String(formData.get("server_id") ?? "").trim();
  const enabled = String(formData.get("enabled") ?? "").trim() === "true";
  if (!serverId) {
    return;
  }
  await client.setMcpServerEnabled(serverId, projectId, enabled);
  revalidatePath("/");
}

export async function deleteMcpServerAction(formData: FormData): Promise<void> {
  const projectId = getProjectId(formData);
  const serverId = String(formData.get("server_id") ?? "").trim();
  if (!serverId) {
    return;
  }
  await client.deleteMcpServer(serverId, projectId);
  revalidatePath("/");
}

// ---- Convene a multi-agent PRD meeting ----

export async function conveneMeetingAction(formData: FormData): Promise<void> {
  const projectId = getProjectId(formData);
  const directive = String(formData.get("directive") ?? "").trim();
  const url = String(formData.get("url") ?? "").trim();
  const taskId = String(formData.get("task_id") ?? "").trim();
  const meetingType = String(formData.get("meeting_type") ?? "prd_panel").trim() || "prd_panel";
  const roles = formData.getAll("roles").map((r) => String(r)).filter(Boolean);
  if (!directive) {
    return;
  }
  if (meetingType === "standup") {
    await fetch(`${API_URL}/v1/meetings/standup`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        project_id: projectId,
        actor: getActor(formData),
        directive,
        url: url || undefined,
        roles: roles.length ? roles : undefined,
        simulated: true,
      }),
    });
    revalidatePath("/");
    return;
  }
  await client.conveneMeeting({
    project_id: projectId,
    directive,
    url: url || undefined,
    roles: roles.length ? roles : undefined,
    task_id: taskId || undefined,
    actor: getActor(formData),
    constraints: { meeting_type: "prd_panel" },
  });
  revalidatePath("/");
}

export async function runSimulatedMeetingAction(formData: FormData): Promise<void> {
  const projectId = getProjectId(formData);
  const meetingId = String(formData.get("meeting_id") ?? "").trim();
  const directive = String(formData.get("directive") ?? "").trim();
  const path = meetingId
    ? `/v1/meetings/${encodeURIComponent(meetingId)}/simulated-run`
    : "/v1/meetings/simulated-run";
  await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({
      project_id: projectId,
      actor: getActor(formData),
      directive: directive || "Community simulated panel fixture",
      simulated: true,
      meeting_type: String(formData.get("meeting_type") ?? "prd_panel").trim() || "prd_panel",
    }),
  });
  revalidatePath("/");
}

export async function loadMeetingEvidenceAction(
  meetingId: string,
  projectId: string,
): Promise<{
  ok: boolean;
  phase_timeline: Array<{ phase: string; status?: string; artifact_id?: string }>;
  artifact_links: Array<{ kind: string; artifact_id: string }>;
  receipt_id?: string | null;
  simulated?: boolean;
  meeting_type?: string;
  decision_id?: string | null;
}> {
  const p = encodeURIComponent(projectId);
  const id = encodeURIComponent(meetingId);
  const res = await fetch(
    `${API_URL}/v1/meetings/${id}/evidence?project_id=${p}`,
    { headers: authHeaders() },
  );
  if (!res.ok) {
    return { ok: false, phase_timeline: [], artifact_links: [] };
  }
  const data = (await res.json()) as {
    phase_timeline?: Array<{ phase: string; status?: string; artifact_id?: string }>;
    artifact_links?: Array<{ kind: string; artifact_id: string }>;
    receipt_id?: string | null;
    simulated?: boolean;
    meeting_type?: string;
    decision_id?: string | null;
  };
  return {
    ok: true,
    phase_timeline: data.phase_timeline ?? [],
    artifact_links: data.artifact_links ?? [],
    receipt_id: data.receipt_id,
    simulated: data.simulated,
    meeting_type: data.meeting_type,
    decision_id: data.decision_id,
  };
}

export async function seedAssuranceDemoAction(formData: FormData): Promise<void> {
  const projectId = getProjectId(formData);
  await fetch(`${API_URL}/v1/assurance/seed-demo`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({
      project_id: projectId,
      actor: getActor(formData),
    }),
  });
  revalidatePath("/");
}

// ---- Start a coding-agent task (clone -> edit -> test -> gated PR) ----

function splitLines(value: string): string[] {
  return value
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);
}

export async function createCodeTaskAction(formData: FormData): Promise<void> {
  const projectId = getProjectId(formData);
  const title = String(formData.get("title") ?? "").trim();
  const instructions = String(formData.get("instructions") ?? "").trim();
  if (!title || !instructions) {
    return;
  }
  await client.createCodeTask({
    project_id: projectId,
    title,
    instructions,
    acceptance_criteria: splitLines(String(formData.get("acceptance_criteria") ?? "")),
    test_commands: splitLines(String(formData.get("test_commands") ?? "")),
    owner: String(formData.get("owner") ?? "").trim() || undefined,
    repo: String(formData.get("repo") ?? "").trim() || undefined,
    base_branch: String(formData.get("base_branch") ?? "").trim() || undefined,
  });
  revalidatePath("/");
}

/**
 * Run an existing task again, as a NEW governed run.
 *
 * The QA benchmark cards have a Launch button; task cards had none, so re-running work meant
 * retyping its title, instructions, acceptance criteria and test commands into the New Code
 * Task form — copying, by hand, data BOSS already holds. Same governed path as any other code
 * task: it plans, tests, reviews and opens a gated PR. Nothing is re-run in place; the original
 * run keeps its history, exactly as redeploy does for deployments.
 */
export async function relaunchCodeTaskAction(formData: FormData): Promise<{ run_id?: string; error?: string }> {
  const projectId = getProjectId(formData);
  const title = String(formData.get("title") ?? "").trim();
  const instructions = String(formData.get("instructions") ?? "").trim();
  // Instructions are what the agent actually works from. Without them a "relaunch" would be a
  // differently-named empty task, so refuse rather than start something that cannot succeed.
  if (!title || !instructions) return { error: "this task has no instructions to re-run" };
  const created = await client.createCodeTask({
    project_id: projectId,
    title,
    instructions,
    acceptance_criteria: splitLines(String(formData.get("acceptance_criteria") ?? "")),
    test_commands: splitLines(String(formData.get("test_commands") ?? "")),
    owner: String(formData.get("owner") ?? "").trim() || undefined,
    repo: String(formData.get("repo") ?? "").trim() || undefined,
    base_branch: String(formData.get("base_branch") ?? "").trim() || undefined,
    // Carry the ORIGINAL run's cost/rigor. Without these a relaunch silently adopted the
    // project default: the currency build ran `lean`, and re-running it produced something
    // else entirely while the button claimed "Strict". A re-run that changes the rules is
    // not a re-run.
    profile: String(formData.get("profile") ?? "").trim() || undefined,
    risk_tier: formData.get("risk_tier") ? Number(formData.get("risk_tier")) : undefined,
  });
  // Deliberately NO revalidatePath here.
  //
  // Revalidating replaces the client tree with a fresh server render, which remounts the
  // panel and destroys the state the caller is about to set — so "Launched ✓" was written to
  // a component that no longer existed, and the button snapped back to its original label
  // with the run already created. The panel re-reads the list itself via listCodeTasksAction,
  // which updates the rows without discarding what the operator just did.
  // The id is returned so the button can name what it started. "Started — it is at the top of
  // this list" was a guess about a list this action cannot see, and when the list did not
  // move the operator pressed the button again.
  return { run_id: created?.run_id };
}

/** The code-task list, fetched on demand.
 *
 * The panel is server-rendered, and `router.refresh()` re-renders on the server while the
 * client keeps the old rows — the same defect that left deleted deployment targets on screen.
 * A panel that owns its data can show a run it just started. */
/** The panel re-reads the list through here after a launch.
 *
 *  This asked for 40 while the server render asked for 200. Roughly 36 of any recent 40 are
 *  BOSS's own "Captain: QUALITY IMPROVE PASS" runs, so the page rendered 35 cards and then
 *  replaced them with almost none the moment the panel refreshed — cards that "display and
 *  then vanish". Both paths must ask for the same thing; a list that changes depending on
 *  which fetch produced it is not a list.
 */
export async function listCodeTasksAction(projectId?: string): Promise<any> {
  const pid = projectId || PROJECT_ID;
  try {
    const res = await fetch(`${API_URL}/v1/code-tasks?project_id=${encodeURIComponent(pid)}&limit=200`,
                            { headers: authHeaders(), cache: "no-store" });
    if (!res.ok) return { error: `code-tasks ${res.status}` };
    return await res.json();
  } catch (e) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
}

export async function getQaBenchmarkReportAction(runId: string) {
  try {
    return await client.getQaBenchmarkReport(runId);
  } catch {
    return null;
  }
}

export async function startQaBenchmarkAppAction(
  formData: FormData,
): Promise<{
  run_id?: string;
  kind?: "meeting" | "code" | "existing";
  pullRequestUrl?: string;
  error?: string;
}> {
  const projectId = getProjectId(formData);
  const benchmarkId = String(formData.get("benchmark_id") ?? "").trim();
  if (!benchmarkId) return { error: "no benchmark named" };
  try {
    const started = await client.startQaBenchmarkApp(benchmarkId, {
      project_id: projectId,
      actor: getActor(formData),
      owner: String(formData.get("owner") ?? "").trim() || undefined,
      repo: String(formData.get("repo") ?? "").trim() || undefined,
      base_branch: String(formData.get("base_branch") ?? "").trim() || undefined,
      // Only ever set by the explicit "Build it again" control. Without it the API answers
      // with the existing build instead of making a second one.
      force: String(formData.get("force") ?? "") === "true" || undefined,
    });
    // Deliberately NO revalidatePath, and the id is returned.
    //
    // Revalidating replaces the client tree while this promise is still resolving, so the
    // component that awaited it is remounted and every state update afterwards lands on an
    // instance nobody renders. The run WAS created on every click; the button simply reverted
    // to "Launch ApproveIt" with nothing on screen having changed. That is the same defect
    // fixed for the relaunch button — and left in its sibling, which is how it reached an
    // operator who had been waiting hours to press it.
    // Launch now convenes a PRD panel first, so the id can be a MEETING run. Saying which
    // kind matters: handing an mrun_ id to the code-task viewer opens nothing, and "View run
    // ->" that opens nothing is the failure mode this button has already had twice.
    const out = started as {
      run_id?: string; status?: string; pull_request_url?: string; title?: string;
    };
    if (out?.status === "already_built") {
      // Not an error and not a launch: the work exists. The card offers it instead of
      // quietly spending twenty minutes and several dollars making a second copy.
      return {
        run_id: out.run_id,
        kind: "existing",
        pullRequestUrl: out.pull_request_url,
      };
    }
    return {
      run_id: out?.run_id,
      kind: out?.status === "meeting_convened" ? "meeting" : "code",
    };
  } catch (e) {
    // A silent throw here is indistinguishable from a dead button. Say what happened.
    return { error: e instanceof Error ? e.message : String(e) };
  }
}

// Per-stage model-call activity for a code task (the internal detail behind each phase).
// Returns [] on any failure so the activity panel degrades gracefully in the UI.

/**
 * Task Shepherd console data, fetched SERVER-side.
 *
 * The first cut fetched from the browser and rendered nothing: the API sends no
 * CORS headers, so a cross-origin call from localhost:3001 to the api on :8000 is
 * blocked before it is made. Every other panel in this console goes through a
 * server action for exactly this reason — worth matching rather than adding CORS
 * to the API just for one panel.
 */
export async function getDeliveryPipelineAction(projectId?: string): Promise<any> {
  const pid = projectId || PROJECT_ID;
  try {
    const res = await fetch(
      `${API_URL}/v1/delivery/pipeline?project_id=${encodeURIComponent(pid)}&limit=25`,
      { headers: authHeaders(), cache: "no-store" },
    );
    if (!res.ok) return { error: `pipeline ${res.status}` };
    return await res.json();
  } catch (e) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
}

// --- Delivery management: onboarding, secrets, triggers ---------------------
// Server actions rather than browser fetches: the API has no CORS headers, and routing these
// through the server also keeps the API key out of the browser.

/** The reason a delivery call failed, as a sentence.
 *
 * FastAPI's `detail` is sometimes a string and sometimes a structure — teardown refuses with
 * `{ok, removed, refused:[...]}`. The client only handled the string case, so a refusal that
 * said "ssh key unusable: No SSH key configured" reached the operator as the text "409". The
 * reason existed at every layer and was discarded at the last one.
 */
/** Who the console acts AS.
 *
 * These calls used the literal "operator", which is not a member of any org — it read like a
 * role and was in fact a made-up name. It went unnoticed while the delivery routes had no
 * authorization; the moment redeploy started checking membership, every button 403'd with
 * "Actor 'operator' is not an active member". The console already has an identity for this —
 * `executive_console`, seeded as an active member and used by every other panel — so the fix
 * is to stop inventing a second one. Override with BOSS_CONSOLE_ACTOR where the seeded
 * identity differs.
 */
const CONSOLE_ACTOR = process.env.BOSS_CONSOLE_ACTOR || "executive_console";

function failureText(detail: unknown, status: number): string {
  if (typeof detail === "string" && detail.trim()) return detail;
  if (detail && typeof detail === "object") {
    const d = detail as Record<string, unknown>;
    for (const key of ["refused", "problems", "errors"]) {
      const list = d[key];
      if (Array.isArray(list) && list.length) return list.map(String).join("; ");
    }
    if (typeof d.reason === "string") return d.reason;
    // Pydantic validation errors: [{loc, msg}, ...]
    if (Array.isArray(detail)) {
      return (detail as { loc?: unknown[]; msg?: string }[])
        .map((e) => `${(e.loc || []).slice(-1)}: ${e.msg}`).join("; ");
    }
    const text = JSON.stringify(detail);
    if (text && text !== "{}") return text.slice(0, 300);
  }
  return `${status}`;
}

async function post(path: string, body: unknown): Promise<any> {
  try {
    const res = await fetch(`${API_URL}${path}`, {
      method: "POST",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
    });
    const out = await res.json().catch(() => ({}));
    if (!res.ok) return { error: failureText(out?.detail, res.status) };
    return out;
  } catch (e) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
}

async function get(path: string): Promise<any> {
  try {
    const res = await fetch(`${API_URL}${path}`, { headers: authHeaders(), cache: "no-store" });
    if (!res.ok) return { error: `${res.status}` };
    return await res.json();
  } catch (e) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
}

export async function listSecretsAction(projectId?: string): Promise<any> {
  return get(`/v1/secrets?project_id=${encodeURIComponent(projectId || PROJECT_ID)}`);
}

/** Upload a secret. The value is written through to a backend; nothing returns it afterwards. */
export async function putSecretAction(input: {
  name: string; value: string; secret_kind?: string; backend?: string; scope_id?: string;
}): Promise<any> {
  return post("/v1/secrets", { project_id: PROJECT_ID, actor: CONSOLE_ACTOR, ...input });
}

/** Generate a keypair. Only the PUBLIC half comes back — the private half never leaves the backend. */
export async function generateKeypairAction(input: {
  name: string; algorithm?: string; backend?: string; scope_id?: string; comment?: string;
}): Promise<any> {
  return post("/v1/secrets/keypair", { project_id: PROJECT_ID, actor: CONSOLE_ACTOR, ...input });
}

export async function verifySecretAction(secretRefId: string): Promise<any> {
  return post(`/v1/secrets/${encodeURIComponent(secretRefId)}/verify`, { actor: CONSOLE_ACTOR });
}

/** Delete a retired target and its history outright, so the name can be reused. */
export async function purgeTargetAction(targetId: string, confirm: string): Promise<any> {
  return post(`/v1/deployment-targets/${encodeURIComponent(targetId)}/purge`,
              { confirm, actor: CONSOLE_ACTOR });
}

/** Bring a retired target back. Its credential, port, domain and health check were kept, so
 *  this is a status change rather than a second trip through onboarding. */
/** Change a target in place. Fields not named are left alone — see merge_target_resources. */
export async function updateTargetAction(targetId: string,
                                         patch: Record<string, unknown>): Promise<any> {
  try {
    const res = await fetch(`${API_URL}/v1/deployment-targets/${encodeURIComponent(targetId)}`, {
      method: "PATCH",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify(patch),
      cache: "no-store",
    });
    const out = await res.json().catch(() => ({}));
    if (!res.ok) return { error: failureText(out?.detail, res.status) };
    return out;
  } catch (e) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
}

export async function reactivateTargetAction(targetId: string): Promise<any> {
  try {
    const res = await fetch(`${API_URL}/v1/deployment-targets/${encodeURIComponent(targetId)}`, {
      method: "PATCH",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({ status: "active" }),
      cache: "no-store",
    });
    const out = await res.json().catch(() => ({}));
    if (!res.ok) return { error: failureText(out?.detail, res.status) };
    return out;
  } catch (e) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
}

/** Live progress of one deployment: steps, timings, logs. Polled by the console. */
export async function getDeploymentAction(runId: string): Promise<any> {
  return get(`/v1/delivery/deployments/${encodeURIComponent(runId)}`);
}

/** Recent deployments — actual deployments, not every code task that opened a PR. */
export async function listDeploymentsAction(limit = 10): Promise<any> {
  return get(`/v1/delivery/deployments?project_id=${encodeURIComponent(PROJECT_ID)}&limit=${limit}`);
}

/** Runnable apps in a repo — so `app entry` is picked, not guessed. */
export async function listRepoAppsAction(owner: string, repo: string, ref = ""): Promise<any> {
  return get(`/v1/delivery/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/apps`
             + (ref ? `?ref=${encodeURIComponent(ref)}` : ""));
}

/** Repositories BOSS has built in or deployed from. */
export async function listReposAction(): Promise<any> {
  return get(`/v1/delivery/repos?project_id=${encodeURIComponent(PROJECT_ID)}`);
}

/** Branches available for a repo, default first, with what BOSS already shipped from it. */
export async function listRefsAction(owner: string, repo: string): Promise<any> {
  return get(`/v1/delivery/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/refs`);
}

/** What BOSS will clone unless the target says otherwise — owner, repo, and the full URL. */
export async function getDeliveryDefaultsAction(): Promise<any> {
  return get("/v1/delivery/defaults");
}

export async function createTargetAction(input: Record<string, unknown>): Promise<any> {
  return post("/v1/deployment-targets", { project_id: PROJECT_ID, actor: CONSOLE_ACTOR, ...input });
}

export async function bindSecretAction(targetId: string, name: string,
                                       secretRefId: string): Promise<any> {
  return post(`/v1/deployment-targets/${encodeURIComponent(targetId)}/secrets`,
              { project_id: PROJECT_ID, name, secret_ref_id: secretRefId, actor: CONSOLE_ACTOR });
}

/** Have BOSS prepare the host: deploy directory, docker check, and a free port. */
export async function bootstrapTargetAction(targetId: string, applyPort = true): Promise<any> {
  return post(`/v1/deployment-targets/${encodeURIComponent(targetId)}/bootstrap`,
              { apply_port: applyPort });
}

export async function preflightTargetAction(targetId: string): Promise<any> {
  return get(`/v1/delivery/targets/${encodeURIComponent(targetId)}/preflight`
             + `?project_id=${encodeURIComponent(PROJECT_ID)}`);
}

export async function setAutoDeployAction(targetId: string,
                                          input: { auto_deploy?: boolean; paused?: boolean }): Promise<any> {
  return post(`/v1/deployment-targets/${encodeURIComponent(targetId)}/auto-deploy`, input);
}

/** Deploy a target at a chosen ref without waiting for a merge. */
export async function deployTargetAction(targetId: string, ref?: string,
                                         planOnly = false): Promise<any> {
  return post("/v1/delivery/deploy",
              { target_id: targetId, ref: ref || undefined, plan_only: planOnly,
                actor: CONSOLE_ACTOR });
}

/** Remove a deployed app. `stop_only` is reversible; destroy needs the app name typed. */
export async function teardownTargetAction(targetId: string, input: {
  stop_only?: boolean; include_data?: boolean; confirm?: string;
}): Promise<any> {
  return post(`/v1/deployment-targets/${encodeURIComponent(targetId)}/teardown`,
              { actor: CONSOLE_ACTOR, ...input });
}

/** What the observability stack monitors, and whether it is up. */
export async function getObservabilityAction(hostTargetId?: string): Promise<any> {
  const q = `project_id=${encodeURIComponent(PROJECT_ID)}`
    + (hostTargetId ? `&host_target_id=${encodeURIComponent(hostTargetId)}` : "");
  return get(`/v1/observability/stack?${q}`);
}

/** Render and deploy the collector/Prometheus/Grafana stack from the live target registry. */
export async function deployObservabilityAction(hostTargetId: string): Promise<any> {
  return post("/v1/observability/stack/deploy",
              { project_id: PROJECT_ID, host_target_id: hostTargetId, actor: CONSOLE_ACTOR });
}

/** Health of everything BOSS has deployed — distinct from whether the deploy succeeded. */
export async function getDeploymentHealthAction(projectId?: string): Promise<any> {
  const pid = projectId || PROJECT_ID;
  try {
    const res = await fetch(
      `${API_URL}/v1/delivery/health?project_id=${encodeURIComponent(pid)}`,
      { headers: authHeaders(), cache: "no-store" },
    );
    if (!res.ok) return { error: `health ${res.status}` };
    return await res.json();
  } catch (e) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
}

/** Retry THIS deployment run (same row) — for one that failed or was blocked. */
export async function executeDeploymentAction(deploymentRunId: string): Promise<any> {
  try {
    const res = await fetch(
      `${API_URL}/v1/delivery/deployments/${encodeURIComponent(deploymentRunId)}/execute`,
      {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        // Same console principal as other mutating actions — required by the API.
        body: JSON.stringify({ actor: CONSOLE_ACTOR }),
        cache: "no-store",
      },
    );
    const body = await res.json().catch(() => ({}));
    if (!res.ok) return { error: body?.detail || `execute ${res.status}` };
    return body;
  } catch (e) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
}

/** Ship the same target again as a NEW run, preserving the previous run's history. */
export async function redeployAction(deploymentRunId: string): Promise<any> {
  try {
    const res = await fetch(
      `${API_URL}/v1/delivery/deployments/${encodeURIComponent(deploymentRunId)}/redeploy`,
      {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ actor: CONSOLE_ACTOR }),
        cache: "no-store",
      },
    );
    const body = await res.json().catch(() => ({}));
    if (!res.ok) return { error: body?.detail || `redeploy ${res.status}` };
    return body;
  } catch (e) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
}

export async function getShepherdOverviewAction(projectId?: string): Promise<any> {
  const pid = projectId || PROJECT_ID;
  try {
    const res = await fetch(
      `${API_URL}/v1/shepherd/overview?project_id=${encodeURIComponent(pid)}&limit=200`,
      { headers: authHeaders(), cache: "no-store" },
    );
    if (!res.ok) return { error: `overview ${res.status}` };
    return await res.json();
  } catch (e) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
}

export async function shepherdSweepAction(projectId?: string): Promise<any> {
  const pid = projectId || PROJECT_ID;
  try {
    const res = await fetch(`${API_URL}/v1/shepherd/sweep`, {
      method: "POST",
      headers: { ...authHeaders(), "content-type": "application/json" },
      body: JSON.stringify({ project_id: pid, limit: 20 }),
    });
    revalidatePath("/");
    return await res.json();
  } catch (e) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
}

export async function shepherdAnswerAction(escalationId: string, answer: string): Promise<any> {
  try {
    const res = await fetch(
      `${API_URL}/v1/shepherd/escalations/${encodeURIComponent(escalationId)}/answer`,
      {
        method: "POST",
        headers: { ...authHeaders(), "content-type": "application/json" },
        body: JSON.stringify({ answer, actor: CONSOLE_ACTOR }),
      },
    );
    revalidatePath("/");
    return await res.json();
  } catch (e) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
}

export async function getCodeTaskActivityAction(runId: string): Promise<LlmCallRow[]> {
  try {
    const res = await client.getCodeTaskActivity(runId);
    return Array.isArray(res?.calls) ? res.calls : [];
  } catch {
    return [];
  }
}

export async function startDiscoveryAction(
  _prev: { runId?: string; error?: string } | null,
  formData: FormData,
): Promise<{ runId?: string; error?: string }> {
  const projectId = getProjectId(formData);
  const seed = String(formData.get("seed") ?? "").trim();
  if (!seed) return { error: "Enter an idea / problem / signal to research." };
  try {
    const res = await client.startDiscovery({
      project_id: projectId,
      seed,
      title: String(formData.get("title") ?? "").trim() || undefined,
      context: String(formData.get("context") ?? "").trim() || undefined,
    });
    revalidatePath("/");
    return { runId: res.run_id };
  } catch (e) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
}

export async function promoteOutcomeAction(
  _prev: { taskId?: string; error?: string } | null,
  formData: FormData,
): Promise<{ taskId?: string; error?: string }> {
  const runId = String(formData.get("run_id") ?? "").trim();
  const idx = Number(formData.get("outcome_index") ?? "0");
  if (!runId) return { error: "missing run_id" };
  try {
    const res = await client.promoteDiscoveryOutcome(runId, idx);
    revalidatePath("/");
    return { taskId: res.task_id };
  } catch (e) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
}

export async function deepenDiscoveryAction(
  _prev: { runId?: string; error?: string } | null,
  formData: FormData,
): Promise<{ runId?: string; error?: string }> {
  const runId = String(formData.get("run_id") ?? "").trim();
  if (!runId) return { error: "missing run_id" };
  try {
    const res = await client.deepenDiscovery(runId);
    revalidatePath("/");
    return { runId: res.run_id };
  } catch (e) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
}

// ---- Onboard a new organization (admin) ----

export async function onboardOrgAction(formData: FormData): Promise<void> {
  const name = String(formData.get("name") ?? "").trim();
  if (!name) {
    return;
  }
  await client.onboardOrg(name);
  revalidatePath("/");
}

// ---- Manage meeting role-agents (per-org template) ----

export async function upsertMeetingRoleAction(formData: FormData): Promise<void> {
  const projectId = getProjectId(formData);
  const title = String(formData.get("title") ?? "").trim();
  const lens = String(formData.get("lens") ?? "").trim();
  if (!title || !lens) {
    return;
  }
  await client.upsertMeetingRole({
    project_id: projectId,
    title,
    lens,
    key: String(formData.get("key") ?? "").trim() || undefined,
    is_default: String(formData.get("is_default") ?? "") === "true",
  });
  revalidatePath("/");
}

export async function setMeetingRoleEnabledAction(formData: FormData): Promise<void> {
  const projectId = getProjectId(formData);
  const roleKey = String(formData.get("role_key") ?? "").trim();
  const enabled = String(formData.get("enabled") ?? "") === "true";
  if (!roleKey) {
    return;
  }
  await client.setMeetingRoleEnabled(roleKey, projectId, enabled);
  revalidatePath("/");
}

export async function deleteMeetingRoleAction(formData: FormData): Promise<void> {
  const projectId = getProjectId(formData);
  const roleKey = String(formData.get("role_key") ?? "").trim();
  if (!roleKey) {
    return;
  }
  await client.deleteMeetingRole(roleKey, projectId);
  revalidatePath("/");
}

/**
 * Change an app that already exists.
 *
 * BOSS could always continue work on a shipped branch -- that is how deferred review findings
 * are fixed. It just could not be ASKED to. Without this the only console control was "run this
 * task again", which rebuilds the same app from the same prompt; continuing meant finding the
 * app's git branch by hand and posting base_branch to the API, which is not something an
 * operator can do. Most real software work is changing software that already exists.
 */
export async function followUpCodeTaskAction(
  formData: FormData,
): Promise<{ run_id?: string; error?: string }> {
  const runId = String(formData.get("run_id") ?? "").trim();
  const instructions = String(formData.get("instructions") ?? "").trim();
  if (!runId) return { error: "no run named" };
  if (!instructions) return { error: "describe the change you want" };
  try {
    const out = await client.followUpCodeTask(runId, instructions, getActor(formData));
    // No revalidatePath: it replaces the client tree while this promise resolves, so the
    // component that awaited it is remounted and the result lands on an instance nobody
    // renders -- the "nothing happened" defect this console has had repeatedly.
    return { run_id: out?.run_id };
  } catch (error) {
    return { error: error instanceof Error ? error.message : String(error) };
  }
}

export async function retryCodeTaskAction(formData: FormData): Promise<void> {
  const runId = String(formData.get("run_id") ?? "").trim();
  if (!runId) return;
  await client.retryCodeTask(
    runId,
    String(formData.get("fix_note") ?? "").trim() || undefined,
    getActor(formData),
  );
  revalidatePath("/");
}

/**
 * Operator-triggered retry. Distinct from the feedback flow, which asks an agent to diagnose
 * and PROPOSE a retry: this is the plain "run it again" the operator reaches for when the
 * findings are already on screen and no clarification is needed.
 *
 * The new attempt resumes rather than restarting — it inherits the parent's approved design,
 * works on the same branch (so the code is already in the tree), and skips re-authoring tests
 * when no test-related gate failed. Returns state so the button can name the new run.
 */
export async function retryCodeTaskWithStateAction(
  _prev: { runId?: string; attempt?: number; error?: string } | null,
  formData: FormData,
): Promise<{ runId?: string; attempt?: number; error?: string }> {
  const runId = String(formData.get("run_id") ?? "").trim();
  if (!runId) return { error: "missing run_id" };
  try {
    const res = await client.retryCodeTask(
      runId,
      String(formData.get("fix_note") ?? "").trim() || undefined,
      getActor(formData),
    );
    revalidatePath("/");
    return { runId: res?.run_id, attempt: res?.attempt };
  } catch (e) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
}

export type FeedbackActionResult = {
  thread_id?: string;
  status?: string;
  question?: string;
  proposal?: {
    classification: "scoped_tweak" | "full_rerun";
    fix_note: string;
    profile: string;
    repair_retries: number;
  };
  run_id?: string;
  error?: string;
};

export async function submitCodeTaskFeedbackAction(
  _prev: FeedbackActionResult | null,
  formData: FormData,
): Promise<FeedbackActionResult> {
  const runId = String(formData.get("run_id") ?? "").trim();
  const message = String(formData.get("message") ?? "").trim();
  if (!runId || !message) return { error: "Describe what should change." };
  try {
    const result = await client.submitCodeTaskFeedback(runId, {
      message,
      expected: String(formData.get("expected") ?? "").trim(),
      anchor: String(formData.get("anchor") ?? "").trim(),
      severity: String(formData.get("severity") ?? "normal").trim(),
      selected_element: String(formData.get("selected_element") ?? "").trim(),
    });
    revalidatePath("/");
    return result;
  } catch (e) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
}

export async function confirmCodeTaskFeedbackAction(
  _prev: FeedbackActionResult | null,
  formData: FormData,
): Promise<FeedbackActionResult> {
  const threadId = String(formData.get("thread_id") ?? "").trim();
  if (!threadId) return { error: "Missing feedback thread." };
  try {
    const result = await client.confirmCodeTaskFeedback(threadId, getActor(formData));
    revalidatePath("/");
    return result;
  } catch (e) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
}

// ---- Delivery: the preview gate, the fleet, and promotion ----
// Design: docs/product/DEPLOYMENT_FLEET_AND_APPROVAL_FLOW.md
// No new console page. These hang off the surfaces that already own the work: the preview
// card (approve) and delivery-manage (fleet), because a separate "Approvals" page would be a
// third place to look for the same delivery.

/** What approving this preview will actually do — read BEFORE showing the button. */
export async function previewGateAction(previewId: string): Promise<ActionResult> {
  const id = (previewId ?? "").trim();
  if (!id) return { ok: false, error: "missing preview id" };
  try {
    const r = await fetch(`${API_URL}/v1/previews/${encodeURIComponent(id)}/gate`, {
      headers: authHeaders({}),
      cache: "no-store",
    });
    if (!r.ok) return { ok: false, error: `HTTP ${r.status}` };
    return { ok: true, data: await r.json() };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

/**
 * Approve the preview — the single human gate on the way to live.
 *
 * Does NOT deploy. It approves the run's merge decision, which merges, which fires the one
 * delivery trigger that already existed.
 */
export async function approvePreviewAction(
  _prev: ActionResult | null,
  formData: FormData,
): Promise<ActionResult> {
  const id = String(formData.get("preview_id") ?? "").trim();
  if (!id) return { ok: false, error: "missing preview_id" };
  const res = await postJsonReturn(`/v1/previews/${encodeURIComponent(id)}/approve`, {
    actor: getActor(formData),
    note: String(formData.get("note") ?? "").trim() || undefined,
  });
  revalidatePath("/");
  return res;
}

/** Promote a succeeded staging deployment to production — the second gate. */
export async function promoteDeploymentAction(
  _prev: ActionResult | null,
  formData: FormData,
): Promise<ActionResult> {
  const id = String(formData.get("deployment_run_id") ?? "").trim();
  if (!id) return { ok: false, error: "missing deployment_run_id" };
  const res = await postJsonReturn(`/v1/deployments/${encodeURIComponent(id)}/promote`, {
    actor: getActor(formData),
    note: String(formData.get("note") ?? "").trim() || undefined,
  });
  revalidatePath("/");
  return res;
}

// ---- Fleet: VMs, and the apps riding on them ----

export async function listDeploymentHostsAction(projectId: string): Promise<ActionResult> {
  const p = (projectId ?? "").trim();
  if (!p) return { ok: false, error: "missing project_id" };
  try {
    const r = await fetch(
      `${API_URL}/v1/deployment-hosts?project_id=${encodeURIComponent(p)}`,
      { headers: authHeaders({}), cache: "no-store" },
    );
    if (!r.ok) return { ok: false, error: `HTTP ${r.status}` };
    return { ok: true, data: await r.json() };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

export async function registerDeploymentHostAction(
  _prev: ActionResult | null,
  formData: FormData,
): Promise<ActionResult> {
  const res = await postJsonReturn(`/v1/deployment-hosts`, {
    project_id: String(formData.get("project_id") ?? "").trim(),
    name: String(formData.get("name") ?? "").trim(),
    address: String(formData.get("address") ?? "").trim(),
    ssh_user: String(formData.get("ssh_user") ?? "root").trim() || "root",
    ssh_port: Number(formData.get("ssh_port") ?? 22) || 22,
    // Not cosmetic: an image built for the wrong architecture fails as a confusing runtime
    // crash rather than a deploy error, so the build step reads this.
    arch: String(formData.get("arch") ?? "").trim() || undefined,
    actor: getActor(formData),
  });
  revalidatePath("/");
  return res;
}

/** Mint the host's keypair. Returns the PUBLIC half and the exact line to run on the box. */
export async function generateHostKeyAction(
  _prev: ActionResult | null,
  formData: FormData,
): Promise<ActionResult> {
  const id = String(formData.get("host_id") ?? "").trim();
  if (!id) return { ok: false, error: "missing host_id" };
  const res = await postJsonReturn(
    `/v1/deployment-hosts/${encodeURIComponent(id)}/key`,
    {
      actor: getActor(formData),
      // Which store holds the PRIVATE half. platform_secret_store is unconfigured on a
      // fully-local run, so the caller has to be able to pick "file" — the same choice the
      // credentials wizard already offers.
      backend: String(formData.get("backend") ?? "").trim() || "platform_secret_store",
    },
  );
  revalidatePath("/");
  return res;
}

/** Point an app at a verified host, from the app card. */
export async function setupDeploymentAction(
  _prev: ActionResult | null,
  formData: FormData,
): Promise<ActionResult> {
  const runId = String(formData.get("run_id") ?? "").trim();
  if (!runId) return { ok: false, error: "missing run_id" };
  const res = await postJsonReturn(
    `/v1/code-tasks/${encodeURIComponent(runId)}/setup-deployment`,
    {
      actor: getActor(formData),
      host_id: String(formData.get("host_id") ?? "").trim(),
      app_name: String(formData.get("app_name") ?? "").trim(),
      app_port: String(formData.get("app_port") ?? "").trim() || undefined,
      environment: "staging",
      auto_deploy: true,
    },
  );
  revalidatePath("/");
  return res;
}

/**
 * Ship this app to its linked TARGET for the first time — later changes go through
 * Approve & ship.
 *
 * Deploys the target, not the run. `/v1/code-tasks/{id}/deploy` is the older plan/ssh path
 * that needs an explicit host and user; using it here would ask the card to re-supply an
 * address the host record already owns — the exact duplication `host_id` exists to remove.
 */
export async function deployRunNowAction(
  _prev: ActionResult | null,
  formData: FormData,
): Promise<ActionResult> {
  const targetId = String(formData.get("target_id") ?? "").trim();
  if (!targetId) return { ok: false, error: "this app is not linked to a host yet" };
  const res = await postJsonReturn("/v1/delivery/deploy", {
    target_id: targetId,
    // Which code task this shipped. The endpoint already accepts it; not sending it left
    // `release_packet.code_task_run_id` null, so the app card — which finds its deployment by
    // exactly that link — could not see the deploy it had just started, and went on showing
    // "not deployed" beside a running deployment.
    code_task_run_id: String(formData.get("run_id") ?? "").trim() || undefined,
    actor: getActor(formData),
  });
  revalidatePath("/");
  return res;
}

/** What this code task shipped — powers the deployment line on the app card. */
export async function runDeploymentsAction(
  runId: string, appName?: string,
): Promise<ActionResult> {
  const id = (runId ?? "").trim();
  if (!id) return { ok: false, error: "missing run id" };
  const q = appName ? `?app_name=${encodeURIComponent(appName)}` : "";
  try {
    const r = await fetch(
      `${API_URL}/v1/code-tasks/${encodeURIComponent(id)}/deployments${q}`,
      { headers: authHeaders({}), cache: "no-store" },
    );
    if (!r.ok) return { ok: false, error: `HTTP ${r.status}` };
    return { ok: true, data: await r.json() };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

/**
 * Retry a deployment that failed.
 *
 * The case this exists for is the honest one: a deploy that failed on a network blip or a host
 * that was briefly unreachable. Same commit, same target — `redeploy` is idempotent by receipt,
 * so a retry cannot double-apply.
 */
export async function retryDeploymentAction(
  _prev: ActionResult | null,
  formData: FormData,
): Promise<ActionResult> {
  const id = String(formData.get("deployment_run_id") ?? "").trim();
  if (!id) return { ok: false, error: "missing deployment_run_id" };
  const res = await postJsonReturn(
    `/v1/delivery/deployments/${encodeURIComponent(id)}/redeploy`,
    { actor: getActor(formData) },
  );
  revalidatePath("/");
  return res;
}

/**
 * PATH 1 — adopt a key the customer already has. The private half goes straight through to the
 * secret backend; BOSS keeps a reference and a fingerprint, never the material.
 */
export async function adoptHostKeyAction(
  _prev: ActionResult | null,
  formData: FormData,
): Promise<ActionResult> {
  const id = String(formData.get("host_id") ?? "").trim();
  const privateKey = String(formData.get("private_key") ?? "");
  if (!id) return { ok: false, error: "missing host_id" };
  if (!privateKey.trim()) return { ok: false, error: "paste the PRIVATE key" };
  const res = await postJsonReturn(
    `/v1/deployment-hosts/${encodeURIComponent(id)}/adopt-key`,
    {
      actor: getActor(formData),
      private_key: privateKey,
      backend: String(formData.get("backend") ?? "").trim() || "file",
    },
  );
  revalidatePath("/");
  return res;
}

/** PATH 2 — BOSS generates its own key and installs it over the credential it already has. */
export async function installHostKeyAction(
  _prev: ActionResult | null,
  formData: FormData,
): Promise<ActionResult> {
  const id = String(formData.get("host_id") ?? "").trim();
  if (!id) return { ok: false, error: "missing host_id" };
  const res = await postJsonReturn(
    `/v1/deployment-hosts/${encodeURIComponent(id)}/install-key`,
    {
      actor: getActor(formData),
      backend: String(formData.get("backend") ?? "").trim() || "file",
    },
  );
  revalidatePath("/");
  return res;
}

/** Make a reachable host able to RUN a deployment — today that means docker. */
export async function provisionHostAction(
  _prev: ActionResult | null,
  formData: FormData,
): Promise<ActionResult> {
  const id = String(formData.get("host_id") ?? "").trim();
  if (!id) return { ok: false, error: "missing host_id" };
  const res = await postJsonReturn(
    `/v1/deployment-hosts/${encodeURIComponent(id)}/provision`,
    { actor: getActor(formData) },
  );
  revalidatePath("/");
  return res;
}

/** Actually connect. `verified` is evidence, not a checkbox someone ticked. */
export async function verifyDeploymentHostAction(
  _prev: ActionResult | null,
  formData: FormData,
): Promise<ActionResult> {
  const id = String(formData.get("host_id") ?? "").trim();
  if (!id) return { ok: false, error: "missing host_id" };
  const res = await postJsonReturn(
    `/v1/deployment-hosts/${encodeURIComponent(id)}/verify`, {},
  );
  revalidatePath("/");
  return res;
}

/**
 * Hand the host back the way BOSS found it, and stop scheduling on it.
 *
 * The other half of onboarding — and the real decommission path later. Removes only BOSS's own
 * keys; leaves docker, the deploy directory, and any key the customer lent BOSS.
 */
export async function retireHostAction(
  _prev: ActionResult | null,
  formData: FormData,
): Promise<ActionResult> {
  const id = String(formData.get("host_id") ?? "").trim();
  if (!id) return { ok: false, error: "missing host_id" };
  const res = await postJsonReturn(
    `/v1/deployment-hosts/${encodeURIComponent(id)}/retire`,
    {
      actor: getActor(formData),
      revoke_key: String(formData.get("revoke_key") ?? "true") === "true",
      remove_docker: String(formData.get("remove_docker") ?? "false") === "true",
      purge: String(formData.get("purge") ?? "false") === "true",
      force: String(formData.get("force") ?? "false") === "true",
    },
  );
  revalidatePath("/");
  return res;
}

/** Point an app at a host. target_ref is derived, so the two cannot drift. */
export async function attachTargetToHostAction(
  _prev: ActionResult | null,
  formData: FormData,
): Promise<ActionResult> {
  const hostId = String(formData.get("host_id") ?? "").trim();
  const targetId = String(formData.get("target_id") ?? "").trim();
  if (!hostId || !targetId) return { ok: false, error: "host_id and target_id are required" };
  const res = await postJsonReturn(
    `/v1/deployment-hosts/${encodeURIComponent(hostId)}/attach`,
    { target_id: targetId },
  );
  revalidatePath("/");
  return res;
}

export async function startPreviewAction(
  _prev: { url?: string; error?: string; note?: string } | null,
  formData: FormData,
): Promise<{ url?: string; error?: string; note?: string }> {
  const runId = String(formData.get("run_id") ?? "").trim();
  if (!runId) return { error: "missing run_id" };
  try {
    const res = await client.startPreview(runId, 60, getActor(formData));
    revalidatePath("/");
    // Return the api-routed proxy URL — the preview's own 127.0.0.1 port isn't reachable from the
    // browser on a remote box, so the api reverse-proxies to it. Works locally too.
    if (res.status === "running" && res.id) {
      return { url: `${API_URL}/v1/previews/${encodeURIComponent(res.id)}/web/` };
    }
    // Library/CLI build — there is no live web app to serve, so it was "run" by executing its
    // tests. Show that outcome instead of mislabeling a passing test run as a failure.
    if (res.mode === "test" || res.kind === "library" || res.status === "verified") {
      const head = res.message
        || `Library build — ran \`${res.command ?? "tests"}\`: ${res.passed ? "PASSED ✓" : "FAILED ✗"} (exit ${res.exit_code ?? "?"}).`;
      const body = res.output ? `\n\n${res.output}` : "";
      return res.passed ? { note: head + body } : { error: head + body };
    }
    return { error: res.error || "preview failed to start" };
  } catch (e) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
}

export async function overrideCodeTaskAction(
  _prev: { url?: string; error?: string } | null,
  formData: FormData,
): Promise<{ url?: string; error?: string }> {
  const runId = String(formData.get("run_id") ?? "").trim();
  const reason = String(formData.get("reason") ?? "").trim();
  if (!runId) return { error: "missing run_id" };
  try {
    const res = await client.overrideCodeTask(runId, ["*"], reason, getActor(formData));
    revalidatePath("/");
    if (res.pull_request_url) return { url: res.pull_request_url };
    return { error: "override did not open a PR" };
  } catch (e) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
}

// ---- AI SRE reliability (RB-11) ----

export async function reliabilityDiagnoseAction(formData: FormData): Promise<void> {
  const incidentId = String(formData.get("incident_id") ?? "");
  if (!incidentId) return;
  await postJson(`/v1/reliability/incidents/${incidentId}/diagnose`, {
    project_id: getProjectId(formData),
    actor: getActor(formData),
  });
}

export async function reliabilityFeedbackAction(formData: FormData): Promise<void> {
  const incidentId = String(formData.get("incident_id") ?? "");
  const verdict = String(formData.get("verdict") ?? "");
  if (!incidentId || !verdict) return;
  await postJson(`/v1/reliability/incidents/${incidentId}/feedback`, {
    project_id: getProjectId(formData),
    actor: getActor(formData),
    verdict,
  });
}

export async function reliabilityRemediateAction(formData: FormData): Promise<void> {
  const incidentId = String(formData.get("incident_id") ?? "");
  if (!incidentId) return;
  await postJson(`/v1/reliability/incidents/${incidentId}/remediate`, {
    project_id: getProjectId(formData),
    actor: getActor(formData),
  });
}

export async function createPreviewFixtureAction(
  _prev: { id?: string; error?: string; open_url?: string } | null,
  formData: FormData,
): Promise<{ id?: string; error?: string; open_url?: string }> {
  const projectId = getProjectId(formData);
  try {
    const res = await fetch(`${API_URL}/v1/previews/fixture`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        project_id: projectId,
        actor: getActor(formData),
        ttl_minutes: 60,
      }),
    });
    if (!res.ok) {
      const detail = (await res.text()).slice(0, 240);
      return { error: `API returned ${res.status}: ${detail || res.statusText}` };
    }
    const body = (await res.json()) as { id?: string; open_url?: string; url?: string };
    revalidatePath("/");
    return { id: body.id, open_url: body.open_url ?? body.url };
  } catch (e) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
}

export async function metatronCaptainTurnAction(
  _prev: ActionResult | null,
  formData: FormData,
): Promise<ActionResult> {
  const message = String(formData.get("message") ?? "").trim();
  if (!message) {
    return { ok: false, error: "Enter a captain message." };
  }
  const result = await postJsonReturn("/v1/metatron/session", {
    project_id: getProjectId(formData),
    actor: getActor(formData),
    thread_id: String(formData.get("thread_id") ?? "").trim() || undefined,
    message,
  });
  revalidatePath("/");
  return result;
}

export async function metatronCaptainWatchAction(
  _prev: ActionResult | null,
  formData: FormData,
): Promise<ActionResult> {
  const result = await postJsonReturn("/v1/metatron/watch", {
    project_id: getProjectId(formData),
    actor: getActor(formData),
  });
  revalidatePath("/");
  return result;
}

export async function metatronCaptainDriveAction(
  _prev: ActionResult | null,
  formData: FormData,
): Promise<ActionResult> {
  const result = await postJsonReturn("/v1/metatron/drive", {
    project_id: getProjectId(formData),
    actor: getActor(formData),
    auto: false,
  });
  revalidatePath("/");
  return result;
}

export async function metatronCaptainAddGoalAction(
  _prev: ActionResult | null,
  formData: FormData,
): Promise<ActionResult> {
  const title = String(formData.get("title") ?? "").trim();
  if (!title) {
    return { ok: false, error: "Goal title is required." };
  }
  const result = await postJsonReturn("/v1/metatron/goals", {
    project_id: getProjectId(formData),
    actor: getActor(formData),
    title,
    kind: String(formData.get("kind") ?? "custom").trim() || "custom",
    description: String(formData.get("description") ?? "").trim(),
  });
  revalidatePath("/");
  return result;
}
