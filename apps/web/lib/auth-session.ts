import { BossApiError, type AuthSessionResponse } from "@boss/api-client";

import { client } from "./client";

export type AuthSessionFeed = {
  apiReachable: boolean;
  authRequired: boolean;
  transportAuthenticated: boolean;
  actorBindingRequired: boolean;
  principalMemberId?: string;
  authMethod?: string;
  oidcEnabled: boolean;
  message: string;
  blockedReason?: string;
};

function mapAuthSession(payload: AuthSessionResponse): AuthSessionFeed {
  return {
    apiReachable: true,
    authRequired: payload.auth_required,
    transportAuthenticated: payload.transport_authenticated,
    actorBindingRequired: payload.actor_binding_required,
    principalMemberId: payload.principal_member_id,
    authMethod: payload.auth_method,
    oidcEnabled: payload.oidc_enabled,
    message: payload.message,
  };
}

export function emptyAuthSession(reason?: string): AuthSessionFeed {
  return {
    apiReachable: false,
    authRequired: false,
    transportAuthenticated: false,
    actorBindingRequired: false,
    oidcEnabled: false,
    message: "Auth session state is unavailable.",
    blockedReason: reason,
  };
}

export async function fetchAuthSession(): Promise<AuthSessionFeed> {
  try {
    return mapAuthSession(await client.getAuthSession());
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyAuthSession(`API returned ${error.status}`);
    }
    return emptyAuthSession(
      error instanceof Error ? error.message : "Unknown API error",
    );
  }
}
