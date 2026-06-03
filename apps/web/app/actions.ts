"use server";

import { revalidatePath } from "next/cache";
import { client, PROJECT_ID, resolveProjectId } from "../lib/client";
import { buildCommandRequest } from "../lib/commands";

function getProjectId(formData: FormData): string {
  const value = formData.get("project_id");
  return typeof value === "string" && value.trim()
    ? value.trim()
    : resolveProjectId(PROJECT_ID);
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
    actor: "executive_console",
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
  const payload = buildCommandRequest(
    formData.get("command"),
    formData.get("deadline"),
    projectId,
  );
  await client.proposeCommand(payload);
  revalidatePath("/");
}

export async function refreshHealthSnapshotAction(formData: FormData): Promise<void> {
  await client.refreshHealthSnapshot({
    project_id: getProjectId(formData),
    actor: "executive_console",
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
    actor: "executive_console",
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
    actor: "executive_console",
  });
  revalidatePath("/");
}

export async function createReplayBundleAction(formData: FormData): Promise<void> {
  await client.createReplayBundle({
    project_id: getProjectId(formData),
    actor: "executive_console",
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
    actor: "executive_console",
  });
  revalidatePath("/");
}

export async function queueOutboxMessageAction(formData: FormData): Promise<void> {
  const messageId = formData.get("message_id");
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
    actor: "executive_console",
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
    actor: "executive_console",
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
    actor: "executive_console",
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
    actor: "executive_console",
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
    actor: "executive_console",
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
