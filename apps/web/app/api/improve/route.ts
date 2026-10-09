import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";
import { apiWrite } from "../../../lib/api-write";
import { apiErrorText } from "../../../lib/api-error";
import { IMPROVE_OPS as OPS, IMPROVE_READS as READS } from "../../../lib/improve-ops";
import { rawRequestUrl } from "../../../lib/raw-request-url";

/**
 * Improve BOSS, scorecards, experiments and the person's feedback, off the Server Action path.
 * Every op is NAMED in lib/improve-ops.ts: the browser cannot reach an arbitrary API path with
 * the console's credentials. Downloads (Export batch, the collector CSV) pass through as bytes.
 *
 * GET  ?view=sharing|preview|send|export|scorecards|experiments|prompts|collector|collector_csv|share_bundle
 * POST {op, ...}
 */
export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const q = rawRequestUrl(req).searchParams;
  const view = READS[q.get("view") || ""];
  if (!q.get("project_id")?.trim() || !view) {
    return NextResponse.json({ error: "project_id and a view are required" }, { status: 400 });
  }
  const headers: Record<string, string> = authHeaders();
  const etag = req.headers.get("if-none-match");
  if (etag) headers["If-None-Match"] = etag;
  try {
    const r = view.load
      ? await apiWrite(`${API_URL}${view.path(q)}`, { method: "POST", headers, cache: "no-store" })
      : await fetch(`${API_URL}${view.path(q)}`, { headers, cache: "no-store" });
    const out = new Headers({ "Cache-Control": "no-cache" });
    const tag = r.headers.get("etag");
    if (tag) out.set("ETag", tag);
    if (r.status === 304) return new Response(null, { status: 304, headers: out });
    if (view.raw && r.ok) {
      for (const h of ["content-type", "content-disposition"]) {
        const v = r.headers.get(h);
        if (v) out.set(h, v);
      }
      return new Response(await r.arrayBuffer(), { status: 200, headers: out });
    }
    const body = await r.json().catch(() => ({}));
    if (!r.ok) return NextResponse.json({ error: apiErrorText(body, r.status) }, { status: r.status, headers: out });
    return NextResponse.json(body, { headers: out });
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}

export async function POST(req: Request) {
  let body: Record<string, unknown>;
  try { body = await req.json(); } catch { return NextResponse.json({ error: "invalid JSON body" }, { status: 400 }); }
  const op = OPS[String(body.op ?? "")];
  if (!op) return NextResponse.json({ error: `op must be one of: ${Object.keys(OPS).join(", ")}` }, { status: 400 });
  try {
    const r = await apiWrite(`${API_URL}${op.path(body)}`, {
      method: op.method, headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(op.body(body)), cache: "no-store",
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) return NextResponse.json({ error: apiErrorText(data, r.status) }, { status: r.status });
    return NextResponse.json(data);
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
