import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";
import { apiWrite } from "../../../lib/api-write";
import { apiErrorText } from "../../../lib/api-error";

/**
 * The channels area's reads and writes.
 *
 * One route, and the action named in the BODY rather than taken from a path this handler would
 * then have to sanitise — the same shape /api/skills uses, for the same reason.
 */
export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const url = new URL(req.url);
  const projectId = (url.searchParams.get("project_id") ?? "").trim();
  const path = url.searchParams.get("view") === "client"
    ? `/v1/channels/youtube/client?project_id=${encodeURIComponent(projectId)}`
    : `/v1/channels?project_id=${encodeURIComponent(projectId)}`;
  try {
    const r = await fetch(`${API_URL}${path}`, {
      headers: authHeaders({ "Content-Type": "application/json" }),
      cache: "no-store",
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      return NextResponse.json({ error: apiErrorText(data, r.status) },
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
  const action = String(body.action ?? "");
  // An allowlist, not a lookup: an action that is not one of these must not reach the API at
  // all, whatever it names.
  const ALLOWED = ["save_client", "connect", "disconnect"] as const;
  if (!ALLOWED.includes(action as (typeof ALLOWED)[number])) {
    return NextResponse.json({ error: `unknown action '${action}'` }, { status: 400 });
  }

  // DISCONNECT IS A DELETE WITH QUERY PARAMS, not a body — so it cannot share the shape below.
  if (action === "disconnect") {
    const q = `?project_id=${encodeURIComponent(String(body.project_id ?? ""))}`
      + `&connection_key=${encodeURIComponent(String(body.connection_key ?? ""))}`
      + `&actor=${encodeURIComponent(String(body.actor ?? "executive_console"))}`;
    try {
      const r = await apiWrite(`${API_URL}/v1/channels/youtube${q}`, {
        method: "DELETE",
        headers: authHeaders({ "Content-Type": "application/json" }),
        cache: "no-store",
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        return NextResponse.json({ error: apiErrorText(data, r.status) },
                                 { status: r.status });
      }
      return NextResponse.json(data);
    } catch (e) {
      return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
    }
  }

  const path = action === "save_client"
    ? "/v1/channels/youtube/client"
    : "/v1/channels/youtube/connect";
  try {
    const r = await apiWrite(`${API_URL}${path}`, {
      method: action === "save_client" ? "PUT" : "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
      cache: "no-store",
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      return NextResponse.json({ error: apiErrorText(data, r.status) },
                               { status: r.status });
    }
    return NextResponse.json(data);
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
