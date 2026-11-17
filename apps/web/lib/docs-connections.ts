/**
 * DOCUMENT SYSTEMS IN THE CONSOLE (BOSSAPP-1384): Setup › Confluence. One client for the routes in
 * apps/api/app/routers/docs_connections.py — the org's Confluence is connected ONCE and every
 * project reads through it (like the ticket systems' org-wide list).
 *
 * Reads answer `{ok, data}` or `{ok:false, error}` with `error` always a plain sentence. Writes go
 * through obsWrite (one press, one effect, an Idempotency-Key). A token is sent once and never read
 * back: the API answers `last4` and a fingerprint only, and only to someone who manages it.
 */
import { obsWrite, type WriteResult } from "../app/observability/obs-write";

type R = Record<string, unknown>;
const o = (v: unknown): R => (v && typeof v === "object" && !Array.isArray(v) ? (v as R) : {});
const a = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const s = (v: unknown, d = ""): string => (typeof v === "string" ? v : typeof v === "number" ? String(v) : d);
const strs = (v: unknown) => a(v).map((x) => s(x)).filter(Boolean);
const enc = encodeURIComponent;

export type DocsConnection = {
  id: string; provider: string; label: string; status: "connected" | "disconnected" | "needs_attention";
  base_url: string; deployment: string; auth_method: "basic" | "bearer"; email: string; acts_as: string | null;
  default_spaces: string[]; knowledge_spaces: string[];
  sync_state: Record<string, { synced_at: string; pages: number }>;
  home_project: { id: string; name: string }; reach: "project" | "org";
  last_tested_at: string | null; last_test_ok: boolean | null; can_manage: boolean;
  last4: string | null; credential_from: string;
};
export type DocsSpace = { id: string; key: string; name: string };
export type DocsRead<T> = { ok: true; data: T } | { ok: false; error: string; code: string; denied: boolean };

export const parseDocsConnection = (v: unknown): DocsConnection => {
  const x = o(v), home = o(x.home_project), sync = o(x.sync_state);
  return {
    id: s(x.id), provider: s(x.provider, "confluence"), label: s(x.label, "Confluence"),
    status: (["connected", "needs_attention"].includes(s(x.status)) ? s(x.status) : "disconnected") as DocsConnection["status"],
    base_url: s(x.base_url), deployment: s(x.deployment, "cloud"), auth_method: s(x.auth_method) === "bearer" ? "bearer" : "basic",
    email: s(x.email), acts_as: s(x.acts_as) || null, default_spaces: strs(x.default_spaces), knowledge_spaces: strs(x.knowledge_spaces),
    sync_state: Object.fromEntries(Object.entries(sync).map(([k, val]) => [k, { synced_at: s(o(val).synced_at), pages: Number(o(val).pages) || 0 }])),
    home_project: { id: s(home.id), name: s(home.name) }, reach: s(x.reach) === "org" ? "org" : "project",
    last_tested_at: s(x.last_tested_at) || null, last_test_ok: typeof x.last_test_ok === "boolean" ? x.last_test_ok : null,
    can_manage: x.can_manage === true, last4: s(x.last4) || null, credential_from: s(x.credential_from),
  };
};
export const parseDocsConnections = (v: unknown): DocsConnection[] => a(o(v).connections).map(parseDocsConnection);
export const parseDocsSpaces = (v: unknown): DocsSpace[] =>
  a(o(v).spaces).map((x) => ({ id: s(o(x).id), key: s(o(x).key), name: s(o(x).name) })).filter((x) => x.key);

/** A GET that always answers in words: the typed error's message, or permission denied. */
export async function docsRead<T>(path: string, parse: (v: unknown) => T): Promise<DocsRead<T>> {
  try {
    const r = await fetch(path, { cache: "no-store" });
    const body = await r.json().catch(() => ({}));
    if (r.ok) return { ok: true, data: parse(body) };
    const d = o(body).detail;
    const code = typeof d === "object" && d ? s(o(d).error) : "";
    const msg = typeof d === "string" ? d : s(o(d).message);
    const denied = r.status === 403;
    return { ok: false, code, denied, error: denied ? (msg || "You don't have permission to see this. Ask an admin.") : msg || `BOSS answered ${r.status}.` };
  } catch (e) {
    return { ok: false, code: "network", denied: false, error: `BOSS could not be reached (${(e as Error)?.message ?? e}).` };
  }
}

export const readDocsConnections = (projectId: string) =>
  docsRead(`/v1/docs-connections?project_id=${enc(projectId)}`, parseDocsConnections);
export const readDocsSpaces = (projectId: string) =>
  docsRead(`/v1/docs-connections/spaces?project_id=${enc(projectId)}`, parseDocsSpaces);

/** "acme.atlassian.net": a site's host, for the row's second line. */
export const siteHost = (url: string) => url.replace(/^https?:\/\//, "").replace(/\/.*$/, "");

/** One line for the folded Setup section: "Confluence · acme.atlassian.net · 2 spaces". */
export function docsSummary(c: DocsConnection | null): string {
  if (!c || c.status === "disconnected") return "Not connected";
  const n = c.knowledge_spaces.length;
  return `Confluence · ${siteHost(c.base_url)}${n ? ` · ${n} space${n === 1 ? "" : "s"} in knowledge` : ""}`;
}

// ── writes ───────────────────────────────────────────────────────────────────────────────────
const press = (scope: string, label: string) => ({ scope: `docs:${scope}`, label });

export type DocsConnectInput = {
  base_url: string; auth_method: "basic" | "bearer"; email: string; api_token: string;
  default_spaces: string[]; use_jira_credential: boolean;
};
/** Connect: BOSS tests first (nothing is stored when the test fails); the token goes to the vault. */
export const connectDocs = (projectId: string, i: DocsConnectInput): Promise<WriteResult> =>
  obsWrite("/v1/docs-connections", {
    project_id: projectId, provider: "confluence", base_url: i.base_url, auth_method: i.auth_method,
    fields: i.email ? { email: i.email } : {}, secrets: i.api_token ? { api_token: i.api_token } : {},
    default_spaces: i.default_spaces, use_jira_credential: i.use_jira_credential,
  }, press("connect", "Connect"));
/** Test connection: one cheap call to the site. */
export const testDocs = (projectId: string): Promise<WriteResult> =>
  obsWrite("/v1/docs-connections/test", { project_id: projectId }, press("test", "Test"));
/** Change the default spaces (agent searches) and the spaces kept in knowledge. */
export const saveDocsSpaces = (projectId: string, patch: { default_spaces?: string[]; knowledge_spaces?: string[] }): Promise<WriteResult> =>
  obsWrite("/v1/docs-connections", { project_id: projectId, ...patch }, press("spaces", "Save"), "PATCH");
export const disconnectDocs = (projectId: string): Promise<WriteResult> =>
  obsWrite(`/v1/docs-connections?project_id=${enc(projectId)}`, {}, press("off", "Disconnect"), "DELETE");
/** Sync the chosen spaces into this project's knowledge now. */
export const syncDocsKnowledge = (projectId: string, full = false): Promise<WriteResult> =>
  obsWrite("/v1/docs-connections/sync", { project_id: projectId, full }, press("sync", "Sync"));
