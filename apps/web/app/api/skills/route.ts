import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";

/**
 * The skills area's reads and writes.
 *
 * Off the Server Action path for the same reason /api/setup is: authoring a skill is an
 * editing loop, and a save that waits on a full route re-render before it can say "saved" is
 * an editing loop people abandon.
 */
export const dynamic = "force-dynamic";

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
  const path = view === "builtin"
    ? `/v1/skills?project_id=${encodeURIComponent(projectId)}`
    : view === "assignments"
      ? `/v1/role-skills?project_id=${encodeURIComponent(projectId)}`
      : view === "mcp"
        ? `/v1/mcp/servers?project_id=${encodeURIComponent(projectId)}`
        : `/v1/skill-definitions?project_id=${encodeURIComponent(projectId)}`;
  try {
    const r = await fetch(`${API_URL}${path}`, {
      headers: authHeaders({ "Content-Type": "application/json" }),
      cache: "no-store",
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      return NextResponse.json({ error: data?.detail ?? `HTTP ${r.status}` },
                               { status: r.status });
    }
    return NextResponse.json(data);
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}

export async function POST(req: Request) {
  let body: Record<string, unknown>;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }
  // DELETE ARRIVES AS A POST, deliberately: one route, one allowlist, and the action named in
  // the body rather than taken from a path this handler would then have to sanitise.
  const action = String(body.action ?? "");
  const remove = action === "delete";
  const key = String(body.skill_key ?? "").trim();
  const projectId = String(body.project_id ?? "").trim();
  const actor = String(body.actor ?? "executive_console");
  if (remove && (!key || !projectId)) {
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
    if (!projectId || !roleKey || !skillId) {
      return NextResponse.json(
        { error: "project_id, role_key and skill_id are required" }, { status: 400 });
    }
    const q = `?project_id=${encodeURIComponent(projectId)}`
      + `&role_key=${encodeURIComponent(roleKey)}`
      + `&skill_id=${encodeURIComponent(skillId)}`
      + `&actor=${encodeURIComponent(actor)}`;
    try {
      const r = await fetch(
        `${API_URL}/v1/role-skills${action === "unassign" ? q : ""}`,
        {
          method: action === "assign" ? "POST" : "DELETE",
          headers: authHeaders({ "Content-Type": "application/json" }),
          ...(action === "assign"
            ? { body: JSON.stringify({ project_id: projectId, role_key: roleKey,
                                       skill_id: skillId, actor }) }
            : {}),
          cache: "no-store",
        });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        return NextResponse.json({ error: data?.detail ?? `HTTP ${r.status}` },
                                 { status: r.status });
      }
      return NextResponse.json(data);
    } catch (e) {
      return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
    }
  }

  const path = remove
    ? `/v1/skill-definitions/${encodeURIComponent(key)}`
      + `?project_id=${encodeURIComponent(projectId)}`
      + `&actor=${encodeURIComponent(actor)}`
    : "/v1/skill-definitions";
  try {
    const r = await fetch(`${API_URL}${path}`, {
      method: remove ? "DELETE" : "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      ...(remove ? {} : { body: JSON.stringify(body) }),
      cache: "no-store",
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      return NextResponse.json({ error: data?.detail ?? `HTTP ${r.status}` },
                               { status: r.status });
    }
    return NextResponse.json(data);
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
