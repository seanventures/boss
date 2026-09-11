import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";

/**
 * The setup wizard's reads and writes, off the Server Action path.
 *
 * Same reason as /api/builds: a Server Action waits on a full route re-render before it can
 * reply, and a wizard step that appears to do nothing for twenty seconds is a wizard people
 * abandon. It also keeps the pasted token on the server side of the boundary — the browser
 * posts it here, this hands it to the API, and it is never a query parameter anywhere.
 */
export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const url = new URL(req.url);
  const view = url.searchParams.get("view") ?? "status";
  const projectId = (url.searchParams.get("project_id") ?? "").trim();
  const path = view === "providers"
    ? "/v1/setup/model-providers"
    : `/v1/setup/status?project_id=${encodeURIComponent(projectId)}`;
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
  const step = String(body.step ?? "").trim();
  // TWO STEPS, TWO UPSTREAMS. Named here rather than taking a path from the client, so this
  // route cannot be used to reach an arbitrary API endpoint with the console's credentials.
  const path = step === "github" ? "/v1/github/easy-connect"
             : step === "model" ? "/v1/setup/model"
             : "";
  if (!path) {
    return NextResponse.json({ error: "step must be 'github' or 'model'" }, { status: 400 });
  }
  try {
    const r = await fetch(`${API_URL}${path}`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body.payload ?? {}),
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
