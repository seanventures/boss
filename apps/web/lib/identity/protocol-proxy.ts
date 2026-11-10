/**
 * SAML AND SCIM ON THE CONSOLE'S ORIGIN (round 3, W2).
 *
 * An IdP is configured with ONE address for BOSS: the public origin, which is the console's. The
 * console forwarded only /v1/*, so `{origin}/saml/v2/metadata` answered with the console's own
 * HTML, the IdP's SAML POST to `{origin}/saml/v2/acs` never reached the API, and a SCIM client
 * pointed at `{origin}/scim/v2/Users` got a page. These are protocol endpoints, not console pages,
 * and each keeps the credential its protocol defines — nothing more:
 *
 *   SCIM       the IdP's bearer token (`Authorization`) passes through unchanged. No cookie, no
 *              CSRF token, no Origin, no network marker: the request speaks for the SCIM key and
 *              for nobody else. Only a machine request (no Origin, no Sec-Fetch-*) is forwarded,
 *              so a browser can never send the signed-in person's ambient anything this way.
 *   SAML meta  a public GET (the SP metadata XML). Nothing is forwarded but the query.
 *   SAML ACS   the IdP's auto-submitted form POST: CROSS-SITE by nature, so it is the ONE write
 *              middleware.ts lets past the cross-site refusal, and only at exactly
 *              `/saml/v2/acs`. It carries the identity cookies (the pre-auth cookie binds the
 *              flow) and the form; the API's answer (a 303 with the new session cookie) is
 *              relayed as is.
 *
 * Every other identity header is dropped, as on /v1 (lib/identity/forward.ts). The calls go out
 * on the platform fetch (directFetch): the server-side wrappers would re-attach the person's
 * cookie and CSRF (server-fetch.ts) or collapse two SCIM keys' reads into one (feed-cache.ts).
 */
import { clientHop, proxyHops } from "../client-hop";
import { readCapped } from "../read-capped";
import { identityCookieHeader, normalisePath } from "./forward";
import { rawRequestUrl } from "../raw-request-url";
import { directFetch } from "../direct-fetch";

export const SAML_METADATA_PATH = "/saml/v2/metadata";
export const SAML_ACS_PATH = "/saml/v2/acs";
const SCIM = /^\/scim\/v2(?:\/|$)/;

export type ProtocolKind = "scim" | "saml-metadata" | "saml-acs";

/** A SAMLResponse is a few KB, a SCIM body smaller still; nothing larger is read. */
export const PROTOCOL_BODY_CAP = 1024 * 1024;

/** Is this a SCIM path, however spelled? Judged on the raw AND the normalised path: both must say so. */
export function isScimPath(pathname: string): boolean {
  return SCIM.test(pathname) && SCIM.test(normalisePath(pathname));
}

/** Which protocol endpoint this request is, or null. SAML is matched exactly (no other spelling). */
export function protocolKind(method: string, pathname: string): ProtocolKind | null {
  const m = method.toUpperCase();
  if (isScimPath(pathname)) return "scim";
  if (pathname === SAML_METADATA_PATH && (m === "GET" || m === "HEAD")) return "saml-metadata";
  if (pathname === SAML_ACS_PATH && m === "POST") return "saml-acs";
  return null;
}

/** A server-to-server request: a browser marks every request it makes, a SCIM client none. */
export function machineRequest(headers: Headers): boolean {
  if (headers.get("origin")) return false;
  return !["sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "sec-fetch-user"].some((h) => headers.get(h));
}

/** The only request headers each protocol forwards (lower-case). */
const PLAIN = ["accept", "content-type", "user-agent", "if-match", "if-none-match"];

export function protocolHeaders(kind: ProtocolKind, incoming: Headers): Headers {
  const out = new Headers();
  for (const name of PLAIN) {
    const value = incoming.get(name);
    if (value) out.set(name, value);
  }
  if (kind === "scim") {
    const auth = incoming.get("authorization");
    if (auth) out.set("authorization", auth);
  }
  if (kind === "saml-acs") {
    const cookie = identityCookieHeader(incoming.get("cookie"));
    if (cookie) out.set("cookie", cookie);
  }
  const hop = clientHop(incoming.get("x-forwarded-for"), proxyHops());
  if (hop) out.set("x-forwarded-for", hop);
  return out;
}

/** The answer headers relayed to the caller. Set-Cookie only from the ACS (it signs the person in). */
const RELAYED = ["content-type", "cache-control", "etag", "location", "www-authenticate", "retry-after", "content-security-policy"];

/** The platform fetch, before the console's server-side wrappers (lib/direct-fetch.ts). */
export { directFetch };

function answer(status: number, detail: string): Response {
  return Response.json({ detail }, { status, headers: { "Cache-Control": "no-store" } });
}

export async function forwardProtocol(req: Request, opts: { apiUrl: string; fetchImpl?: typeof directFetch }): Promise<Response> {
  const url = rawRequestUrl(req);   // the query exactly as sent (NextRequest rewrites 127.0.0.1)
  const kind = protocolKind(req.method, url.pathname);
  if (!kind) return answer(404, "not_found");
  if (kind === "scim" && !machineRequest(req.headers)) return answer(403, "scim_is_server_to_server");
  if (!opts.apiUrl) return answer(503, "BOSS_API_URL is not set on the web server");
  const method = req.method.toUpperCase();
  let body: Uint8Array | undefined;
  if (!["GET", "HEAD", "DELETE"].includes(method)) {
    const buf = await readCapped(req, PROTOCOL_BODY_CAP);
    if (buf === null) return answer(413, "too_large");
    body = buf;
  }
  let upstream: Response;
  try {
    upstream = await (opts.fetchImpl ?? directFetch)(`${opts.apiUrl}${url.pathname}${url.search}`, {
      method, headers: protocolHeaders(kind, req.headers), body: body as BodyInit | undefined,
      cache: "no-store", redirect: "manual",
    });
  } catch {
    return answer(502, "control plane unreachable");
  }
  const headers = new Headers();
  for (const name of RELAYED) {
    const value = upstream.headers.get(name);
    if (value) headers.set(name, value);
  }
  if (kind === "saml-acs") {
    for (const c of upstream.headers.getSetCookie?.() ?? []) headers.append("set-cookie", c);
  }
  return new Response(upstream.body, { status: upstream.status, headers });
}
