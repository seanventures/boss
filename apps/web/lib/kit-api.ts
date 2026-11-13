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
  credentials: (projectId: string, serverId: string) =>
    read<McpCredentials>(`/api/mcp?view=credentials&project_id=${enc(projectId)}&server_id=${enc(serverId)}`),
  signIn: (projectId: string, actor: string, serverId: string, subject: "org" | "agent" = "org", roleKey?: string) =>
    write<{ redirect: string }>("/api/mcp", { action: "oauth_start", project_id: projectId, actor, server_id: serverId, subject, role_key: roleKey ?? null }, "Sign in"),
  signInMine: (projectId: string, actor: string, serverId: string) =>
    write<{ redirect: string }>("/api/mcp", { action: "oauth_mine", project_id: projectId, actor, server_id: serverId }, "Use my own account"),
  credentialPolicy: (projectId: string, actor: string, serverId: string, policy: CredentialPolicy) =>
    write<{ credential_policy: CredentialPolicy }>("/api/mcp", { action: "credential_policy", project_id: projectId, actor, server_id: serverId, policy }, "Whose account"),
  removeCredential: (projectId: string, actor: string, serverId: string, credentialId: string, mine: boolean) =>
    write<{ removed: string }>("/api/mcp", { action: "credential_remove", project_id: projectId, actor, server_id: serverId, credential_id: credentialId, mine }, "Disconnect"),
  accessGrants: (projectId: string, serverId: string) =>
    read<{ grants: AccessGrant[] }>(`/api/mcp?view=access_grants&project_id=${enc(projectId)}&server_id=${enc(serverId)}`),
  revokeGrant: (projectId: string, actor: string, grantId: string) =>
    write<AccessGrant>("/api/mcp", { action: "grant_revoke", project_id: projectId, actor, grant_id: grantId }, "Revoke"),
  addFromCatalogue: (projectId: string, actor: string, key: string, token: string | null) =>
    write<{ server: McpServer; test: McpTestResult }>("/api/mcp", { action: "catalogue_add", project_id: projectId, actor, key, token }, "Add"),
  // BOSSAPP-849..851 — the connector catalog, stdio templates and gateways.
  connectors: (projectId: string, query: string, category: string) =>
    read<ConnectorCatalog>(`/api/mcp?view=connectors&project_id=${enc(projectId)}&q=${enc(query)}&category=${enc(category)}`),
  createStdioTemplate: (projectId: string, actor: string, template: StdioTemplateInput) =>
    write<{ id: string; name: string; url: string }>("/api/mcp", { action: "stdio_template_create", project_id: projectId, actor, template }, "Create template"),
  gateways: (projectId: string) => read<{ items: McpGateway[] }>(`/api/mcp?view=gateways&project_id=${enc(projectId)}`),
  createGateway: (projectId: string, actor: string, name: string, profile: GatewayProfile, expiresInDays: number) =>
    write<{ gateway: McpGateway; token: string }>("/api/mcp", { action: "gateway_create", project_id: projectId, actor, name, profile, expires_in_days: expiresInDays }, "Create gateway"),
  mintGatewayToken: (projectId: string, actor: string, gatewayId: string, expiresInDays: number) =>
    write<{ token: string; record: GatewayToken }>("/api/mcp", { action: "gateway_token", project_id: projectId, actor, gateway_id: gatewayId, expires_in_days: expiresInDays }, "New token"),
  revokeGatewayToken: (projectId: string, actor: string, gatewayId: string, tokenId: string) =>
    write<McpGateway>("/api/mcp", { action: "gateway_token_revoke", project_id: projectId, actor, gateway_id: gatewayId, token_id: tokenId }, "Revoke"),
  revokeGateway: (projectId: string, actor: string, gatewayId: string) =>
    write<McpGateway>("/api/mcp", { action: "gateway_revoke", project_id: projectId, actor, gateway_id: gatewayId }, "Revoke gateway"),
};

export type StdioTemplateInput = {
  name: string; image: string; argv: string[]; network: string; env_allowlist: string[];
  env_defaults: Record<string, string>; env_from_auth: string | null; idle_ttl_seconds: number;
  reuse_key: string; connector_key: string; description: string;
};

export type ConnectorPrefill = {
  name: string; transport: "http" | "stdio"; url: string;
  auth: { type: "none" | "bearer" | "header" | "oauth"; header_name: string | null };
  risk_tier: number; data_boundary: string; description: string;
  tool_tiers: Record<string, number>; tool_policies: Record<string, string>;
  oauth: { discovery_url: string | null; scopes: string[]; token_fallback: string | null } | null;
  provenance: Record<string, string>; stdio_template: StdioTemplateInput | null;
};

export type Connector = {
  key: string; name: string; category: string; transport: "http" | "stdio"; description: string;
  auth_kind: string; token_url: string | null; scopes_tip: string | null; setup: string;
  risk_notes: string; docs_url: string | null; default_tier: number; added: boolean;
  template_ready: boolean; definition_version: number | null; prefill: ConnectorPrefill;
};

export type ConnectorCatalog = {
  catalog_version: string; total: number; categories: { key: string; count: number }[]; items: Connector[];
};

export type GatewayProfile = { servers: { server_id: string; tools: string[] | null; caps: Record<string, "allow" | "off"> }[] };
export type GatewayToken = { id: string; label: string | null; last4: string; expires_at: string | null;
                             revoked_at: string | null; last_used_at: string | null; status: string; created_at: string };
export type McpGateway = { id: string; name: string; status: string; path: string; profile: GatewayProfile;
                           allowed_actions: string[]; tokens: GatewayToken[]; created_at: string };

/** Glob match for a connector's tool tier/policy patterns (`*` and `?`), most specific first. */
export function toolOverrides(prefill: ConnectorPrefill, tools: string[]) {
  const rank = (p: string) => [(p.match(/[*?]/g) ?? []).length, -p.length] as const;
  const sorted = (m: Record<string, unknown>) => Object.keys(m).sort((a, b) => {
    const [x1, y1] = rank(a); const [x2, y2] = rank(b); return x1 - x2 || y1 - y2; });
  const re = (g: string) => new RegExp(`^${g.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".")}$`);
  const pick = <T,>(m: Record<string, T>) => {
    const out: Record<string, T> = {};
    const keys = sorted(m);
    for (const t of tools) { const hit = keys.find((g) => re(g).test(t)); if (hit !== undefined) out[t] = m[hit]; }
    return out;
  };
  return { tiers: pick(prefill.tool_tiers), policies: pick(prefill.tool_policies) };
}

export type McpAuthInput = { type: "none" | "bearer" | "header"; header_name?: string; secret?: string };
export type McpAuthInput = { type: "none" | "bearer" | "header" | "oauth"; header_name?: string; secret?: string;
                             oauth?: { client_id?: string; scope?: string } };

/** Whose credential a call carries (BOSSAPP-847). */
export type CredentialPolicy = "shared" | "per_user" | "per_user_fallback" | "per_agent";

export type McpAuthStatus = "connected" | "needs_reauth" | "not_connected";

export type McpCredential = {
  id: string; subject_kind: "org" | "user" | "agent"; subject_id: string; kind: "oauth" | "token";
  connected_as: string | null; status: "active" | "needs_reauth" | "revoked"; updated_at: string | null; mine?: boolean;
};

export type McpCredentials = {
  server_id: string; credential_policy: CredentialPolicy; auth_status: McpAuthStatus | null;
  credentials: McpCredential[];
  you: { ok: boolean; subject_kind?: string; reason?: string; needs_reauth?: boolean } | null;
};

/** A tool an agent was granted from a Grant card (BOSSAPP-848). */
export type AccessGrant = {
  id: string; server_id: string; role_key: string; tool: string; tier: number; policy: "allow" | "ask";
  status: "active" | "revoked" | "dropped" | "refused"; decision_id: string | null; reason: string | null;
  granted_by: string; created_at: string | null;
};

export type McpServer = {
  id: string; name: string; url: string; enabled: boolean; risk_tier: number; data_boundary: string;
  description?: string | null; trusted: boolean; reachable: boolean | null; last_error?: string | null;
  last_checked_at?: string | null; tools: { name: string; description?: string }[];
  tool_policy: Record<string, string>; tool_risk_overrides: Record<string, number>;
  provenance: Record<string, unknown>; publisher: Record<string, unknown>;
  auth: { type: string; header_name: string | null; set: boolean; last4: string | null;
          oauth?: { authorization_server?: string; client_id?: string; client_source?: string; scope?: string } };
  credential_policy?: CredentialPolicy; auth_status?: McpAuthStatus | null;
  references?: { custom_steps: { step_key: string; label?: string }[]; skills: { skill_key: string; label?: string }[];
                 agents: { role_key: string; tools: string[] }[] };
};
