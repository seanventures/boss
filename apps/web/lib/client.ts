import { createBossApiClient } from "@boss/api-client";

import { writeAwareFetch } from "./api-write";
import { installFeedCache } from "./feed-cache";

/**
 * Where the control plane lives.
 *
 * `NEXT_PUBLIC_*` is inlined at BUILD time, not read at runtime — so the compose file setting
 * `NEXT_PUBLIC_BOSS_API_URL` on the container does nothing, and an image built without the
 * matching `--build-arg` carries an empty string forever. On the server that empty string made
 * `fetch("/v1/orgs")` a RELATIVE url, which Node cannot resolve, and the console showed
 * "Failed to parse URL from /v1/orgs" — a message that names neither the cause nor the fix.
 *
 * So the server also honours `BOSS_API_URL`, an ordinary runtime variable it can actually read.
 * That makes one image deployable anywhere, and it means a forgotten build-arg degrades to a
 * config fix instead of a rebuild. The browser has only the inlined value; nothing can change
 * that, which is why the runtime escape hatch exists for the side that can use it.
 */
const IS_SERVER = typeof window === "undefined";

/**
 * THE BROWSER TALKS TO ITS OWN ORIGIN. Only the server holds the API's address.
 *
 * The browser used to fetch `NEXT_PUBLIC_BOSS_API_URL` directly, which is a different origin,
 * and the API has no CORS middleware anywhere in the codebase — `OPTIONS /v1/factory/flow`
 * answers 401 with no `Access-Control-Allow-Origin`. Twenty-one client components fetch through
 * `lib/*` helpers that catch their own errors and return an empty feed, so those areas rendered
 * BLANK rather than broken, which reads as "no data yet" and had nobody looking for a cause.
 *
 * An empty base makes every `${API_URL}/v1/…` a same-origin path, which `app/v1/[...path]`
 * proxies with the key attached server-side. The server keeps the absolute address, because it
 * is the side that can actually reach it.
 */
export const API_URL = IS_SERVER
  ? (process.env.BOSS_API_URL || process.env.NEXT_PUBLIC_BOSS_API_URL || "").replace(/\/$/, "")
  : "";

/** Say what is wrong, at the point it is wrong.
 *
 * A relative URL on the server is always a misconfiguration, and the error Node raises for it
 * is indecipherable. Callers use this to fail with the variable name instead. */
export function apiUrlProblem(): string | null {
  if (API_URL) return null;
  return IS_SERVER
    ? "BOSS_API_URL is not set on the web server, and this image was built without "
      + "NEXT_PUBLIC_BOSS_API_URL — the console has no control-plane address to call."
    : null;
}
export const PROJECT_ID =
  process.env.NEXT_PUBLIC_BOSS_PROJECT_ID ?? "boss-dogfood";
/** The control-plane key. SERVER ONLY.
 *
 * `NEXT_PUBLIC_BOSS_API_KEY` is inlined into the JavaScript bundle at build time, so reading it
 * in the browser handed every visitor a credential that can write to the control plane. Browser
 * requests now go through `app/v1/[...path]`, which attaches the key on the server. */
export const API_KEY = IS_SERVER ? process.env.BOSS_API_KEY : undefined;

export function resolveProjectId(projectId?: string): string {
  return projectId?.trim() || PROJECT_ID;
}

// Auth headers for direct fetch() calls to the control plane (the deployed API requires a key;
// the typed client sends Bearer, so raw fetches must too). Merge with any extra headers.
export function authHeaders(extra?: Record<string, string>): Record<string, string> {
  return {
    ...(API_KEY ? { Authorization: `Bearer ${API_KEY}` } : {}),
    ...(extra ?? {}),
  };
}

// Collapse duplicate control-plane GETs before they leave this process.
//
// Installed here because every feed imports this module, so one line covers all 173 call
// sites — both the typed client and the raw fetch() helpers. See lib/feed-cache.ts for what
// is and is not cached, and why the window is deliberately tiny.
installFeedCache(API_URL);

// Every typed write goes through lib/api-write.ts, so a server action's calls carry the
// Idempotency-Key derived from the press that started it. Reads pass straight to fetch (and so
// through the feed cache above).
export const client = createBossApiClient({
  baseUrl: API_URL, apiKey: API_KEY, fetchImpl: writeAwareFetch,
});
