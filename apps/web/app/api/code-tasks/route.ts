import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";

/**
 * The code-task list, on demand.
 *
 * The page renders the newest 200 server-side, which is a deliberate ceiling: measured on
 * shared-server, `/v1/code-tasks?limit=200` is 1.41 MB while every other feed on the page combined
 * is ~130 KB, and one load took 111 seconds and starved the API's healthcheck until Traefik
 * evicted the container. Paying that on every render to cover the tail is the wrong trade.
 *
 * So the tail is fetched only when somebody asks for it. A route handler rather than a Server
 * Action because the reply must not re-render the console tree — v1 takes seconds to paint,
 * and "show me the rest" should not cost a repaint of everything.
 */
export const dynamic = "force-dynamic";

/** The API's own ceiling. Asking beyond it wastes a round trip to be told the same thing. */
const MAX = 2000;

export async function GET(req: Request) {
  const params = new URL(req.url).searchParams;
  const projectId = params.get("project_id")?.trim();
  if (!projectId) {
    return NextResponse.json({ error: "missing project_id" }, { status: 400 });
  }
  const asked = Number(params.get("limit") ?? 400);
  const limit = Math.min(Number.isFinite(asked) && asked > 0 ? asked : 400, MAX);
  try {
    const r = await fetch(
      `${API_URL}/v1/code-tasks?project_id=${encodeURIComponent(projectId)}`
        + `&limit=${limit}&exclude_internal=true`,
      { headers: authHeaders({}), cache: "no-store" },
    );
    if (!r.ok) {
      return NextResponse.json({ error: `API returned ${r.status}` }, { status: r.status });
    }
    const d = (await r.json()) as Record<string, unknown>;
    return NextResponse.json({
      code_tasks: d.code_tasks ?? [],
      total: d.total ?? 0,
      truncated: Boolean(d.truncated),
    });
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
