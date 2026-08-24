import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";

/**
 * Retire a deployment target — the "unbind" the console never had.
 *
 * DELETE /v1/deployment-targets/{id} has existed all along and nothing in the console called
 * it, so once an app was bound to a host and port there was no way back: the card offered
 * "Set target" and never an undo, and a wrong binding could only be fixed in the database.
 *
 * A route handler rather than a Server Action, for the same reason as the host list: a Server
 * Action re-renders this route to reply, and that costs 20+ seconds here.
 *
 * The API RETIRES rather than deletes — deployment runs and health samples reference the row,
 * and removing it would erase the history of everything ever shipped there.
 */
export const dynamic = "force-dynamic";

export async function DELETE(req: Request) {
  const targetId = new URL(req.url).searchParams.get("target_id")?.trim();
  if (!targetId) {
    return NextResponse.json({ error: "missing target_id" }, { status: 400 });
  }
  try {
    const r = await fetch(
      `${API_URL}/v1/deployment-targets/${encodeURIComponent(targetId)}`,
      { method: "DELETE", headers: authHeaders({}), cache: "no-store" },
    );
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      return NextResponse.json({ error: data?.detail ?? `HTTP ${r.status}` }, { status: r.status });
    }
    return NextResponse.json(data);
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
