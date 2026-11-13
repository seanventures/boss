import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../../lib/client";
import { rawRequestUrl } from "../../../../lib/raw-request-url";
import { TOOL_CALL_FORWARD } from "../../../../lib/tool-calls";

/**
 * The current tool-call filter as NDJSON (GET /v1/tool-calls/export): streamed through, never
 * buffered. The cap and the redaction happen in the API.
 */
export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const incoming = rawRequestUrl(req).searchParams;
  if (!incoming.get("project_id")?.trim()) {
    return NextResponse.json({ error: "missing project_id" }, { status: 400 });
  }
  const params = new URLSearchParams();
  for (const key of TOOL_CALL_FORWARD) {
    if (key === "cursor" || key === "limit") continue;
    const value = incoming.get(key)?.trim();
    if (value) params.set(key, value);
  }
  try {
    const r = await fetch(`${API_URL}/v1/tool-calls/export?${params.toString()}`, {
      headers: authHeaders(), cache: "no-store",
    });
    if (!r.ok || !r.body) {
      return NextResponse.json({ error: `BOSS answered ${r.status}` }, { status: r.status || 502 });
    }
    const headers = new Headers();
    for (const name of ["content-type", "content-disposition"]) {
      const value = r.headers.get(name);
      if (value) headers.set(name, value);
    }
    return new Response(r.body, { status: 200, headers });
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
