import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";

/**
 * The host list, as a route handler rather than a Server Action.
 *
 * This was a Server Action, and that is why it appeared to hang. A Server Action does not just
 * return its value: the App Router re-renders the current route and ships a fresh RSC payload
 * with the response. The console takes 20-27 seconds to render, so EVERY server action on this
 * page cost a full re-render before the caller saw anything — the lookup itself answers in
 * about two seconds.
 *
 * That is why the symptom moved around and why the API always measured fine: the API WAS fine,
 * and so was the web container, and so was the network. The cost was in the reply path.
 *
 * A route handler returns JSON and nothing else. Same origin, same auth, no re-render.
 */
export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const projectId = new URL(req.url).searchParams.get("project_id")?.trim();
  if (!projectId) {
    return NextResponse.json({ error: "missing project_id" }, { status: 400 });
  }
  try {
    const r = await fetch(
      `${API_URL}/v1/deployment-hosts?project_id=${encodeURIComponent(projectId)}`,
      { headers: authHeaders({}), cache: "no-store" },
    );
    if (!r.ok) {
      return NextResponse.json({ error: `HTTP ${r.status}` }, { status: r.status });
    }
    return NextResponse.json(await r.json());
  } catch (e) {
    return NextResponse.json(
      { error: String((e as Error)?.message ?? e) },
      { status: 502 },
    );
  }
}
