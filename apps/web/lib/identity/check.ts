/**
 * can(), choosing the snapshot by the permission's scope: a project-scoped permission is asked of
 * the active project's snapshot, an org-scoped one of the org's (A3: org keys take effect only
 * from org bindings). Isomorphic — the server's area gate and the browser's controls share it.
 */
import { can, type CanResult, type PermissionSnapshot } from "./contract";
import { PERMISSION_META, type Permission } from "./permissions.gen";
import { askWho } from "./words";

export const ASK_ADMIN = " · ask an Admin";

export function checkWith(org: PermissionSnapshot | null, project: PermissionSnapshot | null, projectId: string | null,
  permission: Permission, now = Date.now()): CanResult {
  const meta = PERMISSION_META[permission];
  const useProject = meta.scope === "project" && projectId && project;
  if (useProject && project!.scope.kind === "project" && project!.scope.projectId !== projectId) {
    // ANOTHER PROJECT than the one whose access is loaded (a Projects card, the first-run guide
    // for a project just added): an org-wide grant covers every project in the org, so the org
    // snapshot answers; without one, say where it is answered — never a "still loading" that
    // never ends (00b-15).
    const viaOrg = can(org, permission, { kind: "org" }, now);
    if (viaOrg.allowed) return viaOrg;
    return { allowed: false, reason: "Open this project to use it — your access there is checked on its page", needsStepUp: false };
  }
  const r = useProject
    ? can(project, permission, { kind: "project", projectId: projectId! }, now)
    : can(org, permission, { kind: "org" }, now);
  if (!r.allowed && r.reason.startsWith("Needs ")) return { ...r, reason: `${r.reason} · ${askWho(permission)}` };
  return r;
}
