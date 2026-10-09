/**
 * THE URL THE BROWSER SENT, for every handler that forwards a request onward.
 *
 * Next's `NextRequest.url` is normalised by a regex that rewrites the FIRST loopback address it
 * finds anywhere in the string (127.x.x.x, [::1]) to `localhost`. When the request's host is not
 * itself `localhost` (a container name, 0.0.0.0, a proxied host), that first match is in the QUERY:
 * an app's `redirect_uri=http://127.0.0.1:59999/cb` reached the API as `http://localhost:59999/cb`
 * and was refused as unregistered (P1-1). The base `Request` keeps the URL exactly as received, so a
 * forwarder reads that one; `req.url` is the fallback for a plain Request (tests, other runtimes).
 */
const BASE_URL = Object.getOwnPropertyDescriptor(Request.prototype, "url")?.get;

export function rawRequestUrl(req: Request): URL {
  let raw: unknown;
  try { raw = BASE_URL?.call(req); } catch { raw = undefined; }
  return new URL(typeof raw === "string" && /^https?:\/\//i.test(raw) ? raw : req.url);
}
