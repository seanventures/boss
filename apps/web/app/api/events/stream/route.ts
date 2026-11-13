import { API_URL, authHeaders } from "../../../../lib/client";
import { rawRequestUrl } from "../../../../lib/raw-request-url";

/**
 * The console's realtime event stream (BOSSAPP-835), passed through from GET /v1/events/stream.
 *
 * Same shape as app/api/preview-logs: the browser cannot reach the API (or send it a bearer from
 * an EventSource), so this same-origin route forwards the request — with the viewer's session,
 * which lib/client's identity fetch attaches — and streams the body back unbuffered. The API
 * authorizes EVERY event for this viewer before writing it; this route adds nothing and filters
 * nothing. EventSource's reconnect sends Last-Event-ID, forwarded so the API replays the gap.
 *
 * A failure is an empty stream that closes: the console keeps polling, as it always did.
 */
export const dynamic = "force-dynamic";

const SSE_HEADERS = {
  "Content-Type": "text/event-stream",
  "Cache-Control": "no-cache, no-transform",
  Connection: "keep-alive",
  "X-Accel-Buffering": "no",
} as const;

const PASSED = ["project_id", "kinds", "max_seconds"] as const;

export async function GET(req: Request) {
  const incoming = rawRequestUrl(req).searchParams;
  const query = new URLSearchParams();
  for (const name of PASSED) {
    const value = incoming.get(name);
    if (value) query.set(name, value);
  }
  const headers: Record<string, string> = { Accept: "text/event-stream" };
  const last = req.headers.get("last-event-id");
  if (last) headers["Last-Event-ID"] = last;
  const qs = query.toString();
  let upstream: Response;
  try {
    upstream = await fetch(`${API_URL}/v1/events/stream${qs ? `?${qs}` : ""}`, {
      headers: authHeaders(headers),
      cache: "no-store",
      signal: req.signal,
    });
  } catch {
    return new Response("retry: 30000\n\n", { headers: SSE_HEADERS });
  }
  if (!upstream.ok || !upstream.body) {
    // Not allowed here, or the API is down: tell EventSource to wait before trying again.
    return new Response("retry: 60000\n\n", { headers: SSE_HEADERS });
  }
  return new Response(upstream.body, { headers: SSE_HEADERS });
}
