/**
 * Server startup: teach lib/api-write.ts where a server-side write's press comes from.
 *
 * The console's writes are keyed per press so the API runs a repeated press once (see
 * lib/api-write.ts and the four duplicate-action incidents it names). In the browser the press
 * is the click. On the server — route handlers, server actions, the typed client — it is the
 * incoming request: its own Idempotency-Key, or the SubmitButton intent middleware.ts lifted into
 * `x-boss-intent`. Reading request headers needs next/headers, which a module that client
 * components import must never touch; this file runs only on the server, so it installs the
 * reader once for the whole process.
 */
export async function register(): Promise<void> {
  if (process.env.NEXT_RUNTIME !== "nodejs") return;
  const [{ headers }, write, forward, { API_URL }, hops] = await Promise.all([
    import("next/headers"),
    import("./lib/api-write"),
    import("./lib/identity/forward"),
    import("./lib/client"),
    import("./lib/client-hop"),
  ]);
  write.installServerIntentResolver(async () => {
    const incoming = await headers();
    return incoming.get(write.IDEMPOTENCY_HEADER) ?? incoming.get(write.INTENT_HEADER);
  });

  // THE PERSON, NOT A KEY (lib/identity/forward.ts): every server-side call to the API carries
  // the browser's identity cookies, and a write also carries the session's CSRF token and the
  // browser's origin. The token comes from the render's own /me (forward.ts sessionCsrf); a
  // second /me is asked only when nothing in this process has seen the session's.
  forward.installIdentityResolver(async (writing) => {
    const incoming = await headers();
    // The client's address through the trusted-proxy rule (B2): every server-side call carries it.
    const clientHop = hops.clientHop(incoming.get("x-forwarded-for"), hops.proxyHops());
    // A sandboxed (opaque-origin) or cross-site non-navigation caller speaks for nobody: a
    // previewed app's script must never be given the person's session (lib/identity/forward.ts).
    if (forward.untrustedBrowserContext(incoming)) return { cookie: "", csrf: null, origin: null, clientHop };
    const stub = (await import("./lib/identity/stub/enabled")).identityStubEnabled();
    const cookie = forward.identityCookieHeader(incoming.get("cookie"), { stub });
    // A browser GET that needs a writing GET's POST twin carries no Origin: derived from its
    // same-origin proof, or none (lib/identity/forward.ts forwardedWriteOrigin).
    const origin = forward.forwardedWriteOrigin(incoming);
    let csrf = incoming.get(forward.CSRF_HEADER_LC);
    const session = forward.parseIdentityCookies(cookie)[forward.SESSION_COOKIE];
    if (writing && !csrf && session && API_URL) csrf = await forward.sessionCsrf(session, API_URL);
    return { cookie, csrf: csrf ?? null, origin, consoleOrigin: forward.consoleOriginOf(incoming), clientHop };
  });
}
