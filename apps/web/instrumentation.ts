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
  const [{ headers }, write] = await Promise.all([
    import("next/headers"),
    import("./lib/api-write"),
  ]);
  write.installServerIntentResolver(async () => {
    const incoming = await headers();
    return incoming.get(write.IDEMPOTENCY_HEADER) ?? incoming.get(write.INTENT_HEADER);
  });
}
