import { createBossApiClient } from "@boss/api-client";

export const API_URL =
  process.env.NEXT_PUBLIC_BOSS_API_URL?.replace(/\/$/, "") ??
  "http://127.0.0.1:8000";
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
