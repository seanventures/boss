import { API_URL, API_KEY } from "../../../lib/client";
import { collectorMachinePost, collectorPathOf } from "../../../lib/console-session";
import { COLLECTOR_BODY_CAP, readCapped } from "../../../lib/read-capped";
import { clientHop, proxyHops } from "../../../lib/client-hop";

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

function forwardHeaders(req: Request, machine: boolean): Headers {
  const out = new Headers();
  req.headers.forEach((value, key) => {
    if (!STRIPPED.has(key.toLowerCase())) out.set(key, value);
  });
  // The collector's machine routes speak for another install, never for this console: they get
  // NO key, in any spelling of their path (re-check 4, item 1) — the API exempts them from
  // transport auth and checks them itself. `machine` is collectorPathOf(), decided once.
  if (API_KEY && !machine) out.set("Authorization", `Bearer ${API_KEY}`);
  if (machine) {
    // THE CLIENT'S ADDRESS, NOT ITS CLAIM (re-check C). A client may send its own
    // X-Forwarded-For, and Next keeps it as is, so it is never forwarded unchanged: the console
    // sends one hop — the one its own proxies appended — or none at all.
    for (const h of ["x-forwarded-for", "x-real-ip", "forwarded"]) out.delete(h);
    const hop = clientHop(req.headers.get("x-forwarded-for"), proxyHops());
    if (hop) out.set("x-forwarded-for", hop);
  }
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
  // ONE collector decision, on the normalised path (re-check 4, item 1): `/ingest/`, `share/?x`
  // and `%69ngest` are the same API route, so they get the same browser check, cap, keyless
  // forward and canonical upstream path — never the raw spelling.
  const collector = collectorPathOf(req.url);
  // The path EXACTLY as sent — its trailing slash and its encoding are the previewed app's,
  // and rebuilding it from the decoded segments dropped the one and re-encoded the other.
  const target = collector
    ? `${API_URL}${collector}`
    : incoming.pathname.startsWith("/v1/")
    ? `${API_URL}${incoming.pathname}${incoming.search}`
    : `${API_URL}/v1/${path.map(encodeURIComponent).join("/")}${incoming.search}`;
  const method = req.method.toUpperCase();
  const hasBody = method !== "GET" && method !== "HEAD" && method !== "DELETE";
  // ANOTHER INSTALL'S POST IS READ UNDER A CAP, never buffered whole (re-check item 3): the
  // collector routes are open to anyone, so a 150 MB chunked body must stop at the cap here,
  // before it costs this process its memory. The API applies its own, finer cap after.
  let body: ArrayBuffer | Uint8Array | undefined;
  // THE BROWSER CHECK, HERE (re-check A). These three paths skip middleware (so Next does not
  // tee their bodies), which also skipped its Origin/cookie check: it is enforced in this handler
  // instead, before a byte of the body is read. Only a server-to-server POST passes.
  if (collector && !collectorMachinePost(req)) {
    return refuse(req, 403, "browser_requests_refused");
  }
  if (hasBody && collector) {
    const read = await readCapped(req, COLLECTOR_BODY_CAP);
    if (read === null) {
      return refuse(req, 413, "too_large");
    }
    body = read;
  } else if (hasBody) {
    body = await req.arrayBuffer();
  }
  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method,
      headers: forwardHeaders(req, collector !== null),
      body: body as BodyInit | undefined,
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
  // Never for a collector request (re-check 4, item 1): the API's Location there is its own
  // internal address, and an anonymous caller is owed nothing but the status and the JSON.
  const relayed = ["content-type", "cache-control", "etag", "content-disposition"];
  if (!collector) relayed.push("location");
  for (const name of relayed) {
    const value = upstream.headers.get(name);
    if (value) headers.set(name, value);
  }
  // Streamed straight through: a proxy that buffers defeats SSE and long reads.
  return new Response(upstream.body, { status: upstream.status, headers });
}

/**
 * A refused collector POST closes its connection (re-check 4, item 2): `Connection: close` tells
 * Node to end the socket after this response instead of draining the rest of a 150 MB body to
 * keep it alive, and cancelling the body stops this handler pulling another byte of it.
 */
function refuse(req: Request, status: number, detail: string): Response {
  if (req.body && !req.body.locked) req.body.cancel().catch(() => undefined);
  return Response.json({ detail }, {
    status, headers: { Connection: "close", "Cache-Control": "no-store" },
  });
}

type Ctx = { params: Promise<{ path: string[] }> };
const handler = async (req: Request, ctx: Ctx) => proxy(req, (await ctx.params).path ?? []);

export const GET = handler;
export const POST = handler;
export const PUT = handler;
export const PATCH = handler;
export const DELETE = handler;
export const HEAD = handler;
