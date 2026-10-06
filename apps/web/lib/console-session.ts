/**
 * CONSOLE TRANSPORT RULES. The console holds no session of its own any more: the API sets and
 * reads `__Host-boss_session` (IDENTITY.md §4.1), and middleware only checks that one is present.
 * The shared-password HMAC cookie (BOSS_CONSOLE_PASSWORD / BOSS_CONSOLE_SESSION_SECRET) is gone;
 * the legacy password is now the claim credential the API checks (§4.11, D25). What remains here
 * is routing: safe redirects, the network-trusted build, the collector's machine posts, the
 * same-origin write rule and the HTTPS rule.
 */

export function safeNext(value: string | null): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") ||
      value.includes("\\") || /[\u0000-\u001f\u007f]/.test(value)) return "/";
  return value;
}

/**
 * NETWORK-TRUSTED CONSOLE: no sign-in of its own. Only an image BUILT with
 * BOSS_CONSOLE_AUTH=network (a Dockerfile build arg baked into the web image) runs this way —
 * for a host whose network or SSO proxy already decides who reaches it, as the console on
 * goldeneye did before it had a sign-in. Never the default, never a runtime toggle someone can
 * set by accident: every other build keeps the password, the session and the HTTPS check.
 * Cross-site writes are still refused (sameOriginMutation).
 */
export function networkTrustedConsole(): boolean {
  return process.env.BOSS_CONSOLE_AUTH === "network";
}

/**
 * THE IMPROVE BOSS COLLECTOR'S MACHINE POSTS (docs/product/RUN_SCORECARDS.md, "Collector"). An
 * install posting its daily heartbeat to this BOSS is a server, not a browser: it has no session
 * and sends no `Origin`. Exactly these three paths, POST only, and only WITHOUT an Origin header —
 * every browser sends Origin on a POST, so no browser write is let through by this, and the
 * console's CSRF rule for browsers is unchanged. The API checks them itself (404 unless Collector
 * mode is on; bounded, validated, rate-limited), and the /v1 proxy attaches no key to them.
 */
export const COLLECTOR_PATHS = new Set(["/v1/telemetry/ingest", "/v1/telemetry/delete", "/v1/telemetry/share"]);

const COLLECTOR_ROUTE = /^\/v1\/telemetry\/(?:ingest|delete|share)$/;

/**
 * THE ONE COLLECTOR-PATH DECISION (re-check 4, item 1). `/v1/telemetry/share/?x` is the same
 * route to the API — it decodes, collapses slashes and redirects a trailing one — so every form
 * that reaches a collector route is named here, once: the query is ignored, percent-encoding is
 * decoded, repeated and trailing slashes collapse, and the match is case-sensitive like the API's.
 * Returns the canonical path ("/v1/telemetry/share") or null. Every collector decision — the
 * browser check, the cap, the key, the client hop, the upstream path — reads this, never the raw
 * pathname.
 */
export function collectorPath(pathname: string): string | null {
  let decoded: string;
  try { decoded = decodeURIComponent(pathname); } catch { decoded = pathname; }
  const canonical = decoded.replace(/\/{2,}/g, "/").replace(/\/+$/, "");
  return COLLECTOR_ROUTE.test(canonical) ? canonical : null;
}

/** collectorPath for a whole URL (its query and fragment never matter). */
export function collectorPathOf(url: string): string | null {
  try { return collectorPath(new URL(url).pathname); } catch { return null; }
}

export function collectorMachinePost(request: Request): boolean {
  if (request.method.toUpperCase() !== "POST") return false;
  if (request.headers.get("origin") || request.headers.get("cookie")) return false;
  // A browser marks every request it makes; a server-to-server POST carries none of these. A
  // cross-site `text/plain` form or no-cors fetch therefore never counts as a machine POST.
  for (const h of ["sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "sec-fetch-user"]) {
    if (request.headers.get(h)) return false;
  }
  return collectorPathOf(request.url) !== null;
}

export function sameOriginMutation(request: Request): boolean {
  if (["GET", "HEAD", "OPTIONS"].includes(request.method.toUpperCase())) return true;
  const origin = request.headers.get("origin");
  if (!origin) return false;
  const url = new URL(request.url);
  if (localHttp(request)) {
    try {
      const actual = new URL(origin);
      // Next can normalize request.url to its internal listener port behind an SSH tunnel.
      // The browser's same-origin address is the validated loopback Host header.
      const host = request.headers.get("host");
      if (!host) return false;
      const browser = new URL(`http://${host}`);
      return origin === actual.origin && actual.origin === browser.origin;
    } catch { return false; }
  }
  if (networkTrustedConsole() && !process.env.BOSS_CONSOLE_PUBLIC_ORIGIN) {
    // Behind a proxy the internal URL is not the browser's: the page must have come from the
    // host the browser asked for.
    const host = request.headers.get("x-forwarded-host") || request.headers.get("host");
    try { return !!host && new URL(origin).host === host; } catch { return false; }
  }
  return origin === (process.env.BOSS_CONSOLE_PUBLIC_ORIGIN || url.origin);
}

export function secureConsoleTransport(request: Request): boolean {
  if (localHttp(request)) return true;
  // Next's production server normalizes request.url to localhost when it listens on loopback,
  // even when an external TLS proxy sent Host: boss.example. Forwarded TLS metadata is only
  // trusted when the operator explicitly configures the public origin AND the listener is
  // kept on loopback. The reverse proxy must overwrite incoming X-Forwarded-* headers.
  const publicOrigin = process.env.BOSS_CONSOLE_PUBLIC_ORIGIN;
  if (!publicOrigin?.startsWith("https://")) return false;
  try {
    const expected = new URL(publicOrigin);
    if (publicOrigin !== expected.origin) return false;
    const url = new URL(request.url);
    const host = request.headers.get("host") || url.host;
    if (host !== expected.host) return false;
    if (url.protocol === "https:" && url.origin === publicOrigin) return true;
    return request.headers.get("x-forwarded-proto") === "https" &&
      request.headers.get("x-forwarded-host") === expected.host;
  } catch { return false; }
}

export function localHttp(request: Request): boolean {
  const url = new URL(request.url);
  if (url.protocol !== "http:") return false;
  try {
    const host = new URL(`http://${request.headers.get("host") || url.host}`);
    return ["localhost", "127.0.0.1", "[::1]"].includes(host.hostname);
  } catch { return false; }
}
