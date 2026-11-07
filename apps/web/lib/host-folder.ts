/**
 * FOLDER ON HOST and INFRA › SERVER (screens 03 and 14): the engine's
 * project_experience/host_folder.py and host_overview.py, through apps/api/app/routers/host_folders.py.
 * Reading a folder or a server's capacity is one read-only ssh session on a verified server.
 */
import { idGet, idPost } from "./identity/api";

const enc = encodeURIComponent;

export type FolderHost = { id: string; name: string; address: string | null; shared: boolean };
export type FolderFacts = {
  path: string; exists: boolean; git?: boolean; remote?: string | null; github?: string | null; branch?: string | null;
  files?: number; top?: string[]; stack?: string[]; build?: string | null; suggested_name?: string; host_id?: string; host?: string;
};
export type FolderRecord = { id: number; project_id: string; host_id: string; host: string; path: string; facts: Partial<FolderFacts> };

export const hostFolders = {
  hosts: (projectId: string) => idGet<{ hosts: FolderHost[] }>(`/v1/projects/${enc(projectId)}/host-folders/hosts`),
  probe: (projectId: string, hostId: string, path: string) =>
    idPost<FolderFacts>(`/v1/projects/${enc(projectId)}/host-folders/probe`, { host_id: hostId, path }),
  register: (projectId: string, hostId: string, path: string) =>
    idPost<FolderRecord>(`/v1/projects/${enc(projectId)}/host-folders`, { host_id: hostId, path }),
};

/** The stack and build lines of "Helm reads it" for a folder. */
export function folderRows(f: FolderFacts): { label: string; sub: string; ok: boolean }[] {
  return [
    { label: "Access", sub: f.exists ? (f.git ? `read · git ${f.branch ?? ""}`.trim() : "read") : "no such folder", ok: f.exists },
    { label: "Stack", sub: f.stack?.length ? f.stack.join(" · ") : "at first build", ok: Boolean(f.stack?.length) },
    { label: "Build & test", sub: f.build ?? "at first build", ok: Boolean(f.build) },
  ];
}

// ── Infra › server ─────────────────────────────────────────────────────────────────────────

export type HostOverview = {
  id: string; name: string; address: string | null; sub: string;
  reachable: { state: "reachable" | "unreachable" | "unknown"; reason: string | null; at: string | null };
  capacity: {
    cpu: { pct: number | null; label: string | null }; memory: { pct: number | null; label: string | null };
    disk: { pct: number | null; label: string | null }; ports: { used: number; range: string | null }; at: string | null;
  };
  projects: { id: string; name: string; initials: string; target_id: string; app: string; env: string; port: number | null;
              health: { word: string; tone: string; status: string | null }; agent: { name: string; role_key: string; live: boolean };
              href: string }[];
  shared_by: number;
  plugins: { key: string; label: string; icon: string }[];
  changes: { id: string; label: string; state: "done" | "failed" | "now"; at: string | null; by: string; href: string; title?: string }[];
};

export const hostOverview = {
  read: (projectId: string, hostId: string) =>
    idGet<HostOverview>(`/v1/infra/hosts/${enc(hostId)}/overview?project_id=${enc(projectId)}`),
  readCapacity: (projectId: string, hostId: string) =>
    idPost<Record<string, unknown>>(`/v1/infra/hosts/${enc(hostId)}/capacity`, { project_id: projectId }),
};

/** "3h", "12d", "1mo" since an ISO time; "" when unknown. */
export function ageShort(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  if (s < 30 * 86400) return `${Math.round(s / 86400)}d`;
  if (s < 365 * 86400) return `${Math.round(s / (30 * 86400))}mo`;
  return `${Math.round(s / (365 * 86400))}y`;
}
