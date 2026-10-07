/**
 * WHICH ACCESS-REQUEST QUEUE PEOPLE › REQUESTS READS (FXB-API-CONTRACTS §9, round 2). An org admin
 * (role_binding:read at the org) reads the org's list. A project Admin, who holds
 * role_binding:assign at a project but cannot read the org's bindings, reads that project's queue
 * (GET /v1/identity/projects/{project_id}/access-requests). Neither: nothing is fetched, so the
 * page never asks for a list the API would refuse. Identity unreadable: the org list, and the API
 * decides.
 */
import { checkWith } from "./check";
import type { PermissionSnapshot } from "./contract";

export type RequestQueue = { path: string; projectId: string | null };

export function accessRequestQueue(org: PermissionSnapshot | null, project: PermissionSnapshot | null,
  projectId: string | null): RequestQueue | null {
  const orgList = { path: "/v1/identity/access-requests?status=open", projectId: null };
  if (!org) return orgList;
  if (checkWith(org, null, null, "role_binding:read").allowed) return orgList;
  if (projectId && project && checkWith(org, project, projectId, "role_binding:assign").allowed) {
    return { path: `/v1/identity/projects/${encodeURIComponent(projectId)}/access-requests?status=open`, projectId };
  }
  return null;
}
