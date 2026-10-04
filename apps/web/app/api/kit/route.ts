import { NextResponse } from "next/server";

import { describeApiError } from "../../../lib/api-error";
import { API_URL, authHeaders } from "../../../lib/client";
import { apiWrite, type WriteMethod } from "../../../lib/api-write";

/**
 * The Kit's reads and writes (Skills & MCP, skills-mcp spec §6.3).
 *
 * One GET, one POST with an action ALLOWLIST — the shape /api/skills uses: the action is matched
 * against literals, never taken from a path this handler would have to sanitise, and an ability
 * key goes into a path only after it is checked against the shape the kit file uses. A 422 from
 * Save & test carries {field, message}; it is passed through as `field` so the drawer can put the
 * sentence under the input the provider objected to.
 */
export const dynamic = "force-dynamic";

const q = encodeURIComponent;
const KEY = /^[a-z][a-z0-9_]{1,40}$/;

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
      const field = detail && typeof detail === "object" && "field" in (detail as object)
        ? (detail as { field?: string | null }).field ?? null : null;
      const message = detail && typeof detail === "object" && "message" in (detail as object)
        ? String((detail as { message?: string }).message) : describeApiError(data, r.status);
      return NextResponse.json({ error: message, field }, { status: r.status });
    }
    return NextResponse.json(data);
  } catch (e) {
    return NextResponse.json(
      { error: `Could not reach the BOSS API: ${String((e as Error)?.message ?? e)}` },
      { status: 502 });
  }
}

export async function GET(req: Request) {
  const url = new URL(req.url);
  const projectId = (url.searchParams.get("project_id") ?? "").trim();
  if (!projectId) return NextResponse.json({ error: "project_id is required" }, { status: 400 });
  const ability = (url.searchParams.get("ability") ?? "").trim();
  if (ability) {
    if (!KEY.test(ability)) return NextResponse.json({ error: "bad ability" }, { status: 400 });
    return forward(`/v1/kit/${q(ability)}?project_id=${q(projectId)}`, { method: "GET" });
  }
  const check = url.searchParams.get("check") === "none" ? "none" : "stale";
  return forward(`/v1/kit?project_id=${q(projectId)}&check=${check}`, { method: "GET" });
}

const ACTIONS = ["connect", "disconnect", "test", "test_all", "try", "holders", "policy",
                 "enabled", "recommended", "test_crew"] as const;
type Action = (typeof ACTIONS)[number];

export async function POST(req: Request) {
  let body: Record<string, unknown>;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }
  const action = String(body.action ?? "") as Action;
  if (!ACTIONS.includes(action)) {
    return NextResponse.json({ error: `unknown action ${action}` }, { status: 400 });
  }
  const projectId = String(body.project_id ?? "").trim();
  const actor = String(body.actor ?? "").trim();
  if (!projectId || !actor) {
    return NextResponse.json({ error: "project_id and actor are required" }, { status: 400 });
  }
  const base = { project_id: projectId, actor };
  if (action === "recommended") {
    return forward("/v1/kit/recommended", { method: "POST", body: { ...base, roles: body.roles ?? null } });
  }
  if (action === "test_crew") {
    return forward("/v1/kit/test-crew", { method: "POST", body: { ...base, live: body.live === true } });
  }
  if (action === "test_all") return forward("/v1/kit/test-all", { method: "POST", body: base });
  const ability = String(body.ability ?? "").trim();
  if (!KEY.test(ability)) return NextResponse.json({ error: "ability is required" }, { status: 400 });
  const at = `/v1/kit/${q(ability)}`;
  switch (action) {
    case "connect":
      return forward(`${at}/connect`, { method: "POST", body: {
        ...base, provider: String(body.provider ?? ""), fields: body.fields ?? {} } });
    case "disconnect":
      return forward(`${at}/connection?project_id=${q(projectId)}&actor=${q(actor)}`, { method: "DELETE" });
    case "test":
      return forward(`${at}/test`, { method: "POST", body: base });
    case "try":
      return forward(`${at}/try`, { method: "POST", body: { ...base, input: String(body.input ?? "") } });
    case "holders":
      return forward(`${at}/holders`, { method: "PUT", body: {
        ...base, roles: Array.isArray(body.roles) ? body.roles.map(String) : [] } });
    case "policy":
      return forward(`${at}/policy`, { method: "POST", body: {
        ...base, tool_ref: String(body.tool_ref ?? ""), policy: String(body.policy ?? "") } });
    case "enabled":
      return forward(`${at}/enabled`, { method: "POST", body: { ...base, enabled: body.enabled === true } });
  }
  return NextResponse.json({ error: `unknown action ${action}` }, { status: 400 });
}
