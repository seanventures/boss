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
  const path = url.searchParams.get("view") === "builtin"
    ? `/v1/skills?project_id=${encodeURIComponent(projectId)}`
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
  const remove = body.action === "delete";
  const key = String(body.skill_key ?? "").trim();
  const projectId = String(body.project_id ?? "").trim();
  if (remove && (!key || !projectId)) {
    return NextResponse.json({ error: "skill_key and project_id are required" }, { status: 400 });
  }
  const path = remove
    ? `/v1/skill-definitions/${encodeURIComponent(key)}`
      + `?project_id=${encodeURIComponent(projectId)}`
      + `&actor=${encodeURIComponent(String(body.actor ?? "executive_console"))}`
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
