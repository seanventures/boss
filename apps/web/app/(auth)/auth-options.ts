/** What the sign-in screens may show, read on the server (GET /v1/auth/options — a seam, S1). */
import { headers } from "next/headers";

import { serverGet } from "../../lib/identity/server";
import type { AuthOptionsWithPolicy } from "../../lib/identity/types-account";

export type AuthOptionsState =
  | { available: true; options: AuthOptionsWithPolicy }
  | { available: false; host: string };

const record = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);
const nullableString = (value: unknown) => value === null || typeof value === "string";

/** An unreadable or incomplete answer cannot establish whether an Owner exists. */
function validOptions(value: unknown): value is AuthOptionsWithPolicy {
  if (!record(value) || typeof value.org_name !== "string" || typeof value.host !== "string"
    || typeof value.smtp !== "boolean" || typeof value.signup !== "boolean"
    || typeof value.legacy_password !== "boolean" || !Array.isArray(value.providers)
    || !value.providers.every((p) => record(p) && typeof p.id === "string"
      && typeof p.name === "string" && typeof p.preset === "string"
      && ["google", "github", "microsoft", "apple", "okta", "entra", "keycloak", "generic"].includes(p.preset))) return false;
  const claim = value.claim;
  if (claim === null) return true;
  return record(claim) && typeof claim.path === "string"
    && ["setup_token", "legacy_password", "network_bridge"].includes(claim.path)
    && nullableString(claim.origin) && nullableString(claim.expires_at)
    && Array.isArray(claim.seats) && claim.seats.every((seat) => typeof seat === "string")
    && (claim.paths === undefined || (Array.isArray(claim.paths)
      && claim.paths.every((path) => typeof path === "string"
        && ["setup_token", "legacy_password", "network"].includes(path))));
}

export async function loadAuthOptionsState(): Promise<AuthOptionsState> {
  const read = await serverGet<unknown>("/v1/auth/options");
  if (read.ok && validOptions(read.data)) return { available: true, options: read.data };
  return { available: false, host: (await headers()).get("host") ?? "" };
}

export async function loadAuthOptions(): Promise<AuthOptionsWithPolicy> {
  const read = await loadAuthOptionsState();
  if (read.available) return read.options;
  // The API cannot say: show the least — passkey and password, no providers, no email tabs.
  const host = read.host;
  return { org_name: "BOSS", host, providers: [], smtp: false, signup: false, legacy_password: false, claim: null };
}
