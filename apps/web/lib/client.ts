import { createBossApiClient } from "@boss/api-client";

export const API_URL =
  process.env.NEXT_PUBLIC_BOSS_API_URL?.replace(/\/$/, "") ??
  "http://127.0.0.1:8000";
export const PROJECT_ID =
  process.env.NEXT_PUBLIC_BOSS_PROJECT_ID ?? "boss-dogfood";

export const client = createBossApiClient({ baseUrl: API_URL });
