/**
 * Types for the account, service-account and agent screens that lib/identity/types.ts does not
 * carry (kept apart so the fix streams do not edit one file).
 */
import type { AuthOptions } from "./types";

/** GET /v1/auth/options, with the policy's password minimum (absent from older APIs). */
export type AuthOptionsWithPolicy = AuthOptions & { password_min_length?: number };

/** The password minimum to say and enforce: the policy's, or 12 when the API did not say. */
export function passwordMin(options: { password_min_length?: number } | null | undefined): number {
  const n = options?.password_min_length;
  return typeof n === "number" && Number.isFinite(n) && n >= 8 ? n : 12;
}

/** GET /v1/identity/me/profile and PATCH /v1/identity/me. */
export interface MyProfile {
  display_name: string;
  email: string | null;
  /** One of the console's eight avatar tones, "0".."7"; null until chosen. */
  avatar_seed: string | null;
}

/** The profile as the page uses it, whatever the API sent: a missing name is no name, never a crash
 *  (the shared-password session's profile had no fields and /account "hit an error"). */
export function asProfile(raw: unknown): MyProfile | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  if (typeof r.display_name !== "string" || !r.display_name.trim()) return null;
  return { display_name: r.display_name, email: typeof r.email === "string" ? r.email : null,
    avatar_seed: typeof r.avatar_seed === "string" ? r.avatar_seed : null };
}

