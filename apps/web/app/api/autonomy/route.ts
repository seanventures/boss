import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";
import { apiWrite } from "../../../lib/api-write";
import { apiErrorText } from "../../../lib/api-error";
import { rawRequestUrl } from "../../../lib/raw-request-url";

/**
 * Read and change the gate-autonomy mode.
 *
 * A route handler, not a Server Action: a Server Action re-renders the whole route to reply,
 * which is what made "Set target" appear to hang for 115 seconds. Every mutating control in v2
 * goes through a handler for that reason.
 *
 * The actor is required and never defaulted. Turning off human review is an act with an owner,
 * and an autonomy change with no owner is the one kind this system must never accept — it is
 * the act that makes every later act unattributable.
 */
export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const projectId = rawRequestUrl(req).searchParams.get("project_id")?.trim();
  if (!projectId) {
    return NextResponse.json({ error: "missing project_id" }, { status: 400 });
  }
  try {
    const r = await fetch(
      `${API_URL}/v1/console/v2/autonomy?project_id=${encodeURIComponent(projectId)}`,
      { headers: authHeaders({}), cache: "no-store" },
    );
    if (!r.ok) return NextResponse.json({ error: `HTTP ${r.status}` }, { status: r.status });
    return NextResponse.json(await r.json());
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}

export async function POST(req: Request) {
  const body = (await req.json().catch(() => ({}))) as Record<string, unknown>;
  const actor = String(body.actor ?? "").trim();
  if (!actor) {
    // Refused HERE as well as at the API, so a caller cannot reach the write path without one.
    return NextResponse.json(
      { error: "actor is required for an autonomy change" },
      { status: 400 },
    );
  }
  const apply = Boolean(body.apply);
  const path = apply ? "/v1/console/v2/autonomy/apply" : "/v1/console/v2/autonomy";
  try {
    const r = await apiWrite(`${API_URL}${path}`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
      cache: "no-store",
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      return NextResponse.json(
        { error: apiErrorText(data, r.status) },
        { status: r.status },
      );
    }
    return NextResponse.json(data);
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
