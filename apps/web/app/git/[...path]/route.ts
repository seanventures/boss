import { API_URL } from "../../../lib/client";
import { identityCookieHeader } from "../../../lib/identity/forward";
import { identityStubEnabled } from "../../../lib/identity/stub/enabled";
import { clientHop, proxyHops } from "../../../lib/client-hop";
import { rawRequestUrl } from "../../../lib/raw-request-url";
import { directFetch } from "../../../lib/direct-fetch";

/**
 * BOSS'S LOCAL GIT REMOTE ON THE CONSOLE'S ORIGIN (BOSSAPP-948): http://localhost:3000/git/<project>.git
 * is the clone URL people see, so git's smart HTTP is passed through to the API's /git routes
 * (apps/api/app/routers/local_git.py), which decide who may read or push.
 *
 * It carries what git sends and nothing it may not: git's own Authorization (HTTP Basic with a
 * BOSS token — the API accepts Basic on /git/* only), the identity cookie when a browser asks,
 * the pack's content type and encoding, and git's protocol header. The host the client named goes
 * up as X-Forwarded-Host: the API refuses a client that did not name this machine (loopback only,
 * unless BOSS_GIT_REMOTE_REACH=network). Packs stream both ways; nothing is buffered here.
 *
 * The call goes out on the platform fetch (lib/direct-fetch.ts), never the global fetch: the
 * console's identity wrapper strips Authorization from every call to the API, so git's token
 * never arrived and every clone through the console got 401 (BOSSAPP-948).
 */
export const dynamic = "force-dynamic";

const FORWARDED = ["authorization", "content-type", "content-encoding", "git-protocol", "accept",
  "accept-encoding", "user-agent", "content-length"];
const RELAYED = ["content-type", "cache-control", "www-authenticate", "expires", "pragma"];

async function proxy(req: Request): Promise<Response> {
  if (!API_URL) {
    return Response.json({ ok: false, error: "BOSS_API_URL is not set on the web server." }, { status: 503 });
  }
  const incoming = rawRequestUrl(req);
  if (!/^\/git\/[A-Za-z0-9._-]+(?:\/[A-Za-z0-9._-]+)?\.git\/(?:info\/refs|git-upload-pack|git-receive-pack)$/.test(incoming.pathname)) {
    return Response.json({ error: "not_found", message: "Not a git smart-HTTP path." }, { status: 404 });
  }
  const headers = new Headers();
  for (const name of FORWARDED) {
    const value = req.headers.get(name);
    if (value) headers.set(name, value);
  }
  const cookie = identityCookieHeader(req.headers.get("cookie"), { stub: identityStubEnabled() });
  if (cookie && !headers.has("authorization")) headers.set("cookie", cookie);
  headers.set("x-forwarded-host", req.headers.get("host") ?? incoming.host);
  const hop = clientHop(req.headers.get("x-forwarded-for"), proxyHops());
  if (hop) headers.set("x-forwarded-for", hop);
  const method = req.method.toUpperCase();
  let upstream: Response;
  try {
    upstream = await directFetch(`${API_URL}${incoming.pathname}${incoming.search}`, {
      method,
      headers,
      body: method === "POST" ? req.body : undefined,
      cache: "no-store",
      redirect: "manual",
      // A pack streams up as it is read (Node's fetch needs half duplex for a streamed body).
      ...(method === "POST" ? { duplex: "half" } : {}),
    } as RequestInit);
  } catch (error) {
    return Response.json({ ok: false, error: error instanceof Error ? error.message : "control plane unreachable" },
      { status: 502 });
  }
  const out = new Headers();
  for (const name of RELAYED) {
    const value = upstream.headers.get(name);
    if (value) out.set(name, value);
  }
  return new Response(upstream.body, { status: upstream.status, headers: out });
}

type Ctx = { params: Promise<{ path: string[] }> };
const handler = async (req: Request, _ctx: Ctx) => proxy(req);

export const GET = handler;
export const POST = handler;
