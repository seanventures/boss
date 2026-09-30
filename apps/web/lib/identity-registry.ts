import {
  BossApiError,
  type EnterpriseIdentityProviderSummary,
  type EnterpriseIdentityRolloutPolicySummary,
  type EnterpriseScimGroupSummary,
  type EnterpriseScimUserSummary,
} from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";
import { editionOptionalList } from "./edition-gating";

const provider = (item: EnterpriseIdentityProviderSummary) => ({
  id: item.id,
  providerKey: item.provider_key,
  providerType: item.provider_type,
  displayName: item.display_name,
  issuer: item.issuer,
  audience: item.audience,
  entityId: item.entity_id,
  ssoUrl: item.sso_url,
  jwksUrl: item.jwks_url,
  certificateFingerprint: item.certificate_fingerprint,
  scimBaseUrl: item.scim_base_url,
  secretRef: item.secret_ref,
  status: item.status,
  testStatus: item.test_status,
  blockingGaps: item.blocking_gaps,
  updatedAt: item.updated_at,
});

const rolloutPolicy = (item: EnterpriseIdentityRolloutPolicySummary) => ({
  id: item.id,
  providerType: item.provider_type,
  runtimeMode: item.runtime_mode,
  allowedDomains: item.allowed_domains,
  rolloutNotes: item.rollout_notes,
  status: item.status,
  createdBy: item.created_by,
  updatedAt: item.updated_at,
});

const group = (item: EnterpriseScimGroupSummary) => ({
  id: item.id,
  externalId: item.external_id,
  displayName: item.display_name,
  roleKeys: item.role_keys,
  permissions: item.permissions,
  status: item.status,
  updatedAt: item.updated_at,
});

const user = (item: EnterpriseScimUserSummary) => ({
  id: item.id,
  externalId: item.external_id,
  userName: item.user_name,
  displayName: item.display_name,
  memberId: item.member_id,
  roleKeys: item.role_keys,
  permissions: item.permissions,
  groups: item.groups,
  status: item.status,
  active: item.active,
  updatedAt: item.updated_at,
});

export type IdentityRegistryFeed = {
  apiReachable: boolean;
  blockedReason?: string;
  identityProviders: ReturnType<typeof provider>[];
  identityRolloutPolicies: ReturnType<typeof rolloutPolicy>[];
  scimGroups: ReturnType<typeof group>[];
  scimUsers: ReturnType<typeof user>[];
};

export function emptyIdentityRegistryFeed(reason?: string): IdentityRegistryFeed {
  return {
    apiReachable: false,
    blockedReason: reason,
    identityProviders: [],
    identityRolloutPolicies: [],
    scimGroups: [],
    scimUsers: [],
  };
}

export async function fetchIdentityRegistryFeed(
  projectId = PROJECT_ID,
  active = true,
): Promise<IdentityRegistryFeed> {
  if (!active) return emptyIdentityRegistryFeed("Open Identity providers to load the registry.");
  const resolved = resolveProjectId(projectId);
  try {
    const [providers, policies, groups, users] = await Promise.all([
      editionOptionalList(client.listEnterpriseIdentityProviders(resolved)),
      editionOptionalList(client.listEnterpriseIdentityRolloutPolicies(resolved)),
      editionOptionalList(client.listEnterpriseScimGroups(resolved)),
      editionOptionalList(client.listEnterpriseScimUsers(resolved)),
    ]);
    return {
      apiReachable: true,
      identityProviders: providers.items.map(provider),
      identityRolloutPolicies: policies.items.map(rolloutPolicy),
      scimGroups: groups.items.map(group),
      scimUsers: users.items.map(user),
    };
  } catch (error) {
    return emptyIdentityRegistryFeed(
      error instanceof BossApiError
        ? `API returned ${error.status}`
        : error instanceof Error ? error.message : "Unknown API error",
    );
  }
}
