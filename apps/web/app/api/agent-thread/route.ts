import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";

/** A thread id before the first message.
 *
 *  The progress line polls `agent_progress`, which is keyed by thread — and on a new chat the
 *  console only learned the id from the turn's response, i.e. after the turn was over. So the
 *  operator watched one frozen sentence for the whole turn while BOSS wrote a perfectly good
 *  trail into a table the page could not address yet.
 *
 *  Deliberately does nothing else: no message, no model call, no writes beyond the thread row.
 */
export async function POST(req: Request) {
  let body: { project_id?: string; actor?: string };
  try {
    body = (await req.json()) as { project_id?: string; actor?: string };
  } catch {
    return NextResponse.json({ error: "invalid JSON" }, { status: 400 });
  }
  const project_id = (body?.project_id ?? "").trim();
  const actor = (body?.actor ?? "").trim();
  if (!project_id || !actor) {
    return NextResponse.json({ error: "project_id and actor are required" }, { status: 400 });
  }
  try {
    const r = await fetch(`${API_URL}/v1/agent/threads/ensure`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ project_id, actor }),
      cache: "no-store",
    });
    if (!r.ok) {
      return NextResponse.json({ error: `API returned ${r.status}` }, { status: r.status });
    }
    return NextResponse.json(await r.json());
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
