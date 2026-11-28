/**
 * APP MAP API client (BOSSAPP-555, BOSSAPP-556).
 * Matches apps/api/app/routes_app_map.py.
 */
import { enc, idGet, idPost, idPut } from "./identity/api";

export type AppMapStatus = "proposed" | "confirmed" | "stale" | "ambiguous" | "unmapped";

export type AppMapBaselineStep = {
  name: string;
  command: string;
  cwd: string;
  exit_code: number | null;
  ok: boolean;
  duration_ms: number;
  output_tail?: string;
};

export type AppMapBaseline = {
  status?: "passed" | "failed" | "unverified";
  commit_sha?: string | null;
  root_path?: string | null;
  fingerprint?: string | null;
  steps?: AppMapBaselineStep[];
  duration_ms?: number | null;
  verified_at?: string | null;
  error?: string | null;
};

export type AppMapEntry = {
  id?: string | null;
  app_key: string;
  app_name: string;
  name?: string;
  root_path: string;
  workspace_root: string;
  path?: string;
  stack?: {
    language?: string | null;
    framework?: string | null;
    role?: string | null;
    package_manager?: string | null;
    workspace_tool?: string | null;
    services?: { dir: string; language: string; role: string }[];
  };
  commands?: {
    install?: string | null;
    build?: string | null;
    lint?: string | null;
    typecheck?: string | null;
    test?: string | null;
    e2e?: string | null;
  };
  run?: {
    command?: string | null;
    health?: string | null;
    entrypoints?: string[];
  };
  manifests?: string[];
  shared_paths?: string[];
  provenance?: {
    source?: string | null;
    confidence?: string | null;
    evidence?: string[];
    confirmed_by?: string | null;
    confirmed_at?: string | null;
  };
  fingerprint?: string | null;
  baseline?: AppMapBaseline | null;
  status: AppMapStatus;
  binding_id?: string | null;
  repository?: string | null;
};

export type AppMapDrift = {
  drifted: boolean;
  old_fingerprint?: string | null;
  new_fingerprint?: string | null;
  manifests?: string[];
  added_manifests?: string[];
  removed_manifests?: string[];
};

export type AppMapResponse = {
  project_id: string;
  app_key: string;
  app_map: AppMapEntry | null;
  status: AppMapStatus;
  message?: string | null;
  drift?: AppMapDrift | null;
};

export type AppMapScanResponse = {
  project_id: string;
  status: "proposed" | "ambiguous" | "empty";
  source: string;
  workspace_root: string;
  selected: AppMapEntry | null;
  apps: AppMapEntry[];
  candidates: AppMapEntry[];
  shared_paths: string[];
  warnings: string[];
  refusal?: string | null;
  drifted?: Array<{ app_key: string; root_path: string } & AppMapDrift>;
};

export type AppMapSaveInput = {
  project_id: string;
  app_name?: string;
  root_path: string;
  workspace_root?: string;
  stack?: AppMapEntry["stack"];
  commands?: AppMapEntry["commands"];
  run?: AppMapEntry["run"];
  shared_paths?: string[];
  manifests?: string[];
  provenance?: AppMapEntry["provenance"];
  fingerprint?: string | null;
  binding_id?: string | null;
  confirm?: boolean;
};

export const appMapApi = {
  list: (projectId: string) =>
    idGet<{ project_id: string; items: AppMapEntry[] }>(`/v1/projects/${enc(projectId)}/app-maps`),

  get: (projectId: string, appKey: string) =>
    idGet<AppMapResponse>(`/v1/apps/${enc(appKey)}/app-map?project_id=${enc(projectId)}`),

  scan: (projectId: string, body: { repository?: string; binding_id?: string; persist?: boolean } = {}) =>
    idPost<AppMapScanResponse>(`/v1/projects/${enc(projectId)}/app-maps/scan`, body),

  save: (appKey: string, body: AppMapSaveInput) =>
    idPut<AppMapResponse>(`/v1/apps/${enc(appKey)}/app-map`, body),

  verifyBaseline: (appKey: string, projectId: string, body: { commit_sha?: string; timeout_seconds?: number } = {}) =>
    idPost<{ project_id: string; app_key: string; baseline: AppMapBaseline; app_map: AppMapEntry }>(
      `/v1/apps/${enc(appKey)}/app-map/baseline`,
      { project_id: projectId, ...body },
    ),
};

export function provenanceLabel(source?: string | null): string {
  switch (source) {
    case "boss_apps_yaml":
      return ".boss/apps.yaml";
    case "workspace_graph":
      return "Workspace graph";
    case "descriptor":
      return "Deploy / CI descriptor";
    case "marker":
      return "Manifest markers";
    case "model":
      return "Model suggestion";
    case "operator":
      return "Operator confirmed";
    case "brief":
      return "Brief / PRD";
    default:
      return source || "Unmapped";
  }
}

export function statusBadge(status: AppMapStatus): { label: string; tone: "ok" | "warn" | "bad" | "muted" } {
  switch (status) {
    case "confirmed":
      return { label: "Confirmed", tone: "ok" };
    case "proposed":
      return { label: "Needs confirmation", tone: "warn" };
    case "stale":
      return { label: "Manifest changed", tone: "warn" };
    case "ambiguous":
      return { label: "Multiple apps found", tone: "bad" };
    default:
      return { label: "Not mapped", tone: "muted" };
  }
}
