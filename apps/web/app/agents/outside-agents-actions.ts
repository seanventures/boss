"use client";

/**
 * What the Outside agents panel posts (KG P6b console). Same-origin `/v1/external-agents…`
 * through the browser proxy (app/v1/[...path]/route.ts adds the key on the server), so the
 * browser never holds a credential and the auth secret itself never crosses this module — the
 * form writes it straight to `/v1/secrets` (infra's own secret store) and only the resulting
 * `secret_ref_id` is ever sent here.
 */
import { apiWrite, type Press } from "../../lib/api-write";
import { notifyChanged } from "../changed";

const enc = encodeURIComponent;

export type Result<T = Record<string, unknown>> = { ok: true; data: T } | { ok: false; error: string };

function apiSentence(body: unknown, status: number): string {
  const b = (body ?? {}) as { detail?: unknown; error?: unknown; message?: unknown };
  const d = b.detail ?? b.error ?? b.message;
  if (typeof d === "string" && d) return d;
  return `The API answered ${status}.`;
}

const P = (scope: string, label: string): Press => ({ scope, label });

export type OutsideAgent = {
  id: string;
  project_id: string;
  display_name: string;
  endpoint_url: string;
  secret_ref_id: string;
  sponsor_principal_id: string;
  status: string;
  created_by: string;
  created_at: string;
  updated_at: string;
};

async function read<T>(path: string): Promise<Result<T>> {
  try {
    const r = await fetch(path, { cache: "no-store" });
    const data = await r.json().catch(() => null);
    if (!r.ok) return { ok: false, error: apiSentence(data, r.status) };
    return { ok: true, data: (data ?? {}) as T };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : "The API did not answer." };
  }
}

async function send<T>(method: "POST", path: string, body: Record<string, unknown>, press: Press): Promise<Result<T>> {
  try {
    const r = await apiWrite(path, {
      method, cache: "no-store",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }, { press });
    const data = await r.json().catch(() => null);
    if (!r.ok) return { ok: false, error: apiSentence(data, r.status) };
    return { ok: true, data: (data ?? {}) as T };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : "The API did not answer." };
  }
}

export const listOutsideAgents = (projectId: string) =>
  read<{ items: OutsideAgent[] }>(`/v1/external-agents?project_id=${enc(projectId)}`);

export const registerOutsideAgent = (projectId: string, actor: string, f: {
  display_name: string; endpoint_url: string; secret_ref_id: string; sponsor_principal_id: string;
}) => send<OutsideAgent>("POST", "/v1/external-agents",
  { project_id: projectId, actor, ...f }, P("outside-agents", "Add outside agent"));

export const testOutsideAgent = (projectId: string, actor: string, agentId: string) =>
  send<{ reachable: boolean }>("POST", `/v1/external-agents/${enc(agentId)}/test`,
    { project_id: projectId, actor }, P(`outside-agent-${agentId}`, "Test"));

export const suspendOutsideAgent = (projectId: string, actor: string, agentId: string) =>
  send<OutsideAgent>("POST", `/v1/external-agents/${enc(agentId)}/suspend`,
    { project_id: projectId, actor }, P(`outside-agent-${agentId}`, "Suspend"));

export const removeOutsideAgent = (projectId: string, actor: string, agentId: string) =>
  send<OutsideAgent>("POST", `/v1/external-agents/${enc(agentId)}/remove`,
    { project_id: projectId, actor }, P(`outside-agent-${agentId}`, "Remove"));

/** Write a secret through infra's own store and hand back only its reference — never the
 *  value itself, which is what `registerOutsideAgent` is allowed to carry. */
export const putOutsideAgentSecret = (projectId: string, actor: string, f: { name: string; value: string }) =>
  send<{ secret_ref_id?: string }>("POST", "/v1/secrets",
    { project_id: projectId, actor, name: f.name, value: f.value, secret_kind: "api_token", backend: "platform_secret_store" },
    P("outside-agents", "Add outside agent"));

export function afterOutsideAgentWrite(reload?: () => void): void {
  notifyChanged("agents");
  reload?.();
}

/** The endpoint's host only — never shows the full URL, which may carry a path or query. */
export function hostOf(endpoint: string): string {
  try { return new URL(endpoint).host; } catch { return endpoint; }
}
