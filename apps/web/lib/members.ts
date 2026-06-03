import {
  BossApiError,
  type OrgMemberSummary,
  type ProjectMemberSummary,
} from "@boss/api-client";

import { client, PROJECT_ID, resolveProjectId } from "./client";

export type ProjectMemberFeedItem = {
  id: string;
  projectId: string;
  memberId: string;
  displayName: string;
  memberType: string;
  roleKeys: string[];
  permissions: string[];
  status: string;
  createdBy: string;
  createdAt: string;
  updatedAt: string;
};

export type ProjectMemberFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: ProjectMemberFeedItem[];
};

export type OrgMemberFeedItem = {
  id: string;
  orgId: string;
  projectId?: string;
  memberId: string;
  displayName: string;
  memberType: string;
  roleKeys: string[];
  permissions: string[];
  status: string;
  createdBy: string;
  createdAt: string;
  updatedAt: string;
};

export type OrgMemberFeed = {
  orgId: string;
  apiReachable: boolean;
  blockedReason?: string;
  items: OrgMemberFeedItem[];
};

function mapMember(payload: ProjectMemberSummary): ProjectMemberFeedItem {
  return {
    id: payload.id,
    projectId: payload.project_id,
    memberId: payload.member_id,
    displayName: payload.display_name,
    memberType: payload.member_type,
    roleKeys: payload.role_keys,
    permissions: payload.permissions,
    status: payload.status,
    createdBy: payload.created_by,
    createdAt: payload.created_at,
    updatedAt: payload.updated_at,
  };
}

function mapOrgMember(payload: OrgMemberSummary): OrgMemberFeedItem {
  return {
    id: payload.id,
    orgId: payload.org_id,
    projectId: payload.project_id,
    memberId: payload.member_id,
    displayName: payload.display_name,
    memberType: payload.member_type,
    roleKeys: payload.role_keys,
    permissions: payload.permissions,
    status: payload.status,
    createdBy: payload.created_by,
    createdAt: payload.created_at,
    updatedAt: payload.updated_at,
  };
}

function emptyMemberFeed(reason?: string, projectId = PROJECT_ID): ProjectMemberFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export function emptyOrgMemberFeed(reason?: string, orgId = "org_boss"): OrgMemberFeed {
  return {
    orgId,
    apiReachable: false,
    blockedReason: reason,
    items: [],
  };
}

export async function fetchMemberFeed(
  projectId = PROJECT_ID,
): Promise<ProjectMemberFeed> {
  try {
    const payload = await client.listProjectMembers(resolveProjectId(projectId));
    return {
      projectId: payload.project_id,
      apiReachable: true,
      items: payload.items.map(mapMember),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyMemberFeed(`API returned ${error.status}`, projectId);
    }
    return emptyMemberFeed(
      error instanceof Error ? error.message : "Unknown API error",
      projectId,
    );
  }
}

export async function fetchOrgMemberFeed(
  orgId: string,
  projectId = PROJECT_ID,
  actor = "ceo",
): Promise<OrgMemberFeed> {
  try {
    const payload = await client.listOrgMembers(
      orgId,
      resolveProjectId(projectId),
      actor,
    );
    return {
      orgId: payload.org_id,
      apiReachable: true,
      items: payload.items.map(mapOrgMember),
    };
  } catch (error) {
    if (error instanceof BossApiError) {
      return emptyOrgMemberFeed(`API returned ${error.status}`, orgId);
    }
    return emptyOrgMemberFeed(
      error instanceof Error ? error.message : "Unknown API error",
      orgId,
    );
  }
}
