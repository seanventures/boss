/** Single-operator Community console session. No control-plane API key reaches the browser. */

export const SESSION_COOKIE = "__Host-boss_console_session";
const LOCAL_SESSION_COOKIE = "boss_console_session_local";
export const SESSION_SECONDS = 8 * 60 * 60;

export type ConsoleAuthConfig = { password: string; secret: string };

export function consoleAuthConfig(): ConsoleAuthConfig | null {
  const password = process.env.BOSS_CONSOLE_PASSWORD ?? "";
  const secret = process.env.BOSS_CONSOLE_SESSION_SECRET ?? "";
  // Both values are generated independently by the installer. No fixed development default.
  if (password.length < 32 || secret.length < 32) return null;
  return { password, secret };
}

function bytes(value: string): Uint8Array<ArrayBuffer> {
  // Keep TextEncoder's native realm: Next's Edge runtime rejects an ArrayBuffer allocated
  // by the bundled application realm even though the Node test runtime accepts it.
  return new TextEncoder().encode(value) as Uint8Array<ArrayBuffer>;
}

function hex(value: Uint8Array): string {
  return Array.from(value, (part) => part.toString(16).padStart(2, "0")).join("");
}

function equalHex(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let i = 0; i < left.length; i += 1) {
    difference |= left.charCodeAt(i) ^ right.charCodeAt(i);
  }
  return difference === 0;
}

async function digest(value: string): Promise<string> {
  return hex(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes(value))));
}

export async function passwordMatches(input: string, config: ConsoleAuthConfig): Promise<boolean> {
  return equalHex(await digest(input), await digest(config.password));
}

async function mac(payload: string, config: ConsoleAuthConfig): Promise<string> {
  // A password change revokes sessions even if the separate signing secret is unchanged.
  const key = await crypto.subtle.importKey(
    "raw", bytes(`${config.secret}:${config.password}`),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  return hex(new Uint8Array(await crypto.subtle.sign("HMAC", key, bytes(payload))));
}

export async function issueSession(
  config: ConsoleAuthConfig, now = Date.now(), nonce = crypto.randomUUID(),
): Promise<string> {
  const payload = `v1.${now + SESSION_SECONDS * 1000}.${nonce}`;
  return `${payload}.${await mac(payload, config)}`;
}

export async function validSession(
  token: string | undefined, config: ConsoleAuthConfig, now = Date.now(),
): Promise<boolean> {
  if (!token || token.length > 180) return false;
  const match = /^v1\.(\d{13})\.([a-f0-9-]{36})\.([a-f0-9]{64})$/.exec(token);
  if (!match) return false;
  const expires = Number(match[1]);
  if (!Number.isSafeInteger(expires) || expires <= now ||
      expires > now + SESSION_SECONDS * 1000) return false;
  const payload = `v1.${match[1]}.${match[2]}`;
  return equalHex(match[3], await mac(payload, config));
}

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
export function collectorMachinePost(request: Request): boolean {
  if (request.method.toUpperCase() !== "POST") return false;
  if (request.headers.get("origin") || request.headers.get("cookie")) return false;
  try { return COLLECTOR_PATHS.has(new URL(request.url).pathname); } catch { return false; }
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

export function sessionCookieName(request: Request): string {
  // Browsers do not consistently accept Secure cookies on plain HTTP loopback.
  return localHttp(request) ? LOCAL_SESSION_COOKIE : SESSION_COOKIE;
}
