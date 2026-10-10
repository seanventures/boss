import { API_URL, authHeaders } from "../../../lib/client";
import { rawRequestUrl } from "../../../lib/raw-request-url";

/**
 * Startup logs for a preview, streamed through the server.
 *
 * The console opened an `EventSource` straight at the API. That could not work here and never
 * had: the browser cannot reach the API at all in this deployment (every other call on the page
 * goes through a Server Action or a route handler, server-side), and even where it can, the API
 * requires `Authorization: Bearer …` while `EventSource` cannot send headers. So the request was
 * refused before it began.
 *
 * What the operator saw was worse than an error: "Waiting for preview output…", forever. The
 * stream's `onerror` closed the connection without saying anything, so a control that could
 * never work looked like one that was still trying.
 *
 * Same shape as the other handlers here — same origin, same auth, no re-render — except the body
 * is passed through as a stream instead of buffered, because a log you only see once the process
 * exits is not a startup log.
 */
export const dynamic = "force-dynamic";

/** Server-Sent Events, verbatim. A proxy that buffers is a proxy that defeats the point. */
const SSE_HEADERS = {
  "Content-Type": "text/event-stream",
  "Cache-Control": "no-cache, no-transform",
  Connection: "keep-alive",
  // Nginx and friends buffer proxied responses by default, which holds the whole stream until
  // it completes — the exact failure this route exists to avoid.
  "X-Accel-Buffering": "no",
} as const;

/** An error the READER can see. The stream is the only channel back to the panel once it is
 *  open, so a failure has to arrive as a log line rather than a status code nobody renders. */
function errorStream(message: string): Response {
  const body = `event: log\ndata: ${JSON.stringify(message)}\n\nevent: end\ndata: {}\n\n`;
  return new Response(body, { headers: SSE_HEADERS });
}

export async function GET(req: Request) {
  const previewId = rawRequestUrl(req).searchParams.get("preview_id")?.trim();
  if (!previewId) return errorStream("Cannot stream logs: no preview id was given.");

  let upstream: Response;
  try {
    upstream = await fetch(
      `${API_URL}/v1/previews/${encodeURIComponent(previewId)}/logs`,
      {
        headers: authHeaders({ Accept: "text/event-stream" }),
        cache: "no-store",
        // The point of this route is that it does not finish quickly.
        signal: req.signal,
      },
    );
  } catch (e) {
    return errorStream(
      `Could not reach the preview service: ${String((e as Error)?.message ?? e)}`,
    );
  }

  if (!upstream.ok || !upstream.body) {
    return errorStream(
      `The preview service returned ${upstream.status} for this preview's logs.`
      + (upstream.status === 404
        ? " The preview may have expired — its logs are gone with it."
        : ""),
    );
  }

  return new Response(upstream.body, { headers: SSE_HEADERS });
}
