import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../../lib/client";
import { apiWrite } from "../../../../lib/api-write";
import { refusalFrom } from "../../../../lib/start-refusal";

/**
 * Talking with agents: the console's door to `/v1/conversations/*` (lib/agent-converse.ts).
 *
 * The turn, summary and propose replies are Server-Sent Events, passed through as a stream —
 * a proxy that buffers would show a room's five answers at once, after the last one, which is
 * the opposite of a room. Everything else is JSON. A refusal is flattened ONCE, here, into a
 * sentence plus the parts the console acts on: `code` (needs_model, budget, busy, …) and, when
 * Start is refused, `readiness` — Start's own checklist, for "Start anyway".
 */
export const dynamic = "force-dynamic";

const SSE_HEADERS = {
  "Content-Type": "text/event-stream",
  "Cache-Control": "no-cache, no-transform",
  Connection: "keep-alive",
  "X-Accel-Buffering": "no",
} as const;

/** Only what the contract names: a path segment cannot smuggle a different API route. */
const SEGMENT = /^[A-Za-z0-9_-]{1,80}$/;

function target(path: string[] | undefined, search = ""): string | null {
  const parts = path ?? [];
  if (!parts.every((p) => SEGMENT.test(p))) return null;
  return `${API_URL}/v1/conversations${parts.length ? `/${parts.join("/")}` : ""}${search}`;
}

async function answer(r: Response): Promise<Response> {
  const raw = await r.text();
  let parsed: unknown;
  try { parsed = JSON.parse(raw); } catch { parsed = { detail: raw }; }
  if (r.ok) return NextResponse.json(parsed, { status: r.status });
  const body = (parsed ?? {}) as { detail?: unknown };
  const detail = (body.detail && typeof body.detail === "object" && !Array.isArray(body.detail))
    ? body.detail as Record<string, unknown> : {};
  const refused = refusalFrom(r.status, parsed);
  return NextResponse.json(
    { error: refused.error, ...(typeof detail.code === "string" ? { code: detail.code } : {}),
      ...(refused.readiness ? { readiness: refused.readiness } : {}) },
    { status: r.status });
}

export async function GET(req: Request, ctx: { params: Promise<{ path?: string[] }> }) {
  const { path } = await ctx.params;
  const url = target(path, new URL(req.url).search);
  if (!url) return NextResponse.json({ error: "Not a conversation path." }, { status: 400 });
  try {
    return await answer(await fetch(url, { headers: authHeaders({}), cache: "no-store" }));
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}

export async function POST(req: Request, ctx: { params: Promise<{ path?: string[] }> }) {
  const { path } = await ctx.params;
  const url = target(path);
  if (!url) return NextResponse.json({ error: "Not a conversation path." }, { status: 400 });
  const body = await req.text();
  let upstream: Response;
  try {
    upstream = await apiWrite(url, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json", Accept: "text/event-stream, application/json" }),
      body,
      cache: "no-store",
      // A room can talk for minutes; when the operator closes it, stop listening.
      signal: req.signal,
    });
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
  if (upstream.ok && upstream.body
      && (upstream.headers.get("content-type") ?? "").startsWith("text/event-stream")) {
    return new Response(upstream.body, { headers: SSE_HEADERS });
  }
  return answer(upstream);
}
