import { NextResponse } from "next/server";

import { describeApiError, stepUpCode } from "../../../../lib/api-error";
import { apiWrite } from "../../../../lib/api-write";
import { API_URL, authHeaders } from "../../../../lib/client";

/** Easy Mode's one-click build (BOSSAPP-1273): /v1/factory/launch with the composer's JSON. */
export const dynamic = "force-dynamic";

export async function POST(req: Request) {
  const body = await req.json().catch(() => null);
  if (!body || typeof body !== "object") {
    return NextResponse.json({ error: "describe what to build first" }, { status: 400 });
  }
  try {
    const { target_project_id: target, ...launch } = body;
    if (target !== undefined && (typeof target !== "string" || !/^[a-zA-Z0-9_-]+$/.test(target))) {
      return NextResponse.json({ error: "Choose a valid project." }, { status: 400 });
    }
    const endpoint = target ? `/v1/projects/${encodeURIComponent(target)}/factory/launch` : "/v1/factory/launch";
    const r = await apiWrite(`${API_URL}${endpoint}`, {
      method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(launch), cache: "no-store",
    });
    const data = await r.json().catch(() => ({}));
    // A CONSENT REQUEST PASSES THROUGH AS THE API SENT IT: the console's consent sheet reads
    // `error: "consent_required"` and its agents, and the kept project id rides along for the retry.
    if (r.status === 409 && (data as { error?: unknown }).error === "consent_required") {
      return NextResponse.json(data, { status: 409 });
    }
    if (!r.ok) {
      const sentence = (data as { message?: unknown }).message;
      return NextResponse.json({ ...(data as object),
        error: typeof sentence === "string" && sentence ? sentence : describeApiError(data, r.status),
        ...stepUpCode(data) },
        { status: r.status });
    }
    return NextResponse.json(data, { status: r.status });
  } catch (e) {
    return NextResponse.json({ error: `The build did not reach BOSS: ${(e as Error).message}` }, { status: 502 });
  }
}
