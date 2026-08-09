import { createBossApiClient } from "@boss/api-client";

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
export const API_URL = (
  (IS_SERVER ? process.env.BOSS_API_URL : "") ||
  process.env.NEXT_PUBLIC_BOSS_API_URL ||
  ""
).replace(/\/$/, "");

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
export const API_KEY =
  process.env.BOSS_API_KEY ?? process.env.NEXT_PUBLIC_BOSS_API_KEY;

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

export const client = createBossApiClient({ baseUrl: API_URL, apiKey: API_KEY });
