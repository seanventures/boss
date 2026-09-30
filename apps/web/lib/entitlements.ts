import {
  BossApiError,
  type EntitlementConnectionSessionSummary,
  type EntitlementSourceSummary,
} from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type EntitlementSourceFeedItem = {
  id: string;
  provider: string;
  sourceId: string;
  sourceKind: string;
  status: string;
  effectiveStatus: string;
  authMethod: string;
  connectionStatus: string;
  connectionRef?: string;
  oauthScopes: string[];
  connectionMetadata: Record<string, unknown>;
  runtimeSupported: boolean;
  credentialPresent: boolean;
  multiTenantAllowed: boolean;
  capacityState: string;
  capacityResetAt?: string;
  observedAt?: string;
  credentialRef?: string;
  supportedModels: string[];
};

export type EntitlementConnectionSessionFeedItem = {
  id: string;
  sourceId: string;
  provider: string;
  authMethod: string;
  connectionMode: string;
  status: string;
  requestedScopes: string[];
  loginCommand?: string;
  connectionRef?: string;
  instructions: Record<string, unknown>;
  requestedBy: string;
  completedBy?: string;
  expiresAt?: string;
  createdAt: string;
  completedAt?: string;
};

export type EntitlementFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: EntitlementSourceFeedItem[];
  connectionSessions: EntitlementConnectionSessionFeedItem[];
};

function mapSource(payload: EntitlementSourceSummary): EntitlementSourceFeedItem {
  return {
    id: payload.id,
    provider: payload.provider,
    sourceId: payload.source_id,
    sourceKind: payload.source_kind,
    status: payload.status,
    effectiveStatus: payload.effective_status,
    authMethod: payload.auth_method,
    connectionStatus: payload.connection_status,
    connectionRef: payload.connection_ref,
    oauthScopes: payload.oauth_scopes,
    connectionMetadata: payload.connection_metadata,
    runtimeSupported: payload.runtime_supported,
    credentialPresent: Boolean(payload.observed_capacity?.credential_present),
    multiTenantAllowed: payload.multi_tenant_allowed,
    capacityState:
      typeof payload.observed_capacity?.capacity_state === "string"
        ? payload.observed_capacity.capacity_state
        : "unknown",
    capacityResetAt:
      typeof payload.observed_capacity?.reset_at === "string"
        ? payload.observed_capacity.reset_at
        : typeof payload.observed_capacity?.rate_limit_reset_at === "string"
          ? payload.observed_capacity.rate_limit_reset_at
          : undefined,
    observedAt:
      typeof payload.observed_capacity?.observed_at === "string"
        ? payload.observed_capacity.observed_at
        : typeof payload.observed_capacity?.last_checked_at === "string"
          ? payload.observed_capacity.last_checked_at
          : undefined,
    credentialRef: payload.credential_ref,
    supportedModels: payload.supported_models,
  };
}

function mapConnectionSession(
  payload: EntitlementConnectionSessionSummary,
): EntitlementConnectionSessionFeedItem {
  return {
    id: payload.id,
    sourceId: payload.source_id,
    provider: payload.provider,
    authMethod: payload.auth_method,
    connectionMode: payload.connection_mode,
    status: payload.status,
    requestedScopes: payload.requested_scopes,
    loginCommand: payload.login_command,
    connectionRef: payload.connection_ref,
    instructions: payload.instructions,
    requestedBy: payload.requested_by,
    completedBy: payload.completed_by,
    expiresAt: payload.expires_at,
    createdAt: payload.created_at,
    completedAt: payload.completed_at,
  };
}

export function emptyEntitlementFeed(reason?: string, projectId = PROJECT_ID): EntitlementFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    items: [],
    connectionSessions: [],
  };
}

export async function fetchEntitlementFeed(
  projectId = PROJECT_ID,
): Promise<EntitlementFeed> {
  try {
    const resolvedProjectId = resolveProjectId(projectId);
    const [payload, sessions] = await Promise.all([
      client.listEntitlementSources(resolvedProjectId),
      client.listEntitlementConnectionSessions(resolvedProjectId, { limit: 20 }),
    ]);
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapSource),
      connectionSessions: sessions.items.map(mapConnectionSession),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyEntitlementFeed(`API returned ${error.status}`, projectId);
    }
    return emptyEntitlementFeed(
      error instanceof Error ? error.message : "Unknown API error",
      projectId,
    );
  }
}
