import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";
import { apiWrite } from "../../../lib/api-write";
import { refusalFrom } from "../../../lib/start-refusal";

/**
 * How this project sends email — read and set from the console.
 *
 * The readiness check told an operator with no mail server to "POST /v1/email/config", because
 * there was no screen for it (review of the readiness change). The newsletter, sales-signal and
 * trend examples all end in an email, so the first thing they needed was a form. This proxies
 * the existing API: the password goes to the secret backend there, and is never read back.
 */
export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const project = new URL(req.url).searchParams.get("project_id")?.trim() ?? "";
  if (!project) return NextResponse.json({ error: "project_id is required" }, { status: 400 });
  try {
    const r = await fetch(
      `${API_URL}/v1/email/config?project_id=${encodeURIComponent(project)}`,
      { headers: authHeaders({}), cache: "no-store" });
    const body = await r.json().catch(() => ({}));
    return r.ok ? NextResponse.json(body)
      : NextResponse.json(refusalFrom(r.status, body), { status: r.status });
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}

export async function POST(req: Request) {
  let body: Record<string, unknown>;
  try {
    body = (await req.json()) as Record<string, unknown>;
  } catch {
    return NextResponse.json({ error: "body must be JSON" }, { status: 400 });
  }
  try {
    const r = await apiWrite(`${API_URL}/v1/email/config`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
      cache: "no-store",
    });
    const parsed = await r.json().catch(() => ({}));
    return r.ok ? NextResponse.json(parsed)
      : NextResponse.json(refusalFrom(r.status, parsed), { status: r.status });
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
