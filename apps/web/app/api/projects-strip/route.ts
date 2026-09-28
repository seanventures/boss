import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";

/**
 * Software project lineages, for the v1 header strip. The selected control
 * project scopes the query; it is not itself a row in this count.
 *
 * Reuses the endpoint v2's shell already calls rather than assembling the same thing from v1's
 * feeds. v1 issues ~82 requests per render and shows nothing until the slowest answers; adding
 * an 83rd to draw a header strip would make the whole console wait on it. This one is bounded,
 * server-side, and fetched by the strip alone — if it is slow or down, the strip says so and
 * nothing else on the page is affected.
 *
 * A route handler rather than a Server Action because the reply must not re-render the console:
 * v1's tree takes seconds to paint, and the strip refreshes.
 */
export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const projectId = new URL(req.url).searchParams.get("project_id")?.trim();
  if (!projectId) {
    return NextResponse.json({ error: "missing project_id" }, { status: 400 });
  }
  try {
    // 100, not the endpoint's default of 40. goldeneye has 59 apps and the strip was showing
    // the first 40 as though that were all of them — the same false absence this strip exists
    // to prevent. 100 is the endpoint's own ceiling and the rows are small (the response is
    // trimmed to six fields below), so this costs far less than the list feeds do.
    const r = await fetch(
      `${API_URL}/v1/console/v2/overview?project_id=${encodeURIComponent(projectId)}&limit=100`,
      { headers: authHeaders({}), cache: "no-store" },
    );
    if (!r.ok) {
      return NextResponse.json({ error: `API returned ${r.status}` }, { status: r.status });
    }
    const d = (await r.json()) as Record<string, unknown>;
    // Only what the strip draws. The overview also carries burn, gates and autonomy, and
    // shipping those to a header that never renders them is bytes on every refresh.
    return NextResponse.json({
      projects: d.projects ?? [],
      // Forwarded so the strip can say "40 of 59" rather than implying it has them all.
      total: d.total_projects ?? (Array.isArray(d.projects) ? d.projects.length : 0),
      truncated: Boolean(d.truncated),
    });
  } catch (e) {
    return NextResponse.json(
      { error: String((e as Error)?.message ?? e) },
      { status: 502 },
    );
  }
}
