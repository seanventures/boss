import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";
import { rawRequestUrl } from "../../../lib/raw-request-url";

/** Is this machine the reason a run is slow, and what should the operator do about it. */
export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const projectId = (rawRequestUrl(req).searchParams.get("project_id") ?? "").trim();
  try {
    const r = await fetch(
      `${API_URL}/v1/projects/${encodeURIComponent(projectId)}/host-advice`,
      { headers: authHeaders({ "Content-Type": "application/json" }), cache: "no-store" },
    );
    if (!r.ok) return NextResponse.json({ show: false });
    return NextResponse.json(await r.json());
  } catch {
    // Silent: a machine-health hint must never itself become an error on the page.
    return NextResponse.json({ show: false });
  }
}
