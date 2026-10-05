/**
 * The Activity log's route handlers share one forwarder: the filter vocabulary is written
 * ONCE here (a param missing from this list is dropped, so a typo cannot widen a query), the
 * console's credentials stay on the server, and the ETag travels both ways so a quiet live tail
 * is a 304 end to end.
 */
import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "./client";
import { apiErrorText } from "./api-error";

/** Everything the log's reads understand. */
export const LOG_FORWARD = [
  "project_id", "q", "kind", "status", "actor", "entity_type", "level", "exclude_level",
  "hide_verbose", "source", "exclude_source", "run_id", "workflow_run_id", "step_id", "agent",
  "exclude_agent", "service", "act", "subject", "since", "until", "range", "sort", "order",
  "limit", "offset", "cursor", "after", "total", "summary", "id", "before", "format", "prefix",
] as const;

export async function forwardLogRead(req: Request, path: string): Promise<Response> {
  const incoming = new URL(req.url).searchParams;
  if (!incoming.get("project_id")?.trim()) {
    return NextResponse.json({ error: "missing project_id" }, { status: 400 });
  }
  const params = new URLSearchParams();
  for (const key of LOG_FORWARD) {
    const value = incoming.get(key)?.trim();
    if (value) params.set(key, value);
  }
  const headers: Record<string, string> = authHeaders({ "Content-Type": "application/json" });
  const etag = req.headers.get("if-none-match");
  if (etag) headers["If-None-Match"] = etag;
  try {
    const r = await fetch(`${API_URL}${path}?${params.toString()}`, { headers, cache: "no-store" });
    const out = new Headers({ "Cache-Control": "no-cache" });
    const tag = r.headers.get("etag");
    if (tag) out.set("ETag", tag);
    if (r.status === 304) return new Response(null, { status: 304, headers: out });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      // The API's own sentence ("Sort by time; …", "step_id needs workflow_run_id…") is shown
      // under the query bar, so it is forwarded as words, never as an object.
      return NextResponse.json({ error: apiErrorText(body, r.status) }, { status: r.status, headers: out });
    }
    return NextResponse.json(body, { headers: out });
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
