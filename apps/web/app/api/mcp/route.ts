import { NextResponse } from "next/server";

import { describeApiError } from "../../../lib/api-error";
import { API_URL, authHeaders } from "../../../lib/client";
import { apiWrite, type WriteMethod } from "../../../lib/api-write";
import { loadPath } from "../../../lib/writing-get";
import { rawRequestUrl } from "../../../lib/raw-request-url";

/**
 * MCP servers for the Library (skills-mcp spec §5.5): list, the curated catalogue, test what was
 * TYPED (stores nothing), register, edit, review, remove. The one door every MCP write in the
 * console goes through; a server id goes into a path only after the id-shape check.
 *
 * A token typed into a form travels here once, inside `auth.secret`, and is written through to
 * the secret store by the API. Nothing here or upstream ever returns it.
 */
export const dynamic = "force-dynamic";

const q = encodeURIComponent;
const ID = /^[A-Za-z0-9_-]{1,80}$/;

async function forward(path: string, init: { method: "GET" | WriteMethod; body?: unknown }) {
  try {
    const headers = authHeaders({ "Content-Type": "application/json" });
    const body = init.body === undefined ? {} : { body: JSON.stringify(init.body) };
    const r = init.method === "GET"
      ? await fetch(`${API_URL}${path}`, { headers, cache: "no-store" })
      : await apiWrite(`${API_URL}${path}`, { method: init.method, headers, ...body, cache: "no-store" });
    const data = r.status === 204 ? {} : await r.json().catch(() => ({}));
    if (!r.ok) {
      const detail = (data as { detail?: unknown }).detail;
      const field = detail && typeof detail === "object" ? (detail as { field?: string }).field ?? null : null;
      const message = detail && typeof detail === "object" && "message" in (detail as object)
        ? String((detail as { message?: string }).message) : describeApiError(data, r.status);
      return NextResponse.json({ error: message, field }, { status: r.status });
    }
    return NextResponse.json(data);
  } catch (e) {
    return NextResponse.json(
      { error: `Could not reach the BOSS API: ${String((e as Error)?.message ?? e)}` }, { status: 502 });
  }
}

export async function GET(req: Request) {
  const url = rawRequestUrl(req);
  const projectId = (url.searchParams.get("project_id") ?? "").trim();
  if (url.searchParams.get("view") === "catalogue") {
    return forward(`/v1/mcp/catalogue?project_id=${q(projectId)}`, { method: "GET" });
  }
  // BOSSAPP-849..851: the connector catalog, stdio templates and gateways.
  if (url.searchParams.get("view") === "connectors") {
    const search = (url.searchParams.get("q") ?? "").slice(0, 120);
    const category = (url.searchParams.get("category") ?? "").replace(/[^a-z]/g, "").slice(0, 30);
    return forward(`/v1/mcp/connectors?project_id=${q(projectId)}&q=${q(search)}&category=${q(category)}`,
      { method: "GET" });
  }
  if (url.searchParams.get("view") === "stdio_templates") {
    return forward(`/v1/mcp/stdio-templates?project_id=${q(projectId)}`, { method: "GET" });
  }
  if (url.searchParams.get("view") === "gateways") {
    return forward(`/v1/mcp/gateways?project_id=${q(projectId)}`, { method: "GET" });
  }
  if (url.searchParams.get("view") === "credentials") {
    // Who has signed in to one server (never a token) and what a call by the viewer would carry.
    const serverId = String(url.searchParams.get("server_id") ?? "");
    if (!ID.test(serverId)) return NextResponse.json({ error: "server_id is required" }, { status: 400 });
    return forward(`/v1/mcp/servers/${q(serverId)}/credentials?project_id=${q(projectId)}`, { method: "GET" });
  }
  if (url.searchParams.get("view") === "access_grants") {
    // Tools agents were granted from a Grant card (BOSSAPP-848).
    const serverId = String(url.searchParams.get("server_id") ?? "");
    const server = ID.test(serverId) ? `&server_id=${q(serverId)}` : "";
    return forward(`/v1/mcp/access-grants?project_id=${q(projectId)}${server}`, { method: "GET" });
  }
  if (url.searchParams.get("view") === "grants") {
    const role = (url.searchParams.get("role") ?? "").trim();
    return forward(`/v1/role-mcp-servers?project_id=${q(projectId)}&role_key=${q(role)}`, { method: "GET" });
  }
  const check = ["none", "stale", "now"].includes(url.searchParams.get("check") ?? "")
    ? String(url.searchParams.get("check")) : "none";
  // A writing GET (it re-checks stale servers): its POST twin, through the write path.
  return forward(loadPath(`/v1/mcp/servers?project_id=${q(projectId)}&check=${check}`), { method: "POST" });
}

const ACTIONS = ["test", "register", "update", "review", "delete", "catalogue_add", "role_tools",
                 "policy", "grant", "revoke", "stdio_template_create", "gateway_create",
                 "gateway_token", "gateway_token_revoke", "gateway_revoke",
                 "oauth_start", "oauth_mine", "credential_policy",
                 "credential_remove", "token_mine", "grant_revoke"] as const;

export async function POST(req: Request) {
  let body: Record<string, unknown>;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }
  const action = String(body.action ?? "");
  if (!ACTIONS.includes(action as (typeof ACTIONS)[number])) {
    return NextResponse.json({ error: `unknown action ${action}` }, { status: 400 });
  }
  const projectId = String(body.project_id ?? "").trim();
  const actor = String(body.actor ?? "").trim();
  if (!projectId || !actor) {
    return NextResponse.json({ error: "project_id and actor are required" }, { status: 400 });
  }
  if (action === "test") {
    return forward("/v1/mcp/servers/test", { method: "POST", body: {
      project_id: projectId, actor, url: String(body.url ?? ""), auth: body.auth ?? null } });
  }
  if (action === "register") {
    return forward("/v1/mcp/servers", { method: "POST", body: {
      ...(body.server as object ?? {}), project_id: projectId, actor } });
  }
  if (action === "catalogue_add") {
    const key = String(body.key ?? "");
    if (!/^[a-z0-9_-]{1,40}$/.test(key)) return NextResponse.json({ error: "bad key" }, { status: 400 });
    return forward(`/v1/mcp/catalogue/${q(key)}`, { method: "POST", body: {
      project_id: projectId, actor, token: body.token ? String(body.token) : null } });
  }
  if (action === "stdio_template_create") {
    // Running a command on BOSS's host: the API holds it to host:exec (Owner/Admin, step-up).
    return forward("/v1/mcp/stdio-templates", { method: "POST", body: {
      ...(body.template as object ?? {}), project_id: projectId, actor } });
  }
  if (action === "gateway_create") {
    return forward("/v1/mcp/gateways", { method: "POST", body: {
      project_id: projectId, actor, name: String(body.name ?? ""), profile: body.profile ?? {},
      allowed_actions: Array.isArray(body.allowed_actions) ? body.allowed_actions.map(String) : null,
      expires_in_days: typeof body.expires_in_days === "number" ? body.expires_in_days : null } });
  }
  if (action === "gateway_token" || action === "gateway_token_revoke" || action === "gateway_revoke") {
    const gatewayId = String(body.gateway_id ?? "");
    if (!ID.test(gatewayId)) return NextResponse.json({ error: "gateway_id is required" }, { status: 400 });
    const base = `/v1/mcp/gateways/${q(gatewayId)}`;
    if (action === "gateway_revoke") {
      return forward(`${base}/revoke`, { method: "POST", body: { project_id: projectId, actor } });
    }
    if (action === "gateway_token") {
      return forward(`${base}/tokens`, { method: "POST", body: {
        project_id: projectId, actor, expires_in_days: typeof body.expires_in_days === "number" ? body.expires_in_days : null,
        label: body.label ? String(body.label) : null } });
    }
    const tokenId = String(body.token_id ?? "");
    if (!ID.test(tokenId)) return NextResponse.json({ error: "token_id is required" }, { status: 400 });
    return forward(`${base}/tokens/${q(tokenId)}/revoke`, { method: "POST", body: { project_id: projectId, actor } });
  }
  if (action === "grant_revoke") {
    const grantId = String(body.grant_id ?? "");
    if (!ID.test(grantId)) return NextResponse.json({ error: "grant_id is required" }, { status: 400 });
    return forward(`/v1/mcp/access-grants/${q(grantId)}/revoke`, { method: "POST", body: {
      project_id: projectId, actor } });
  }
  const serverId = String(body.server_id ?? "");
  if (!ID.test(serverId)) return NextResponse.json({ error: "server_id is required" }, { status: 400 });
  if (action === "oauth_start") {
    // Sign in (BOSSAPP-846): the server's shared sign-in, or one agent's own.
    const subject = body.subject === "agent" ? "agent" : "org";
    return forward(`/v1/mcp/servers/${q(serverId)}/oauth/start`, { method: "POST", body: {
      project_id: projectId, actor, subject, role_key: body.role_key ? String(body.role_key) : null } });
  }
  if (action === "oauth_mine") {
    // Use my own account (BOSSAPP-847): the signed-in person's own sign-in.
    return forward(`/v1/mcp/servers/${q(serverId)}/oauth/mine`, { method: "POST", body: {
      project_id: projectId, actor } });
  }
  if (action === "token_mine") {
    return forward(`/v1/mcp/servers/${q(serverId)}/credentials/mine`, { method: "POST", body: {
      project_id: projectId, actor, token: String(body.token ?? "") } });
  }
  if (action === "credential_policy") {
    return forward(`/v1/mcp/servers/${q(serverId)}/credential-policy`, { method: "POST", body: {
      project_id: projectId, actor, policy: String(body.policy ?? "") } });
  }
  if (action === "credential_remove") {
    const credentialId = String(body.credential_id ?? "");
    if (!ID.test(credentialId)) return NextResponse.json({ error: "credential_id is required" }, { status: 400 });
    const mine = body.mine === true ? "mine/" : "";
    return forward(`/v1/mcp/servers/${q(serverId)}/credentials/${mine}${q(credentialId)}`
      + `?project_id=${q(projectId)}&actor=${q(actor)}`, { method: "DELETE" });
  }
  if (action === "role_tools") {
    // One agent's tools on one server it holds (the drawer's `Tools ▾`, rarely used).
    return forward("/v1/role-mcp-servers", { method: "POST", body: {
      project_id: projectId, actor, role_key: String(body.role_key ?? ""), mcp_server_id: serverId,
      tools: Array.isArray(body.tools) ? body.tools.map(String) : [] } });
  }
  const where = `/v1/mcp/servers/${q(serverId)}`;
  if (action === "policy") {
    return forward(`${where}/policy`, { method: "POST", body: {
      project_id: projectId, actor, tool: String(body.tool ?? ""), policy: String(body.policy ?? "") } });
  }
  if (action === "grant") {
    return forward("/v1/role-mcp-servers", { method: "POST", body: {
      project_id: projectId, actor, role_key: String(body.role_key ?? ""), mcp_server_id: serverId,
      tools: Array.isArray(body.tools) ? body.tools.map(String) : [] } });
  }
  if (action === "revoke") {
    return forward(`/v1/role-mcp-servers?project_id=${q(projectId)}&role_key=${q(String(body.role_key ?? ""))}`
      + `&mcp_server_id=${q(serverId)}&actor=${q(actor)}`, { method: "DELETE" });
  }
  if (action === "review") {
    return forward(`${where}/review`, { method: "POST", body: { project_id: projectId, actor } });
  }
  if (action === "update") {
    return forward(`${where}?project_id=${q(projectId)}`, { method: "PATCH", body: body.changes ?? {} });
  }
  const release = body.release_grants === true ? "&release_grants=true" : "";
  return forward(`${where}?project_id=${q(projectId)}${release}`, { method: "DELETE" });
}
