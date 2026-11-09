"use client";

/**
 * Delete a project, with a full cleanup (services/projects_delete.py).
 *
 * GET  /v1/projects/{id}/delete  what goes, counted by kind, its apps on servers, what stays
 * POST /v1/projects/{id}/delete  project:delete (owner, step-up) + step_up "confirmed" + the name
 *
 * The write goes through the identity client (idPost), so a stale second factor opens "Confirm
 * it's you" and the press is sent once more; cancelled, it is refused as `step_up_cancelled`
 * and the dialog says "Not deleted". The API decides; the console only mirrors it.
 */
import { declareAction } from "../lib/identity/guarded-action";
import { enc, idGet, idPost } from "../lib/identity/api";

export type DeleteCount = { key: string; label: string; count: number };
export type DeleteApp = { target_id: string; app_name: string; environment: string; host: string | null; production: boolean };

export type DeletePreview = {
  id: string;
  name: string;
  slug: string;
  home: boolean;
  /** Why it cannot be deleted at all (the install's home project), in words. */
  refused: string | null;
  counts: DeleteCount[];
  total: number;
  staging_apps: DeleteApp[];
  production_apps: DeleteApp[];
  previews_running: number;
  graph: { nodes: number; edges: number };
  keeps: string[];
  export: { available: boolean; href?: string; note: string };
};

export type DeleteDone = { deleted: true; id: string; name: string; counts: DeleteCount[]; rows: number };

/** What deleting the project would remove, and what stays. */
export const readDelete = (projectId: string) =>
  idGet<DeletePreview>(`/v1/projects/${enc(projectId)}/delete`);

/** Delete the project. `confirmName` is the name as the person typed it. */
export const deleteProject = (projectId: string, confirmName: string, alsoProduction = false) =>
  idPost<DeleteDone>(`/v1/projects/${enc(projectId)}/delete`,
    { step_up: "confirmed", confirm_name: confirmName, also_production: alsoProduction });

// THE PERMISSION EACH ACTION DECLARES (IDENTITY.md §6.5, R4): a mirror, never the decision.
declareAction(readDelete, "project:read");
declareAction(deleteProject, "project:delete");
