/**
 * The console's calls to the Kit (Skills & MCP). Reads are plain fetches; every write goes
 * through apiWrite (one press, one effect). A failed write resolves to `{ok: false, error,
 * field}` rather than throwing, so a drawer can put the sentence where it belongs.
 */
import { apiWrite } from "./api-write";
import type { Ability, CrewTest, GrantResult, Kit, McpCatalogueEntry, McpTestResult, TestResult } from "./kit-types";

export type Result<T> = { ok: true; data: T } | { ok: false; error: string; field?: string | null; status?: number };

export type CrewMember = { role_key: string; display_name: string | null };
export type KitWithCrew = Kit & { crew?: CrewMember[] };

async function read<T>(url: string): Promise<Result<T>> {
  try {
    const r = await fetch(url, { cache: "no-store" });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) return { ok: false, error: String(data?.reason ?? data?.error ?? `HTTP ${r.status}`), status: r.status };
    return { ok: true, data: data as T };
  } catch (e) {
    return { ok: false, error: String((e as Error)?.message ?? e), status: 0 };
  }
}

async function write<T>(url: string, body: Record<string, unknown>, _label: string): Promise<Result<T>> {
  try {
    // No press scope: a long Test all must not refuse a toggle pressed meanwhile. apiWrite still
    // makes each press one effect (its Idempotency-Key and in-flight dedupe).
    const r = await apiWrite(url, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) return { ok: false, error: String(data?.error ?? `HTTP ${r.status}`), field: data?.field ?? null, status: r.status };
    return { ok: true, data: data as T };
  } catch (e) {
    return { ok: false, error: String((e as Error)?.message ?? e), status: 0 };
  }
}

const enc = encodeURIComponent;

export const kitApi = {
  load: (projectId: string, check: "stale" | "none" = "stale") =>
    read<KitWithCrew>(`/api/kit?project_id=${enc(projectId)}&check=${check}`),
  ability: (projectId: string, key: string) =>
    read<Ability>(`/api/kit?project_id=${enc(projectId)}&ability=${enc(key)}`),
  connect: (projectId: string, actor: string, ability: string, provider: string, fields: Record<string, string>) =>
    write<Ability>("/api/kit", { action: "connect", project_id: projectId, actor, ability, provider, fields }, "Save & test"),
  disconnect: (projectId: string, actor: string, ability: string) =>
    write<Ability>("/api/kit", { action: "disconnect", project_id: projectId, actor, ability }, "Disconnect"),
  test: (projectId: string, actor: string, ability: string) =>
    write<TestResult & { ability: Ability }>("/api/kit", { action: "test", project_id: projectId, actor, ability }, "Test"),
  testAll: (projectId: string, actor: string) =>
    write<{ tests: Record<string, TestResult>; kit: KitWithCrew }>("/api/kit", { action: "test_all", project_id: projectId, actor }, "Test all"),
  tryIt: (projectId: string, actor: string, ability: string, input: string) =>
    write<{ ok: boolean; preview: string; receipt_id: string | null }>("/api/kit", { action: "try", project_id: projectId, actor, ability, input }, "Run"),
  holders: (projectId: string, actor: string, ability: string, roles: string[]) =>
    write<Ability>("/api/kit", { action: "holders", project_id: projectId, actor, ability, roles }, "Agents"),
  policy: (projectId: string, actor: string, ability: string, toolRef: string, policy: string) =>
    write<Ability>("/api/kit", { action: "policy", project_id: projectId, actor, ability, tool_ref: toolRef, policy }, "Tools"),
  enabled: (projectId: string, actor: string, ability: string, enabled: boolean) =>
    write<Ability>("/api/kit", { action: "enabled", project_id: projectId, actor, ability, enabled }, enabled ? "Turn on" : "Turn off"),
  recommended: (projectId: string, actor: string, roles?: string[]) =>
    write<GrantResult>("/api/kit", { action: "recommended", project_id: projectId, actor, roles: roles ?? null }, "Apply recommended"),
  testCrew: (projectId: string, actor: string, live: boolean) =>
    write<CrewTest>("/api/kit", { action: "test_crew", project_id: projectId, actor, live }, "Test crew"),
};

export const mcpApi = {
  list: (projectId: string, check: "none" | "stale" | "now" = "none") =>
    read<{ items: McpServer[] }>(`/api/mcp?project_id=${enc(projectId)}&check=${check}`),
  catalogue: (projectId: string) => read<{ items: McpCatalogueEntry[] }>(`/api/mcp?view=catalogue&project_id=${enc(projectId)}`),
  test: (projectId: string, actor: string, url: string, auth: McpAuthInput | null) =>
    write<McpTestResult>("/api/mcp", { action: "test", project_id: projectId, actor, url, auth }, "Save & test"),
  register: (projectId: string, actor: string, server: Record<string, unknown>) =>
    write<McpServer>("/api/mcp", { action: "register", project_id: projectId, actor, server }, "Save & test"),
  update: (projectId: string, actor: string, serverId: string, changes: Record<string, unknown>) =>
    write<McpServer>("/api/mcp", { action: "update", project_id: projectId, actor, server_id: serverId, changes }, "Save"),
  review: (projectId: string, actor: string, serverId: string) =>
    write<McpServer>("/api/mcp", { action: "review", project_id: projectId, actor, server_id: serverId }, "Mark reviewed"),
  remove: (projectId: string, actor: string, serverId: string, releaseGrants: boolean) =>
    write<Record<string, never>>("/api/mcp", { action: "delete", project_id: projectId, actor, server_id: serverId, release_grants: releaseGrants }, "Remove"),
  policy: (projectId: string, actor: string, serverId: string, tool: string, policy: string) =>
    write<McpServer>("/api/mcp", { action: "policy", project_id: projectId, actor, server_id: serverId, tool, policy }, "Tools"),
  grant: (projectId: string, actor: string, serverId: string, role: string) =>
    write<Record<string, unknown>>("/api/mcp", { action: "grant", project_id: projectId, actor, server_id: serverId, role_key: role, tools: [] }, "Agents"),
  revoke: (projectId: string, actor: string, serverId: string, role: string) =>
    write<Record<string, unknown>>("/api/mcp", { action: "revoke", project_id: projectId, actor, server_id: serverId, role_key: role }, "Agents"),
  grants: (projectId: string, role: string) =>
    read<{ assignments: { role_key: string; mcp_server_id: string; tools: string[] }[] }>(
      `/api/mcp?view=grants&project_id=${enc(projectId)}&role=${enc(role)}`),
  roleTools: (projectId: string, actor: string, serverId: string, role: string, tools: string[]) =>
    write<Record<string, unknown>>("/api/mcp", { action: "role_tools", project_id: projectId, actor, server_id: serverId, role_key: role, tools }, "Tools"),
  addFromCatalogue: (projectId: string, actor: string, key: string, token: string | null) =>
    write<{ server: McpServer; test: McpTestResult }>("/api/mcp", { action: "catalogue_add", project_id: projectId, actor, key, token }, "Add"),
};

export type McpAuthInput = { type: "none" | "bearer" | "header"; header_name?: string; secret?: string };

export type McpServer = {
  id: string; name: string; url: string; enabled: boolean; risk_tier: number; data_boundary: string;
  description?: string | null; trusted: boolean; reachable: boolean | null; last_error?: string | null;
  last_checked_at?: string | null; tools: { name: string; description?: string }[];
  tool_policy: Record<string, string>; tool_risk_overrides: Record<string, number>;
  provenance: Record<string, unknown>; publisher: Record<string, unknown>;
  auth: { type: string; header_name: string | null; set: boolean; last4: string | null };
  references?: { custom_steps: { step_key: string; label?: string }[]; skills: { skill_key: string; label?: string }[];
                 agents: { role_key: string; tools: string[] }[] };
};
