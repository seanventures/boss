import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";
import { apiWrite } from "../../../lib/api-write";
import { apiErrorText } from "../../../lib/api-error";
import { GOVERN_OPS as OPS } from "../../../lib/govern-ops";
import { rawRequestUrl } from "../../../lib/raw-request-url";

/**
 * Govern's reads and writes, off the Server Action path (a Server Action waits on a full
 * re-render of the console). Every op is NAMED here — the browser cannot reach an arbitrary API
 * path with the console's credentials — and each one sends the request the old Govern
 * components sent: the same method, path and body (tests/govern-writes-what-it-wrote-before),
 * plus the version the screen showed as `If-Match`.
 *
 * GET  ?project_id=&view=limits|spend|envelopes|daily|project_limits[&window=]
 * POST {op, ...}  setting · pace · pace_preview · cap_preview · monthly_cap · token_cap ·
 *                 reconcile · initiative · limits · project_limit
 */
export const dynamic = "force-dynamic";

const str = (v: unknown) => (v === undefined || v === null ? "" : String(v));
const enc = (v: unknown) => encodeURIComponent(str(v));

const READS: Record<string, (q: URLSearchParams) => string> = {
  limits: (q) => `/v1/govern?project_id=${enc(q.get("project_id"))}`,
  spend: (q) => `/v1/govern/spend?project_id=${enc(q.get("project_id"))}&window=${enc(q.get("window") || "7d")}`,
  envelopes: (q) => `/v1/govern/envelopes?project_id=${enc(q.get("project_id"))}`,
  daily: (q) => `/v1/budget/daily?project_id=${enc(q.get("project_id"))}&days=30`,
  project_limits: (q) => `/v1/govern/project-limits?project_id=${enc(q.get("project_id"))}`,
};

export async function GET(req: Request) {
  const q = rawRequestUrl(req).searchParams;
  const view = READS[q.get("view") || "limits"];
  if (!q.get("project_id")?.trim() || !view) {
    return NextResponse.json({ error: "project_id and a view are required" }, { status: 400 });
  }
  const headers: Record<string, string> = authHeaders();
  const etag = req.headers.get("if-none-match");
  if (etag) headers["If-None-Match"] = etag;
  try {
    const r = await fetch(`${API_URL}${view(q)}`, { headers, cache: "no-store" });
    const out = new Headers({ "Cache-Control": "no-cache" });
    const tag = r.headers.get("etag");
    if (tag) out.set("ETag", tag);
    if (r.status === 304) return new Response(null, { status: 304, headers: out });
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
  const op = OPS[str(body.op)];
  if (!op) return NextResponse.json({ error: `op must be one of: ${Object.keys(OPS).join(", ")}` }, { status: 400 });
  const headers: Record<string, string> = authHeaders({ "Content-Type": "application/json" });
  const ifMatch = req.headers.get("if-match");
  if (ifMatch) headers["If-Match"] = ifMatch;
  try {
    const r = await apiWrite(`${API_URL}${op.path(body)}`, {
      method: op.method, headers, body: JSON.stringify(op.body(body)), cache: "no-store",
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      const detail = (data as { detail?: unknown }).detail;
      return NextResponse.json(
        typeof detail === "object" && detail !== null && !Array.isArray(detail)
          ? { error: apiErrorText({ detail: (detail as { message?: unknown }).message ?? detail }, r.status), refusal: detail }
          : { error: apiErrorText(data, r.status) },
        { status: r.status });
    }
    return NextResponse.json(data);
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
