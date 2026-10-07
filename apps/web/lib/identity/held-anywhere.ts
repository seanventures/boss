/**
 * HELD AT THE ORG OR AT ANY PROJECT, for a page a project Admin must reach though they hold
 * nothing at the org: Identity › Access requests needs role_binding:assign somewhere. Server-only;
 * read once per request (React cache).
 *
 * ONE READ: GET /v1/identity/me/projects (FXB-API-CONTRACTS §10) lists every project where the
 * viewer holds access — through their own or a group's binding, at the project or org-wide — with
 * the permissions held THERE. (Before it existed, the console probed up to 25 listed projects one
 * /me/permissions call each, and missed any project past the 25th.) Identity unreadable: null (the
 * caller lets the page render and the API decides, as every identity page does).
 */
import { cache } from "react";

import { checkWith } from "./check";
import { snapshotFrom, type PermissionSnapshot } from "./contract";
import type { Permission } from "./permissions.gen";
import { serverGet } from "./server";
import { loadIdentityCached } from "./server-cache";

export const MY_PROJECTS_PATH = "/v1/identity/me/projects";

export type MyProject = { project_id: string; name: string | null; permissions: string[]; via: string[] };
export type HeldAt = { org: boolean; projects: string[] };

/** Pure: where `permission` is held, from the org snapshot and the /me/projects answer. */
export function heldFrom(org: PermissionSnapshot, mine: readonly MyProject[] | null, permission: Permission): HeldAt {
  const projects = (mine ?? []).filter((p) => p.permissions.includes(permission)).map((p) => p.project_id);
  return { org: checkWith(org, null, null, permission).allowed, projects };
}

const myProjects = cache(async (): Promise<MyProject[] | null> => {
  const answer = await serverGet<{ projects: MyProject[] }>(MY_PROJECTS_PATH);
  return answer.ok ? answer.data.projects ?? [] : null;
});

export const heldAnywhere = cache(async (permission: Permission): Promise<HeldAt | null> => {
  const boot = await loadIdentityCached(null);
  if (!boot.org) return null;
  const org = snapshotFrom(boot.org);
  const mine = await myProjects();
  const held = heldFrom(org, mine, permission);
  if (mine === null && !held.org) return null;     // not answerable here: the API decides
  return held;
});

export function heldSomewhere(at: HeldAt | null): boolean {
  return at === null || at.org || at.projects.length > 0;
}
