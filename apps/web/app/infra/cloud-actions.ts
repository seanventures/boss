"use client";

/**
 * What Infra › Cloud's buttons post (spec §4.8; parity P-S-06..P-S-36). Every write keeps the
 * route the old cloud screens used (app/actions.ts → packages/api-client), sent through the
 * browser /v1 proxy with `send` (apiWrite: one press, one effect). Dollars typed in a form are
 * converted to micros HERE, because the routes take micros; the screen never shows micros.
 */
import type { Press } from "../../lib/api-write";
import { send } from "./infra-actions";

const enc = encodeURIComponent;
const P = (scope: string, label: string): Press => ({ scope, label });

// ── accounts ────────────────────────────────────────────────────────────────────────────────

/** `Paste key`: the value goes to the local vault; BOSS keeps a reference. */
export const pasteKey = (projectId: string, actor: string, f: { provider: string; account_label: string; secret_value: string; base_url?: string }) =>
  send<{ secret_ref_id?: string; secret?: { secret_ref_id?: string } }>("POST", "/v1/providers/easy-connect", {
    project_id: projectId, actor, provider: f.provider, account_label: f.account_label.trim(),
    secret_value: f.secret_value.trim(), backend: "local_vault", read_only: true, ...(f.base_url ? { base_url: f.base_url } : {}),
  }, P("cloud-account-new", "Paste key"));

/** `Use secret ref`: bind existing secret references to a new connection. */
export const connectWithRefs = (projectId: string, actor: string, f: {
  provider: string; account_label: string; auth_method: string; credential_refs: Record<string, string>; scopes: string[];
  read_only: boolean; billing_readable: boolean; deploy_capable: boolean; destructive_actions_disabled: boolean;
}) => send("POST", "/v1/providers", { project_id: projectId, actor, ...f }, P("cloud-account-new", "Use secret ref"));

export const savePolicy = (projectId: string, actor: string, accountId: string, f: {
  account_label: string; owner_actor: string; scopes: string[]; read_only: boolean; billing_readable: boolean;
  deploy_capable: boolean; destructive_actions_disabled: boolean;
}) => send("PATCH", `/v1/providers/${enc(accountId)}`, { project_id: projectId, actor, ...f }, P(`cloud-account-${accountId}`, "Save policy"));

export const testAccount = (projectId: string, actor: string, accountId: string) =>
  send("POST", `/v1/providers/${enc(accountId)}/test?project_id=${enc(projectId)}&actor=${enc(actor)}`, null, P(`cloud-account-${accountId}`, "Test"));
export const syncAccount = (projectId: string, actor: string, accountId: string | null) =>
  send("POST", "/v1/resources/sync", { project_id: projectId, actor, ...(accountId ? { provider_account_id: accountId } : {}) },
       P(accountId ? `cloud-account-${accountId}` : "cloud-resources", accountId ? "Sync" : "Sync all"));
export const enableAccount = (projectId: string, actor: string, accountId: string) =>
  send("POST", `/v1/providers/${enc(accountId)}/enable`, { project_id: projectId, actor }, P(`cloud-account-${accountId}`, "Enable"));
export const disableAccount = (projectId: string, actor: string, accountId: string) =>
  send("POST", `/v1/providers/${enc(accountId)}/disable`, { project_id: projectId, actor }, P(`cloud-account-${accountId}`, "Disable"));
export const rotateAccount = (projectId: string, actor: string, accountId: string, refs: Record<string, string>, overlapMinutes: number) =>
  send("POST", `/v1/providers/${enc(accountId)}/rotate`, { project_id: projectId, actor, credential_refs: refs, overlap_minutes: overlapMinutes },
       P(`cloud-account-${accountId}`, "Rotate"));
export const revokeAccount = (projectId: string, actor: string, accountId: string) =>
  send("POST", `/v1/providers/${enc(accountId)}/revoke`, { project_id: projectId, actor }, P(`cloud-account-${accountId}`, "Revoke"));

// ── resources ───────────────────────────────────────────────────────────────────────────────

export const cleanUp = (projectId: string, actor: string, accountId: string | null, maxCandidates: number) =>
  send("POST", "/v1/resource-janitor/run", { project_id: projectId, actor, max_candidates: maxCandidates,
    ...(accountId ? { provider_account_id: accountId } : {}) }, P("cloud-resources", "Clean up"));

// ── capacity ────────────────────────────────────────────────────────────────────────────────

export const addPool = (projectId: string, actor: string, f: {
  pool_key: string; kind: string; provider_account_id: string | null; current_workers: number; desired_workers: number;
  min_workers: number; max_workers: number; max_queue_depth_per_worker: number; cost_per_worker_hour_usd_micros: number;
  capabilities: string[];
}) => send("POST", "/v1/worker-pools", { project_id: projectId, actor, ...f, provider_account_id: f.provider_account_id ?? undefined },
           P("cloud-capacity", "+ Pool"));
export const reviewScaling = (projectId: string, actor: string, poolId: string | null, spillover: boolean) =>
  send("POST", "/v1/autoscaler/plans", { project_id: projectId, actor, cloud_spillover_enabled: spillover,
    ...(poolId ? { worker_pool_id: poolId } : {}) }, P("cloud-capacity", "Review scaling"));

// ── changes ─────────────────────────────────────────────────────────────────────────────────

export const createChange = (projectId: string, actor: string, f: {
  provider_account_id: string | null; title: string; summary: string; operations: Record<string, unknown>[];
  expected_cost_usd_micros: number; rollback_plan: string[]; risk_tier: number;
}) => send<{ id?: string }>("POST", "/v1/changesets", {
  project_id: projectId, actor, title: f.title, summary: f.summary, expected_cost_usd_micros: f.expected_cost_usd_micros,
  rollback_plan: f.rollback_plan, risk_tier: f.risk_tier, ...(f.provider_account_id ? { provider_account_id: f.provider_account_id } : {}),
  operations: f.operations.map((op) => ({
    skill_id: String(op.skill_id ?? ""), action: String(op.action ?? ""), resource_type: String(op.resource_type ?? ""),
    ...(typeof op.resource_ref === "string" ? { resource_ref: op.resource_ref } : {}),
    desired_state: op.desired_state && typeof op.desired_state === "object" && !Array.isArray(op.desired_state) ? op.desired_state : {},
    risk_tier: typeof op.risk_tier === "number" ? op.risk_tier : f.risk_tier,
    destructive: Boolean(op.destructive), production_affecting: Boolean(op.production_affecting),
  })),
}, P("cloud-change-new", "+ Change"));
export const approveChange = (actor: string, id: string) =>
  send("POST", `/v1/changesets/${enc(id)}/approve`, { actor }, P(`cloud-change-${id}`, "Approve"));
export const applyChange = (actor: string, id: string) =>
  send("POST", `/v1/changesets/${enc(id)}/apply`, { actor }, P(`cloud-change-${id}`, "Apply"));
