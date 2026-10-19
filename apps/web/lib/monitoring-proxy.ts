/**
 * MONITORING'S MACHINE ROUTES ON THE CONSOLE'S ORIGIN (BOSSAPP-598, Phase 1a).
 *
 * External tools are given ONE address for BOSS: the public console URL. So these pass the
 * console without a session, and the console adds nothing — no cookie, no CSRF, no key:
 *
 *   POST /v1/ingest/**            OTLP, Prometheus remote write, alert webhooks. The body is
 *                                 protobuf, snappy or gzip: forwarded as raw bytes, never
 *                                 decoded or re-encoded, read under a 4 MB cap.
 *   GET|POST /v1/alert-actions/<token>   an email's Ack link, from a browser that has no session;
 *                                 the token is the capability, the API answers HTML.
 *
 * Only the headers the senders authenticate with pass (Authorization, X-BOSS-Signature, …): the
 * API checks each source's own credential. The call goes out on the platform fetch
 * (protocol-proxy directFetch): the server-side wrapper strips Authorization and X-BOSS-* and
 * would re-attach the person's cookie.
 *
 * Only a PLAIN spelling is a machine route (no `%`, no `//`, no dot segment): middleware.ts
 * skips exactly those, and refuses any other spelling of the two prefixes, as does the route.
 */
import { clientHop, proxyHops } from "./client-hop";
import { readCapped } from "./read-capped";
import { normalisePath } from "./identity/forward";
import { directFetch } from "./identity/protocol-proxy";
import { rawRequestUrl } from "./raw-request-url";

export const MONITORING_BODY_CAP = 4 * 1024 * 1024;

const SEG = "[A-Za-z0-9_~-][A-Za-z0-9._~-]*";
const INGEST = new RegExp(`^/v1/ingest(?:/${SEG})+$`);
const ACTION = new RegExp(`^/v1/alert-actions/${SEG}$`);
/** Anything a server could route under either prefix, however it is spelled. */
const PREFIX = /^\/v1\/(?:ingest|alert-actions)(?:\/|$)/i;

export type MonitoringKind = "ingest" | "alert-action";

/** Which machine route this plain path is, or null. Dot segments (`.`/`..`) never match. */
export function monitoringPath(pathname: string): MonitoringKind | null {
  if (/(?:^|\/)\.\.?(?:\/|$)/.test(pathname)) return null;
  if (INGEST.test(pathname)) return "ingest";
  if (ACTION.test(pathname)) return "alert-action";
  return null;
}

/** A spelling of the two prefixes that is not the plain one: refused, never proxied as a person. */
export function oddMonitoringSpelling(pathname: string): boolean {
  return monitoringPath(pathname) === null && (PREFIX.test(pathname) || PREFIX.test(normalisePath(pathname)));
}

/** What each machine route forwards (lower-case); nothing else reaches the API. */
const FORWARDED = [
  "authorization", "content-type", "content-encoding", "accept", "user-agent",
  "x-boss-timestamp", "x-boss-signature", "x-boss-token", "sentry-hook-resource", "x-prometheus-remote-write-version",
];
const RELAYED = ["content-type", "cache-control", "retry-after", "content-security-policy", "www-authenticate"];

function answer(status: number, detail: string): Response {
  return Response.json({ detail }, { status, headers: { Connection: "close", "Cache-Control": "no-store" } });
}

export function monitoringHeaders(incoming: Headers): Headers {
  const out = new Headers();
  for (const name of FORWARDED) {
    const v = incoming.get(name);
    if (v) out.set(name, v);
  }
  const hop = clientHop(incoming.get("x-forwarded-for"), proxyHops());
  if (hop) out.set("x-forwarded-for", hop);
  return out;
}

export async function forwardMonitoring(req: Request, opts: { apiUrl: string; fetchImpl?: typeof directFetch }): Promise<Response> {
  const url = rawRequestUrl(req);
  const kind = monitoringPath(url.pathname);
  const method = req.method.toUpperCase();
  const cancel = () => { if (req.body && !req.body.locked) req.body.cancel().catch(() => undefined); };
  if (!kind) { cancel(); return answer(404, "not_found"); }
  const allowed = kind === "ingest" ? method === "POST" : ["GET", "HEAD", "POST"].includes(method);
  if (!allowed) { cancel(); return answer(405, "method_not_allowed"); }
  if (!opts.apiUrl) { cancel(); return answer(503, "BOSS_API_URL is not set on the web server"); }
  let body: Uint8Array | undefined;
  if (method === "POST") {
    const read = await readCapped(req, MONITORING_BODY_CAP);
    if (read === null) return answer(413, "too_large");
    body = read;
  }
  let upstream: Response;
  try {
    upstream = await (opts.fetchImpl ?? directFetch)(`${opts.apiUrl}${url.pathname}${url.search}`, {
      method, headers: monitoringHeaders(req.headers), body: body as BodyInit | undefined, cache: "no-store", redirect: "manual",
    });
  } catch {
    return answer(502, "control plane unreachable");
  }
  const headers = new Headers();
  for (const name of RELAYED) {
    const v = upstream.headers.get(name);
    if (v) headers.set(name, v);
  }
  return new Response(upstream.body, { status: upstream.status, headers });
}
