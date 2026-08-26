import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";

/**
 * Talk to an agent, from the v2 chat drawer.
 *
 * A route handler rather than a Server Action, and here the reason is sharper than usual: a
 * Server Action re-renders the whole route to reply, and v1's chat lost the answer the instant
 * it arrived because `revalidatePath("/")` snapped the panel back to its empty state — question
 * sent, "3s" ticking, then nothing, as though it had never been asked.
 *
 * A chat that loses the reply it just received is worse than one that is slow.
 */
export const dynamic = "force-dynamic";

export async function POST(req: Request) {
  const body = (await req.json().catch(() => ({}))) as Record<string, unknown>;
  const message = String(body.message ?? "").trim();
  const projectId = String(body.project_id ?? "").trim();
  const actor = String(body.actor ?? "").trim();

  if (!projectId) {
    return NextResponse.json({ error: "missing project_id" }, { status: 400 });
  }
  if (!message) {
    return NextResponse.json({ error: "nothing to say" }, { status: 400 });
  }
  if (!actor) {
    // Never invented. Defaulting to "operator" once produced "Authenticated principal 'ceo' is
    // not allowed to act as 'operator'" — correct, because claiming a principal you are not is
    // impersonation whether or not it was meant.
    return NextResponse.json({ error: "actor is required" }, { status: 400 });
  }

  try {
    const r = await fetch(`${API_URL}/v1/engine/agent/chat`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        project_id: projectId,
        actor,
        message,
        surface: "chat",
        thread_id: String(body.thread_id ?? "").trim() || undefined,
        // Scopes the turn to one agent, so a conversation with the CTO stays with the CTO
        // rather than joining one shared thread.
        external_ref: String(body.agent_id ?? "").trim() || undefined,
      }),
      cache: "no-store",
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      return NextResponse.json(
        { error: (data as { detail?: string })?.detail ?? `HTTP ${r.status}` },
        { status: r.status },
      );
    }
    return NextResponse.json(data);
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
