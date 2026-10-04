/**
 * The console's half of the ONE agent model (engine/boss_engine/services/agents.py).
 *
 * An agent is a role, what it holds (skills and MCP servers), and optionally a Studio profile.
 * Every agent write the console makes — quick create, edit, delete, giving or taking a skill or
 * an MCP server — goes through here to /v1/agents and /v1/role-*, via the same-origin proxy
 * (app/v1/[...path]/route.ts, which attaches the API key on the server).
 *
 * THE ACTOR IS ALWAYS PASSED IN. These functions take the signed-in actor from the page and
 * never default one: the Create-agent wizard sent the literal 'vp_agents', which a 'ceo' key may
 * not act as, so on every authed install it registered a role and then 403'd.
 * tests/agent-writes-carry-the-signed-in-actor.test.ts enumerates every console file that writes
 * an agent and fails on any literal actor.
 */
import { apiWrite } from "./api-write";
import type { Persona } from "./agent-world-types";

export type GrantableSkill = {
  skill_id: string; label: string; summary: string; source: string;
  behaviour_kind: string; risk_tier: number; enabled: boolean;
};
export type GrantableServer = {
  mcp_server_id: string; server: string; url: string; enabled: boolean;
  risk_tier: number; description?: string | null;
};
export type AgentSummary = {
  role_key: string; name: string; mission: string; reports_to: string | null;
  autonomy_level: number; skill_count: number; mcp_server_count: number;
};
export type Grantable = {
  skills: GrantableSkill[]; mcp_servers: GrantableServer[]; agents: AgentSummary[];
};
export type McpGrant = { server_id: string; tools?: string[] };
export type AgentResult<T = Record<string, unknown>> =
  | { ok: true; data: T }
  | { ok: false; error: string };

/** FastAPI says why in `detail`: a sentence, or (pydantic 422) a list of {msg, loc}. Flatten it
 *  into the sentence an operator reads — the skills proxy showed "[object Object]" here. */
export function errorSentence(body: unknown, status: number): string {
  const detail = (body as { detail?: unknown; error?: unknown } | null)?.detail
    ?? (body as { error?: unknown } | null)?.error;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const parts = detail.map((item) => {
      if (item && typeof item === "object" && "msg" in item) {
        const loc = Array.isArray((item as { loc?: unknown }).loc)
          ? ((item as { loc: unknown[] }).loc).filter((p) => p !== "body").join(".") : "";
        return `${loc ? `${loc}: ` : ""}${String((item as { msg: unknown }).msg)}`;
      }
      return String(item);
    }).filter(Boolean);
    if (parts.length) return parts.join("; ");
  }
  if (status === 403) return "You are not allowed to change agents on this project — ask a "
    + "member with role_review and workflow_manage to do it.";
  if (status === 503) return "The control plane cannot reach its database right now; try again "
    + "in a minute.";
  return `The control plane answered ${status} without saying why.`;
}

async function send<T>(path: string, method: "POST" | "PATCH" | "DELETE",
                       body: Record<string, unknown> | null,
                       press: { scope: string; label: string }): Promise<AgentResult<T>> {
  try {
    const r = await apiWrite(path, {
      method,
      headers: { "Content-Type": "application/json" },
      ...(body ? { body: JSON.stringify(body) } : {}),
    }, { press });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) return { ok: false, error: errorSentence(data, r.status) };
    return { ok: true, data: data as T };
  } catch (e) {
    return { ok: false, error: `The request did not reach the control plane: ${String((e as Error)?.message ?? e)}` };
  }
}

const enc = encodeURIComponent;

export async function loadGrantable(projectId: string): Promise<AgentResult<Grantable>> {
  try {
    const r = await fetch(`/v1/agents/grantable?project_id=${enc(projectId)}`, { cache: "no-store" });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) return { ok: false, error: errorSentence(data, r.status) };
    return { ok: true, data: data as Grantable };
  } catch (e) {
    return { ok: false, error: `Could not read what agents can be given: ${String((e as Error)?.message ?? e)}` };
  }
}

export function createAgent(projectId: string, actor: string, input: {
  name: string; purpose: string; skills: string[]; mcpServers: McpGrant[];
  reportsTo?: string | null; roleKey?: string;
  /** Agent World: how it speaks (linted server-side; never widens what it may do). */
  persona?: Persona;
  /** What it starts from: a blank agent, a shipped role, or a "thinks like" archetype. */
  bornFrom?: { kind: "blank" } | { kind: "template"; template_key: string }
    | { kind: "archetype"; archetype: string };
}) {
  return send("/v1/agents", "POST", {
    project_id: projectId, actor, name: input.name, purpose: input.purpose,
    skills: input.skills,
    mcp_servers: input.mcpServers.map((g) => ({ server_id: g.server_id, tools: g.tools ?? [] })),
    reports_to: input.reportsTo || null,
    ...(input.roleKey ? { role_key: input.roleKey } : {}),
    ...(input.persona ? { persona: input.persona } : {}),
    ...(input.bornFrom ? { born_from: input.bornFrom } : {}),
  }, { scope: "agent-create", label: "Create agent" });
}

/** Only the fields present are sent, and the API changes only those. */
export function updateAgent(projectId: string, actor: string, roleKey: string, changes: {
  display_name?: string; mission?: string;
  boundaries?: { must: string[]; must_not: string[] }; reports_to?: string | null;
  /** Saving it seals a new version once; the answer carries version, fingerprint, warnings. */
  persona?: Persona;
}) {
  return send(`/v1/agents/${enc(roleKey)}`, "PATCH",
    { project_id: projectId, actor, ...changes },
    { scope: `agent-${roleKey}`, label: "Save agent" });
}

/** `retireOlderVersions`: also retire the older workflow versions that are all that still
 *  name it — the delete page says so before it is pressed (AgentDelete). */
export function deleteAgent(projectId: string, actor: string, roleKey: string,
                            opts: { retireOlderVersions?: boolean } = {}) {
  const retire = opts.retireOlderVersions ? "&retire_older_versions=true" : "";
  return send(`/v1/agents/${enc(roleKey)}?project_id=${enc(projectId)}&actor=${enc(actor)}${retire}`,
    "DELETE", null, { scope: `agent-${roleKey}`, label: "Delete agent" });
}

/** Point one workflow mapping at its workflow's current version (workflows.repoint_mapping).
 *  The move an agent-delete refusal names when a mapping still sends work to an older version
 *  that names the agent — saving the workflow never moved it (review of milestone/agents,
 *  round 3). */
export function repointMapping(projectId: string, actor: string, mappingId: string) {
  return send(`/v1/workflow-mappings/${enc(mappingId)}/repoint`, "POST",
    { project_id: projectId, actor },
    { scope: `mapping-${mappingId}`, label: "Point at the current version" });
}

export function grantSkill(projectId: string, actor: string, roleKey: string, skillId: string) {
  return send("/v1/role-skills", "POST",
    { project_id: projectId, actor, role_key: roleKey, skill_id: skillId },
    { scope: `agent-${roleKey}-skills`, label: "Give this skill" });
}

export function revokeSkill(projectId: string, actor: string, roleKey: string, skillId: string) {
  return send(`/v1/role-skills?project_id=${enc(projectId)}&role_key=${enc(roleKey)}`
    + `&skill_id=${enc(skillId)}&actor=${enc(actor)}`,
    "DELETE", null, { scope: `agent-${roleKey}-skills`, label: "Remove skill" });
}

export function grantMcp(projectId: string, actor: string, roleKey: string, serverId: string,
                         tools: string[] = []) {
  return send("/v1/role-mcp-servers", "POST",
    { project_id: projectId, actor, role_key: roleKey, mcp_server_id: serverId, tools },
    { scope: `agent-${roleKey}-mcp`, label: "Give this MCP server" });
}

export function revokeMcp(projectId: string, actor: string, roleKey: string, serverId: string) {
  return send(`/v1/role-mcp-servers?project_id=${enc(projectId)}&role_key=${enc(roleKey)}`
    + `&mcp_server_id=${enc(serverId)}&actor=${enc(actor)}`,
    "DELETE", null, { scope: `agent-${roleKey}-mcp`, label: "Remove MCP server" });
}

/** The server's rule for a key (agents._KEY): lower_snake_case, a letter first, 2-64 long. */
export function isAgentKey(value: string): boolean {
  return /^[a-z][a-z0-9_]{1,63}$/.test(value);
}

/** "post, read_channel" → ["post", "read_channel"]. Empty means every tool the server has. */
export function parseToolList(value: string): string[] {
  return Array.from(new Set(value.split(/[\s,]+/).map((t) => t.trim()).filter(Boolean)));
}
