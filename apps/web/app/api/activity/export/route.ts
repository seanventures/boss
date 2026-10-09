import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../../lib/client";
import { LOG_FORWARD } from "../../../../lib/activity-proxy";
import { rawRequestUrl } from "../../../../lib/raw-request-url";

/**
 * The current filter as a file (GET /v1/activity/export): streamed through, never buffered —
 * a 50,000-line export must not sit in this process's memory. Redaction and the cap happen in
 * the API; `X-Truncated` says when the cap was reached.
 */
export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const incoming = rawRequestUrl(req).searchParams;
  if (!incoming.get("project_id")?.trim()) {
    return NextResponse.json({ error: "missing project_id" }, { status: 400 });
  }
  const params = new URLSearchParams();
  for (const key of LOG_FORWARD) {
    const value = incoming.get(key)?.trim();
    if (value) params.set(key, value);
  }
  try {
    const r = await fetch(`${API_URL}/v1/activity/export?${params.toString()}`, {
      headers: authHeaders(), cache: "no-store",
    });
    if (!r.ok || !r.body) {
      return NextResponse.json({ error: `BOSS answered ${r.status}` }, { status: r.status || 502 });
    }
    const headers = new Headers();
    for (const name of ["content-type", "content-disposition", "x-truncated", "x-row-count"]) {
      const value = r.headers.get(name);
      if (value) headers.set(name, value);
    }
    return new Response(r.body, { status: 200, headers });
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
