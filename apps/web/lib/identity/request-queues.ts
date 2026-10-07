/**
 * EVERY ACCESS-REQUEST QUEUE THE VIEWER ANSWERS (Identity › Access requests).
 * An org admin (role_binding:read at the org) reads the org's list, which holds every request.
 * Anyone else reads the queue of each project where they hold role_binding:assign
 * (GET /v1/identity/projects/{id}/access-requests) — the projects GET /v1/identity/me/projects
 * says they hold it at (held-anywhere.ts). Pure: the server page supplies what is held.
 */
import { checkWith } from "./check";
import type { PermissionSnapshot } from "./contract";
import type { HeldAt } from "./held-anywhere";
import type { RequestQueue } from "./access-request-queue";

export const ORG_QUEUE: RequestQueue = { path: "/v1/identity/access-requests?status=open", projectId: null };

export function requestQueues(org: PermissionSnapshot | null, held: HeldAt | null): RequestQueue[] {
  if (!org || checkWith(org, null, null, "role_binding:read").allowed) return [ORG_QUEUE];
  return (held?.projects ?? []).map((id) => ({
    path: `/v1/identity/projects/${encodeURIComponent(id)}/access-requests?status=open`, projectId: id }));
}
