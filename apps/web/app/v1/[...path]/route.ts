import { API_URL, API_KEY } from "../../../lib/client";

/**
 * The control plane, on the console's own origin.
 *
 * Twenty-one client components reach the API through `lib/*` helpers that fetch
 * `${API_URL}/v1/...` directly. In a browser that is a CROSS-ORIGIN request, and the API has no
 * CORS middleware anywhere — `OPTIONS /v1/factory/flow` answers `401` with no
 * `Access-Control-Allow-Origin` at all. So every one of those calls is refused before it is
 * sent, and because the helpers catch their own errors and return an empty feed, the areas do
 * not show an error: they show NOTHING, which reads as "no data yet" rather than "this cannot
 * work". Measured on the live console, on `/`:
 *
 *     Access to fetch at 'https://boss-api.../v1/factory/flow?project_id=boss-dogfood'
 *     from origin 'https://boss...' has been blocked by CORS policy
 *
 * The fix is the one this codebase already uses for the same problem — see
 * `app/api/preview-logs/route.ts`, which says it plainly: *"the browser cannot reach the API at
 * all in this deployment"*. There were 27 hand-written route handlers doing this one endpoint at
 * a time. This is the general case, so a new client feed needs no new handler.
 *
 * It also takes the API key OFF the wire. `API_KEY` falls back to `NEXT_PUBLIC_BOSS_API_KEY`,
 * which is inlined into the JavaScript bundle at build time — every visitor gets a copy of a
 * credential that can write to the control plane. The key is attached HERE, on the server, and
 * the browser sends none.
 */
export const dynamic = "force-dynamic";

/** Never let the caller choose its own identity: the key is the server's to add. */
const STRIPPED = new Set([
  "authorization", "x-boss-api-key", "host", "connection", "content-length",
  "cookie", "origin", "referer",
]);

function forwardHeaders(req: Request): Headers {
  const out = new Headers();
  req.headers.forEach((value, key) => {
    if (!STRIPPED.has(key.toLowerCase())) out.set(key, value);
  });
  if (API_KEY) out.set("Authorization", `Bearer ${API_KEY}`);
  return out;
}

async function proxy(req: Request, path: string[]): Promise<Response> {
  if (!API_URL) {
    // Say which variable, on the side that can set it. A relative fetch here would loop back
    // into this handler and the console would hang rather than fail.
    return Response.json(
      {
        ok: false,
        error:
          "BOSS_API_URL is not set on the web server, so the console has no control-plane " +
          "address to proxy to.",
      },
      { status: 503 },
    );
  }
  const incoming = new URL(req.url);
  // The path EXACTLY as sent — its trailing slash and its encoding are the previewed app's,
  // and rebuilding it from the decoded segments dropped the one and re-encoded the other.
  const target = incoming.pathname.startsWith("/v1/")
    ? `${API_URL}${incoming.pathname}${incoming.search}`
    : `${API_URL}/v1/${path.map(encodeURIComponent).join("/")}${incoming.search}`;
  const method = req.method.toUpperCase();
  const hasBody = method !== "GET" && method !== "HEAD" && method !== "DELETE";
  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method,
      headers: forwardHeaders(req),
      body: hasBody ? await req.arrayBuffer() : undefined,
      cache: "no-store",
      redirect: "manual",
    });
  } catch (error) {
    // A dead control plane must read as a control-plane failure, not as an empty feed.
    return Response.json(
      { ok: false, error: error instanceof Error ? error.message : "control plane unreachable" },
      { status: 502 },
    );
  }
  const headers = new Headers();
  // `location` too: a previewed app's redirect arrives already rewritten under
  // /v1/previews/<id>/web by the API, and a 3xx without it strands the browser on a blank page.
  for (const name of ["content-type", "cache-control", "etag", "content-disposition", "location"]) {
    const value = upstream.headers.get(name);
    if (value) headers.set(name, value);
  }
  // Streamed straight through: a proxy that buffers defeats SSE and long reads.
  return new Response(upstream.body, { status: upstream.status, headers });
}

type Ctx = { params: Promise<{ path: string[] }> };
const handler = async (req: Request, ctx: Ctx) => proxy(req, (await ctx.params).path ?? []);

export const GET = handler;
export const POST = handler;
export const PUT = handler;
export const PATCH = handler;
export const DELETE = handler;
export const HEAD = handler;
