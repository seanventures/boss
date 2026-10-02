"use server";

import { revalidatePath } from "next/cache";
import { API_KEY, API_URL } from "../lib/client";
import { apiWrite } from "../lib/api-write";

async function post(path: string, body: Record<string, unknown>): Promise<void> {
  const response = await apiWrite(`${API_URL}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(API_KEY ? { Authorization: `Bearer ${API_KEY}` } : {}),
    },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const payload = (await response.json()) as { detail?: string };
      detail = payload.detail ?? detail;
    } catch {}
    throw new Error(detail);
  }
  revalidatePath("/");
}

function text(formData: FormData, key: string, fallback = ""): string {
  return String(formData.get(key) ?? fallback).trim();
}

function integer(formData: FormData, key: string, fallback: number): number {
  const parsed = Number(text(formData, key, String(fallback)));
  return Number.isFinite(parsed) ? Math.round(parsed) : fallback;
}

function lines(formData: FormData, key: string): string[] {
  return text(formData, key)
    .split(/\r?\n|,/)
    .map((item) => item.trim())
    .filter(Boolean);
}

export async function upsertRankedWorkAction(formData: FormData): Promise<void> {
  const subjectId = text(formData, "subject_id") || crypto.randomUUID();
  await post("/v1/work-orchestration/items", {
    project_id: text(formData, "project_id"),
    actor: text(formData, "actor", "ceo"),
    subject_type: text(formData, "subject_type", "feature"),
    subject_id: subjectId,
    title: text(formData, "title"),
    description: text(formData, "description"),
    rationale: text(formData, "rationale"),
    workload_class: text(formData, "workload_class", "general"),
    estimated_units: integer(formData, "estimated_units", 1),
    strategic_value: integer(formData, "strategic_value", 50),
    customer_impact: integer(formData, "customer_impact", 50),
    urgency: integer(formData, "urgency", 50),
    cost_of_delay: integer(formData, "cost_of_delay", 50),
    risk_reduction: integer(formData, "risk_reduction", 50),
    dependency_pressure: integer(formData, "dependency_pressure", 50),
    confidence: integer(formData, "confidence", 75),
    manual_adjustment: integer(formData, "manual_adjustment", 0),
  });
}

export async function upsertResourceQuotaAction(formData: FormData): Promise<void> {
  await post("/v1/work-orchestration/quotas", {
    project_id: text(formData, "project_id"),
    actor: text(formData, "actor", "ceo"),
    scope_type: text(formData, "scope_type", "project"),
    scope_id: text(formData, "scope_id"),
    resource_kind: text(formData, "resource_kind", "job_concurrency"),
    reserved_min: integer(formData, "reserved_min", 0),
    soft_limit: integer(formData, "soft_limit", 2),
    hard_limit: integer(formData, "hard_limit", 3),
    burst_limit: integer(formData, "burst_limit", 0),
    allocation_weight: Number(text(formData, "allocation_weight", "1")),
    borrow_enabled: formData.get("borrow_enabled") === "true",
  });
}

export async function planResourceAllocationsAction(formData: FormData): Promise<void> {
  await post("/v1/work-orchestration/allocations/plan", {
    project_id: text(formData, "project_id"),
    actor: text(formData, "actor", "ceo"),
    resource_kind: text(formData, "resource_kind", "job_concurrency"),
  });
}

export async function syncTrackerBindingAction(formData: FormData): Promise<void> {
  await post("/v1/work-orchestration/tracker-bindings", {
    project_id: text(formData, "project_id"),
    actor: text(formData, "actor", "ceo"),
    work_item_id: text(formData, "work_item_id"),
    provider_key: text(formData, "provider_key", "plane"),
    external_project_id: text(formData, "external_project_id") || undefined,
    external_issue_id: text(formData, "external_issue_id") || undefined,
    external_key: text(formData, "external_key") || undefined,
    external_url: text(formData, "external_url") || undefined,
    external_state: text(formData, "external_state") || undefined,
  });
}

export async function setDefaultWorkTrackerAction(formData: FormData): Promise<void> {
  await post("/v1/work-trackers/set-default", {
    project_id: text(formData, "project_id"),
    actor: text(formData, "actor", "ceo"),
    provider_key: text(formData, "provider_key", "plane"),
  });
}

export async function connectWorkTrackerAction(formData: FormData): Promise<void> {
  const providerKey = text(formData, "provider_key", "plane");
  const configuration: Record<string, string> = {};
  // Common fields from the connect form (provider-specific extras ignored if empty)
  for (const key of [
    "api_base_url",
    "web_base_url",
    "workspace_slug",
    "project_id_external",
    "base_url",
    "email",
    "project_key",
    "issue_type",
    "team_id",
    "team_key",
    "owner",
    "repo",
    "organization",
    "project",
    "work_item_type",
  ]) {
    const value = text(formData, key);
    if (!value) continue;
    if (key === "project_id_external") configuration.project_id = value;
    else configuration[key] = value;
  }
  await post("/v1/work-trackers/connect", {
    project_id: text(formData, "project_id"),
    actor: text(formData, "actor", "ceo"),
    provider_key: providerKey,
    configuration,
    api_token: text(formData, "api_token") || undefined,
    set_as_default: formData.get("set_as_default") === "true",
    test: formData.get("skip_test") !== "true",
  });
}

export async function upsertDeliveryBotAction(formData: FormData): Promise<void> {
  await post("/v1/work-orchestration/bot-identities", {
    project_id: text(formData, "project_id"),
    actor: text(formData, "actor", "ceo"),
    provider: text(formData, "provider", "mattermost"),
    agent_key: text(formData, "agent_key"),
    display_name: text(formData, "display_name"),
    external_identity: text(formData, "external_identity") || undefined,
  });
}

export async function upsertStatusChannelAction(formData: FormData): Promise<void> {
  await post("/v1/work-orchestration/status-channels", {
    project_id: text(formData, "project_id"),
    actor: text(formData, "actor", "ceo"),
    provider: text(formData, "provider", "mattermost"),
    channel_key: text(formData, "channel_key"),
    display_name: text(formData, "display_name"),
    purpose: text(formData, "purpose"),
    external_channel_id: text(formData, "external_channel_id") || undefined,
    external_url: text(formData, "external_url") || undefined,
    bot_identity_id: text(formData, "bot_identity_id") || undefined,
    event_kinds: lines(formData, "event_kinds"),
    allowed_data_boundaries: ["public", "internal"],
  });
}
