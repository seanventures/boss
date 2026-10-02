import { NextResponse } from "next/server";

import { describeApiError } from "../../../lib/api-error";
import { API_URL, authHeaders } from "../../../lib/client";
import { apiWrite, type WriteMethod } from "../../../lib/api-write";

/**
 * The skills area's reads and writes — skills, who holds them, and the MCP servers they reach.
 *
 * Off the Server Action path for the same reason /api/setup is: authoring a skill is an
 * editing loop, and a save that waits on a full route re-render before it can say "saved" is
 * an editing loop people abandon. The MCP panel uses it too, wherever it is mounted (Skills
 * and MCP, Capabilities), so every screen that changes a server goes through one door and
 * reports a refusal the same way.
 */
export const dynamic = "force-dynamic";

async function forward(path: string, init: { method: "GET" | WriteMethod; body?: unknown }) {
  try {
    const headers = authHeaders({ "Content-Type": "application/json" });
    const body = init.body === undefined ? {} : { body: JSON.stringify(init.body) };
    // Writes go through apiWrite so a repeated press repeats its Idempotency-Key (45e7693);
    // a read is a plain fetch.
    const r = init.method === "GET"
      ? await fetch(`${API_URL}${path}`, { headers, cache: "no-store" })
      : await apiWrite(`${API_URL}${path}`,
                       { method: init.method, headers, ...body, cache: "no-store" });
    // A 204 has no body; that is success, not an empty error.
    const data = r.status === 204 ? {} : await r.json().catch(() => ({}));
    if (!r.ok) {
      return NextResponse.json({ error: describeApiError(data, r.status) }, { status: r.status });
    }
    return NextResponse.json(data);
  } catch (e) {
    return NextResponse.json(
      { error: `Could not reach the BOSS API: ${String((e as Error)?.message ?? e)}` },
      { status: 502 });
  }
}

const q = encodeURIComponent;

export async function GET(req: Request) {
  const url = new URL(req.url);
  const projectId = (url.searchParams.get("project_id") ?? "").trim();
  // TWO CATALOGUES, AND A PROJECT HAS BOTH. `/v1/skills` is what BOSS SHIPS — GitHub PRs,
  // Mattermost posts, model calls, provider cost sync. `/v1/skill-definitions` is what this
  // project authored. A page that showed only the second told a new operator they had nothing,
  // while seventeen skills sat one endpoint away.
  // THREE VIEWS. `builtin` is what BOSS ships; `assignments` is which agent holds which skill;
  // the default is what this project authored. All three render one page, so they share a
  // route rather than growing three that each need the same auth and error handling.
  const view = url.searchParams.get("view");
  // `mcp` is the fourth: a server is where a connector's tools live, and a skill with
  // behaviour "mcp_tool" names one of them. Same page because they answer the same question —
  // what can an agent actually do here — and splitting them made an operator check two.
  // `check` is passed through from a fixed set: none (read what was recorded), stale (ask the
  // servers not checked in the last minute), now (ask every one).
  const check = ["none", "stale", "now"].includes(url.searchParams.get("check") ?? "")
    ? String(url.searchParams.get("check")) : "none";
  const path = view === "builtin"
    ? `/v1/skills?project_id=${q(projectId)}`
    : view === "assignments"
      ? `/v1/role-skills?project_id=${q(projectId)}`
      : view === "mcp"
        ? `/v1/mcp/servers?project_id=${q(projectId)}&check=${check}`
        : `/v1/skill-definitions?project_id=${q(projectId)}`;
  return forward(path, { method: "GET" });
}

export async function POST(req: Request) {
  let body: Record<string, unknown>;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }
  // EVERY WRITE ARRIVES AS A POST NAMING ITS ACTION, deliberately: one route, one allowlist,
  // and the action matched against literals rather than taken from a path this handler would
  // then have to sanitise. An unknown action is refused, not forwarded.
  const action = String(body.action ?? "");
  const remove = action === "delete";
  const key = String(body.skill_key ?? "").trim();
  const projectId = String(body.project_id ?? "").trim();
  const actor = String(body.actor ?? "executive_console");
  if (!projectId) {
    return NextResponse.json({ error: "project_id is required" }, { status: 400 });
  }
  if (remove && !key) {
    return NextResponse.json({ error: "skill_key and project_id are required" }, { status: 400 });
  }

  // GIVING A SKILL TO AN AGENT, AND TAKING IT BACK.
  //
  // Separate from the workflow-step end, which lives in /api/delivery-workflows. The same
  // skill can be held by an agent, placed in a step, both, or neither — so neither route may
  // assume the other happened.
  const assigning = action === "assign" || action === "unassign";
  if (assigning) {
    const roleKey = String(body.role_key ?? "").trim();
    const skillId = String(body.skill_id ?? "").trim();
    if (!roleKey || !skillId) {
      return NextResponse.json(
        { error: "project_id, role_key and skill_id are required" }, { status: 400 });
    }
    const query = `?project_id=${q(projectId)}&role_key=${q(roleKey)}`
      + `&skill_id=${q(skillId)}&actor=${q(actor)}`;
    return action === "assign"
      ? forward("/v1/role-skills", {
          method: "POST",
          body: { project_id: projectId, role_key: roleKey, skill_id: skillId, actor } })
      : forward(`/v1/role-skills${query}`, { method: "DELETE" });
  }

  // TURNING A SKILL ON OR OFF without re-saving every field of it.
  if (action === "enable" || action === "disable") {
    if (!key) return NextResponse.json({ error: "skill_key is required" }, { status: 400 });
    return forward(`/v1/skill-definitions/${q(key)}`, {
      method: "PATCH", body: { project_id: projectId, actor, enabled: action === "enable" } });
  }

  // MCP SERVERS: register, edit (any field, or just on/off), remove. The server id goes into
  // the path, so it is checked against the id shape BOSS issues rather than trusted.
  if (action.startsWith("mcp_")) {
    const serverId = String(body.server_id ?? "").trim();
    if (action !== "mcp_register" && !/^[A-Za-z0-9_-]{1,80}$/.test(serverId)) {
      return NextResponse.json({ error: "server_id is required" }, { status: 400 });
    }
    const where = `/v1/mcp/servers/${q(serverId)}?project_id=${q(projectId)}`;
    if (action === "mcp_register") {
      return forward("/v1/mcp/servers", {
        method: "POST", body: { ...(body.server as object ?? {}), project_id: projectId, actor } });
    }
    if (action === "mcp_update") {
      return forward(where, { method: "PATCH", body: body.changes ?? {} });
    }
    if (action === "mcp_delete") {
      // Same rule as a skill: `release_grants` is only sent after the panel has named the
      // agents that hold the server, and the API refuses without it while any do.
      const release = body.release_grants === true ? "&release_grants=true" : "";
      return forward(`${where}${release}`, { method: "DELETE" });
    }
    return NextResponse.json({ error: `unknown action ${action}` }, { status: 400 });
  }

  if (remove) {
    // `release_grants` is only ever sent after the operator has been told which agents hold
    // the skill and confirmed — see the Delete button on the Skills page.
    const release = body.release_grants === true ? "&release_grants=true" : "";
    return forward(`/v1/skill-definitions/${q(key)}?project_id=${q(projectId)}`
      + `&actor=${q(actor)}${release}`, { method: "DELETE" });
  }
  if (action !== "" && action !== "save") {
    return NextResponse.json({ error: `unknown action ${action}` }, { status: 400 });
  }
  const definition: Record<string, unknown> = { ...body };
  delete definition.action;
  return forward("/v1/skill-definitions", { method: "POST", body: definition });
}
