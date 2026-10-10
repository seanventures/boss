import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";
import { apiWrite } from "../../../lib/api-write";
import { apiErrorText } from "../../../lib/api-error";
import { rawRequestUrl } from "../../../lib/raw-request-url";

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

/**
 * The target list. THIS DID NOT EXIST, and its absence broke a different feature entirely.
 *
 * The route implemented only DELETE, so the workflow view's `GET /api/deployment-targets`
 * answered 405 — and because both loads sat in one async function whose targets branch ended
 * `if (!res.ok) return;`, that non-ok reply exited the function before the HOSTS fetch ran.
 * The deploy step then reported "this project has no verified host" about a host that was
 * verified and ready. One missing handler, and the symptom appeared somewhere else entirely.
 */
export async function GET(req: Request) {
  const projectId = rawRequestUrl(req).searchParams.get("project_id")?.trim();
  if (!projectId) {
    return NextResponse.json({ error: "missing project_id" }, { status: 400 });
  }
  try {
    const r = await fetch(
      `${API_URL}/v1/deployment-targets?project_id=${encodeURIComponent(projectId)}`,
      { headers: authHeaders({}), cache: "no-store" },
    );
    if (!r.ok) {
      return NextResponse.json({ error: `HTTP ${r.status}` }, { status: r.status });
    }
    return NextResponse.json(await r.json());
  } catch (e) {
    return NextResponse.json(
      { error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}


export async function DELETE(req: Request) {
  const url = rawRequestUrl(req);
  const targetId = url.searchParams.get("target_id")?.trim();
  const actor = url.searchParams.get("actor")?.trim();
  if (!targetId) {
    return NextResponse.json({ error: "missing target_id" }, { status: 400 });
  }
  if (!actor) {
    return NextResponse.json({ error: "actor is required" }, { status: 422 });
  }
  try {
    const r = await apiWrite(
      `${API_URL}/v1/deployment-targets/${encodeURIComponent(targetId)}`
        + `?actor=${encodeURIComponent(actor)}`,
      { method: "DELETE", headers: authHeaders({}), cache: "no-store" },
    );
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      return NextResponse.json({ error: apiErrorText(data, r.status) }, { status: r.status });
    }
    return NextResponse.json(data);
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
