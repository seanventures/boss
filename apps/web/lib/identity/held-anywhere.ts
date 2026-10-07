/**
 * HELD AT THE ORG OR AT ANY PROJECT (round 2 follow-up 1), for a page a project Admin must reach
 * though they hold nothing at the org: Identity › Access requests needs role_binding:assign
 * somewhere. Server-only; read once per request (React cache).
 *
 * The API has no "my projects" read, so the candidates are the console's active project and the
 * projects the viewer can list (GET /v1/projects, when readable), at most MAX_PROJECTS; each is
 * asked of /v1/identity/me/permissions?project_id=… . Identity unreadable: null (the caller lets
 * the page render and the API decides, as every identity page does).
 */
import { cache } from "react";

import { PROJECT_ID } from "../client";
import { fetchProjectFeed } from "../projects";
import { checkWith } from "./check";
import { ME_PERMISSIONS_PATH, snapshotFrom, type MePermissionsResponse } from "./contract";
import type { Permission } from "./permissions.gen";
import { serverGet } from "./server";
import { loadIdentityCached } from "./server-cache";

export const MAX_PROJECTS = 25;

export type HeldAt = { org: boolean; projects: string[] };

/** The projects to ask about: the active one first, then the listable ones. */
export function candidateProjects(active: string, listed: readonly string[]): string[] {
  return [...new Set([active, ...listed].filter(Boolean))].slice(0, MAX_PROJECTS);
}

export const heldAnywhere = cache(async (permission: Permission): Promise<HeldAt | null> => {
  const boot = await loadIdentityCached(null);
  if (!boot.org) return null;
  const org = snapshotFrom(boot.org);
  if (checkWith(org, null, null, permission).allowed) return { org: true, projects: [] };
  const feed = await fetchProjectFeed().catch(() => null);
  const ids = candidateProjects(PROJECT_ID, feed?.apiReachable ? feed.items.map((p) => p.id) : []);
  const answers = await Promise.all(ids.map((id) =>
    serverGet<MePermissionsResponse>(`${ME_PERMISSIONS_PATH}?project_id=${encodeURIComponent(id)}`)));
  const projects = ids.filter((id, i) => {
    const a = answers[i];
    return a.ok && checkWith(org, snapshotFrom(a.data), id, permission).allowed;
  });
  return { org: false, projects };
});

export function heldSomewhere(at: HeldAt | null): boolean {
  return at === null || at.org || at.projects.length > 0;
}
