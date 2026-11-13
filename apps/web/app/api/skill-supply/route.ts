import { NextResponse } from "next/server";

import { describeApiError } from "../../../lib/api-error";
import { API_URL, authHeaders } from "../../../lib/client";
import { apiWrite, type WriteMethod } from "../../../lib/api-write";
import { rawRequestUrl } from "../../../lib/raw-request-url";

/**
 * A skill's supply chain (BOSSAPP-841..844): its History (versions, diff, restore, publish), its
 * Tests (cases, runs, grades) and Import from a source (GitHub, skills.sh, a URL).
 *
 * One door like /api/skills: reads by `view`, writes as a POST naming an allowlisted action.
 */
export const dynamic = "force-dynamic";

async function forward(path: string, init: { method: "GET" | WriteMethod; body?: unknown }) {
  try {
    const headers = authHeaders({ "Content-Type": "application/json" });
    const body = init.body === undefined ? {} : { body: JSON.stringify(init.body) };
    const r = init.method === "GET"
      ? await fetch(`${API_URL}${path}`, { headers, cache: "no-store" })
      : await apiWrite(`${API_URL}${path}`,
                       { method: init.method, headers, ...body, cache: "no-store" });
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
  const url = rawRequestUrl(req);
  const p = url.searchParams;
  const projectId = (p.get("project_id") ?? "").trim();
  const key = (p.get("skill_key") ?? "").trim();
  const view = p.get("view") ?? "versions";
  const project = `project_id=${q(projectId)}`;
  if (view === "sources") return forward(`/v1/skill-sources?${project}`, { method: "GET" });
  if (view === "consents") return forward(`/v1/skill-consents?${project}`, { method: "GET" });
  if (!key) return NextResponse.json({ error: "skill_key is required" }, { status: 400 });
  if (view === "diff") {
    return forward(`/v1/skill-definitions/${q(key)}/diff?${project}`
      + `&from_version=${q(p.get("from") ?? "")}&to_version=${q(p.get("to") ?? "")}`, { method: "GET" });
  }
  if (view === "tests") {
    const version = p.get("version");
    return forward(`/v1/skill-definitions/${q(key)}/tests?${project}`
      + (version ? `&version=${q(version)}` : ""), { method: "GET" });
  }
  if (view === "versions") return forward(`/v1/skill-definitions/${q(key)}/versions?${project}`, { method: "GET" });
  return NextResponse.json({ error: `unknown view ${view}` }, { status: 400 });
}

export async function POST(req: Request) {
  let body: Record<string, unknown>;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }
  const action = String(body.action ?? "");
  const projectId = String(body.project_id ?? "").trim();
  const actor = String(body.actor ?? "").trim();
  const key = String(body.skill_key ?? "").trim();
  if (!actor) return NextResponse.json({ error: "actor is required" }, { status: 400 });
  if (!projectId) return NextResponse.json({ error: "project_id is required" }, { status: 400 });
  const base = { project_id: projectId, actor };
  const skillPath = `/v1/skill-definitions/${q(key)}`;
  const needKey = ["restore", "publish", "add_case", "delete_case", "run_tests"];
  if (needKey.includes(action) && !key) {
    return NextResponse.json({ error: "skill_key is required" }, { status: 400 });
  }
  switch (action) {
    case "restore":
    case "publish":
      return forward(`${skillPath}/${action}`, { method: "POST", body: { ...base, version: body.version } });
    case "add_case":
      return forward(`${skillPath}/tests/cases`, {
        method: "POST", body: { ...base, name: body.name, input: body.input ?? {} } });
    case "delete_case":
      return forward(`${skillPath}/tests/cases/${q(String(body.case_id ?? ""))}?project_id=${q(projectId)}`
        + `&actor=${q(actor)}`, { method: "DELETE" });
    case "run_tests":
      return forward(`${skillPath}/tests/run`, { method: "POST", body: { ...base, version: body.version ?? null } });
    case "grade":
      return forward(`/v1/skill-test-runs/${q(String(body.run_id ?? ""))}/grade`, {
        method: "POST", body: { ...base, grade: body.grade, notes: body.notes ?? "" } });
    case "preview_source":
    case "import_source":
      return forward(action === "preview_source" ? "/v1/skill-sources/preview" : "/v1/skill-sources", {
        method: "POST",
        body: { ...base, kind: body.kind, locator: body.locator, ref: body.ref ?? "", pin: body.pin ?? "",
                skill_key: body.skill_key || null, auto_update: body.auto_update === true } });
    case "sync_source":
      return forward(`/v1/skill-sources/${q(String(body.source_id ?? ""))}/sync`, { method: "POST", body: base });
    default:
      return NextResponse.json({ error: `unknown action ${action}` }, { status: 400 });
  }
}
