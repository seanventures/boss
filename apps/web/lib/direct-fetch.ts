/**
 * A CALL THAT CARRIES THE CLIENT'S OWN CREDENTIAL GOES OUT ON THE PLATFORM FETCH.
 *
 * lib/client.ts wraps the global fetch twice for every call to BOSS_API_URL: the feed cache
 * (lib/feed-cache.ts) collapses and keeps reads, and the identity wrapper
 * (lib/identity/server-fetch.ts) replaces the caller's credentials with the signed-in person's —
 * it strips `Authorization`, every X-Boss-* and the forwarded chain, then adds the session cookie.
 * That is right for the console's own calls and wrong for a pass-through whose caller brings its
 * own credential: git's HTTP Basic token (app/git), an IdP's SCIM bearer, an OIDC client's
 * secret, a monitoring source's token. Through the wrappers the API saw no credential at all
 * (BOSSAPP-948: `git clone http://localhost:3000/git/<p>.git` got 401 with a valid token), and
 * the feed cache could have served one token's read to another.
 *
 * Every module that forwards a request's own `Authorization` calls this, never `fetch`
 * (tests/a-client-credential-reaches-the-api.test.ts enforces it and proves it end to end).
 */
export function directFetch(input: string, init: RequestInit): Promise<Response> {
  const g = globalThis as unknown as { __bossOriginalFetch?: typeof fetch };
  return (g.__bossOriginalFetch ?? globalThis.fetch)(input, init);
}
