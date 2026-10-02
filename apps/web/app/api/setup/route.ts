import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";
import { apiWrite } from "../../../lib/api-write";
import { apiErrorText } from "../../../lib/api-error";

/**
 * The setup wizard's reads and writes, off the Server Action path.
 *
 * Same reason as /api/builds: a Server Action waits on a full route re-render before it can
 * reply, and a wizard step that appears to do nothing for twenty seconds is a wizard people
 * abandon. It also keeps the pasted token on the server side of the boundary — the browser
 * posts it here, this hands it to the API, and it is never a query parameter anywhere.
 */
export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const url = new URL(req.url);
  const view = url.searchParams.get("view") ?? "status";
  const projectId = (url.searchParams.get("project_id") ?? "").trim();
  const path = view === "providers"
    ? "/v1/setup/model-providers"
    // Which coding-agent CLIs this machine already has, what each still needs, and which of
    // them was already connected. There is no credential in this answer and none is asked
    // for: a subscription is an OAuth session the CLI holds, so detection is most of it.
    // project_id travels because the recorded connection is per project — without it the
    // answer is live detection only, which is what made a saved connection look undone.
    // Which model answers this project's typed decisions, and whether a Jev key is held —
    // never the key itself.
    : view === "decision-model"
    ? `/v1/setup/decision-model?project_id=${encodeURIComponent(projectId)}`
    : view === "coding-agents"
    ? `/v1/setup/coding-agents?project_id=${encodeURIComponent(projectId)}`
    : `/v1/setup/status?project_id=${encodeURIComponent(projectId)}`;
  try {
    const r = await fetch(`${API_URL}${path}`, {
      headers: authHeaders({ "Content-Type": "application/json" }),
      cache: "no-store",
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      return NextResponse.json({ error: apiErrorText(data, r.status) },
                               { status: r.status });
    }
    return NextResponse.json(data);
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}

export async function POST(req: Request) {
  let body: Record<string, unknown>;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }
  const step = String(body.step ?? "").trim();
  // TWO STEPS, TWO UPSTREAMS. Named here rather than taking a path from the client, so this
  // route cannot be used to reach an arbitrary API endpoint with the console's credentials.
  const path = step === "github" ? "/v1/github/easy-connect"
             : step === "model" ? "/v1/setup/model"
             : step === "coding-agent" ? "/v1/setup/coding-agents/connect"
             // A subscription whose credential TRAVELS — claude_code's headless token. BOSS
             // chooses the store, so the operator hands the value over once here instead of
             // editing an env file on the API container and restarting it.
             : step === "coding-agent-credential" ? "/v1/setup/coding-agents/credential"
             // An optional TypeSafe (Jev) key for typed decisions. Proved, then stored by BOSS.
             : step === "decision-key" ? "/v1/setup/decision-model/key"
             : "";
  if (!path) {
    return NextResponse.json(
      { error: "step must be 'github', 'model', 'coding-agent', "
               + "'coding-agent-credential' or 'decision-key'" }, { status: 400 });
  }
  try {
    const r = await apiWrite(`${API_URL}${path}`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body.payload ?? {}),
      cache: "no-store",
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      // A detail can be a SENTENCE or a DECISION. The credential route answers 409 with an
      // object naming what to resend, and flattening that to a string would leave the console
      // unable to offer the choice it describes.
      const detail = data?.detail;
      return NextResponse.json(
        typeof detail === "object" && detail !== null
          ? { error: apiErrorText(data, r.status),
              decision: detail }
          : { error: apiErrorText(data, r.status) },
        { status: r.status });
    }
    return NextResponse.json(data);
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
