"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import { API_URL, authHeaders, client, PROJECT_ID, resolveProjectId } from "../lib/client";
import type { ActionResult } from "./actions";

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

function getOptionalString(formData: FormData, name: string): string | undefined {
  const value = formData.get(name);
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

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
