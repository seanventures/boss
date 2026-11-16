"use client";

/**
 * Move a project, and read the install's backups (BOSSAPP-836).
 *
 * GET  /v1/projects/{id}/export?actor=     the bundle; secrets are empty placeholders
 * POST /v1/projects/{id}/import            mode=dry_run, then mode=apply with import_id
 * GET  /v1/instance/backups                newest first
 *
 * Upgrade rollback is not an API call: the API cannot see Docker. The card shows `boss rollback`.
 */
import { declareAction } from "../../lib/identity/guarded-action";
import { enc, idGet, idPost } from "../../lib/identity/api";
import type { BackupRow } from "../../lib/portability-console";

export type ImportPlan = {
  import_id?: string;
  status?: string;
  summary?: Record<string, Record<string, number>>;
  errors?: unknown[];
};

export const readBackups = () => idGet<{ backups: BackupRow[] }>("/v1/instance/backups");

export const exportProject = (projectId: string, actor: string) =>
  idGet<unknown>(`/v1/projects/${enc(projectId)}/export?actor=${enc(actor)}`);

export const planImport = (projectId: string, actor: string, bundle: unknown) =>
  idPost<ImportPlan>(`/v1/projects/${enc(projectId)}/import`, { actor, mode: "dry_run", bundle });

export const applyImport = (projectId: string, actor: string, importId: string) =>
  idPost<ImportPlan>(`/v1/projects/${enc(projectId)}/import`, { actor, mode: "apply", import_id: importId });

declareAction(readBackups, "instance:read");
declareAction(exportProject, "project:update");
declareAction(planImport, "project:update");
declareAction(applyImport, "project:update");
