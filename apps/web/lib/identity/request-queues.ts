/**
 * EVERY ACCESS-REQUEST QUEUE THE VIEWER ANSWERS (Identity › Access requests, round 2 follow-up 1).
 * An org admin (role_binding:read at the org) reads the org's list, which holds every request.
 * Anyone else reads the queue of each project where they hold role_binding:assign
 * (GET /v1/identity/projects/{id}/access-requests). Pure: the server page supplies what is held.
 */
import { checkWith } from "./check";
import type { PermissionSnapshot } from "./contract";
import type { HeldAt } from "./held-anywhere";
import type { RequestQueue } from "./access-request-queue";

export const ORG_QUEUE: RequestQueue = { path: "/v1/identity/access-requests?status=open", projectId: null };

export function requestQueues(org: PermissionSnapshot | null, held: HeldAt | null, candidates: readonly string[]): RequestQueue[] {
  if (!org || checkWith(org, null, null, "role_binding:read").allowed) return [ORG_QUEUE];
  const projects = held?.org ? candidates : held?.projects ?? [];
  return projects.map((id) => ({ path: `/v1/identity/projects/${encodeURIComponent(id)}/access-requests?status=open`, projectId: id }));
}
