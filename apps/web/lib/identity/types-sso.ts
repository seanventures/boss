/**
 * Identity › Sign-in: one provider in full (GET /v1/identity/providers/{id}, FX-B contract §3) —
 * the list row plus its group mappings and its SCIM keys. Never a secret.
 */
import type { Provider } from "./types";

export interface GroupMapping {
  external_group: string;
  group_id: string;
  group_name: string | null;
}

export interface ScimKey {
  id: string;
  name: string;
  status: "active" | "grace" | "revoked" | "expired";
  created_at: string | null;
  expires_at: string | null;
  last_used_at: string | null;
}

export type ClientAuth = "client_secret" | "private_key_jwt";

export interface ProviderDetail extends Provider {
  /** Absent on an API without GET /providers/{id}: the mappings are then unknown, not empty. */
  group_mappings?: GroupMapping[];
  scim_keys?: ScimKey[];
  client_auth?: ClientAuth;
}

/** POST /v1/identity/providers/{id}/scim-keys → shown once. */
export interface IssuedScimKey {
  id: string;
  key: string;
  principal_id: string;
  shown_once: true;
  expires_at: string | null;
}

/** The SCIM principal a provider's keys belong to (identity_sso.py scim_principal_id). */
export function scimPrincipalId(providerId: string): string {
  const i = providerId.indexOf("_");
  return `sys_scim_${i >= 0 ? providerId.slice(i + 1) : providerId}`;
}
