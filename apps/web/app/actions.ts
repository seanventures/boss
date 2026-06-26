"use server";

import { revalidatePath } from "next/cache";
import type { DataBoundary } from "@boss/api-client";
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

export async function tallyVotesAction(_prev: ActionResult | null, formData: FormData): Promise<ActionResult> {
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
  return postJsonReturn(`/v1/meetings/tally`, {
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
  await client.requeueDeadletterJob(deadletterId.trim(), {
    actor: getActor(formData),
    reason: getOptionalString(formData, "reason") ?? "operator_requested_requeue",
  });
  revalidatePath("/");
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
  await client.createProviderAccount({
    project_id: getProjectId(formData),
    provider: provider.trim(),
    account_label: accountLabel.trim(),
    credential_ref: getOptionalString(formData, "credential_ref"),
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

export async function testProviderAccountAction(formData: FormData): Promise<void> {
  const providerAccountId = formData.get("provider_account_id");
  if (typeof providerAccountId !== "string" || !providerAccountId.trim()) {
    throw new Error("provider_account_id is required");
  }
  await client.testProviderAccount(
    providerAccountId.trim(),
    getProjectId(formData),
    getActor(formData),
  );
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
  const roles = formData.getAll("roles").map((r) => String(r)).filter(Boolean);
  if (!directive) {
    return;
  }
  await client.conveneMeeting({
    project_id: projectId,
    directive,
    url: url || undefined,
    roles: roles.length ? roles : undefined,
    task_id: taskId || undefined,
    actor: getActor(formData),
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
