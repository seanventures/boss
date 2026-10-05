/**
 * The Log's reads from the browser, through the console's own route handlers (same origin, the
 * console's credentials never reach the page). Every read sends the last ETag it saw; a 304 is
 * "nothing new", which is what a live tail is most of the time.
 */
import {
  mapActivityFacets,
  mapActivityHistogram,
  mapActivityLog,
  mapActivityLogItem,
  type ActivityFacets,
  type ActivityHistogram,
  type ActivityLog,
  type ActivityLogItem,
} from "./activity-log";

export type Got<T> =
  | { ok: true; data: T; etag: string | null; notModified?: false }
  | { ok: true; notModified: true; etag: string | null; data?: undefined }
  | { ok: false; status: number; error: string };

async function read<T>(path: string, params: Record<string, string | number | undefined>,
                       map: (body: unknown) => T, opts: { etag?: string | null; signal?: AbortSignal } = {},
): Promise<Got<T>> {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== "") q.set(k, String(v));
  try {
    const r = await fetch(`${path}?${q.toString()}`, {
      cache: "no-store", signal: opts.signal,
      headers: opts.etag ? { "If-None-Match": opts.etag } : undefined,
    });
    const etag = r.headers.get("etag");
    if (r.status === 304) return { ok: true, notModified: true, etag };
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      const detail = (body as { error?: unknown; detail?: unknown }).error ?? (body as { detail?: unknown }).detail;
      return { ok: false, status: r.status, error: typeof detail === "string" ? detail : `BOSS answered ${r.status}` };
    }
    return { ok: true, data: map(body), etag };
  } catch (e) {
    if ((e as Error)?.name === "AbortError") return { ok: false, status: 0, error: "aborted" };
    return { ok: false, status: 0, error: String((e as Error)?.message ?? e) };
  }
}

export function fetchLogPage(projectId: string, params: Record<string, string | number | undefined>,
                             opts: { etag?: string | null; signal?: AbortSignal } = {}): Promise<Got<ActivityLog>> {
  return read("/api/activity", { project_id: projectId, total: 0, ...params },
              (b) => mapActivityLog(b, projectId), opts);
}

export function fetchHistogram(projectId: string, params: Record<string, string | number | undefined>,
                               opts: { etag?: string | null; signal?: AbortSignal } = {}): Promise<Got<ActivityHistogram | null>> {
  return read("/api/activity/histogram", { project_id: projectId, ...params }, mapActivityHistogram, opts);
}

export function fetchFacets(projectId: string, params: Record<string, string | number | undefined>,
                            opts: { etag?: string | null; signal?: AbortSignal } = {}): Promise<Got<ActivityFacets | null>> {
  return read("/api/activity/facets", { project_id: projectId, ...params }, mapActivityFacets, opts);
}

export type KindCount = { kind: string; n: number };

/** `kind:` completion by prefix, over the window and the current filter (not its kind). */
export function fetchKinds(projectId: string, params: Record<string, string | number | undefined>, prefix: string,
                           opts: { signal?: AbortSignal } = {}): Promise<Got<KindCount[]>> {
  const { kind: _kind, ...rest } = params;
  return read("/api/activity/kinds", { project_id: projectId, ...rest, prefix, limit: 20 }, (b) => {
    const body = b as { kinds?: { kind?: unknown; n?: unknown }[] };
    return (body.kinds ?? []).map((k) => ({ kind: String(k.kind ?? ""), n: Number(k.n ?? 0) })).filter((k) => k.kind);
  }, opts);
}

export type LogRow = { item: ActivityLogItem; labels: Record<string, string> };

export function fetchRow(projectId: string, id: string): Promise<Got<LogRow | null>> {
  return read(`/api/activity/row/${encodeURIComponent(id)}`, { project_id: projectId }, (b) => {
    const body = b as { item?: unknown; labels?: Record<string, string> };
    const item = mapActivityLogItem(body.item)[0];
    return item ? { item, labels: body.labels ?? {} } : null;
  });
}

export type LogContext = { anchor: string; items: ActivityLogItem[]; labels: Record<string, string> };

export function fetchContext(projectId: string, id: string): Promise<Got<LogContext>> {
  return read("/api/activity/context", { project_id: projectId, id, before: 20, after: 20 }, (b) => {
    const body = b as { anchor?: string; items?: unknown[]; labels?: Record<string, string> };
    return { anchor: body.anchor ?? id, items: (body.items ?? []).flatMap(mapActivityLogItem),
             labels: body.labels ?? {} };
  });
}

/** The export URL for the current filter: a plain download, streamed by the API. */
export function exportHref(projectId: string, params: Record<string, string>, format: "csv" | "ndjson"): string {
  const q = new URLSearchParams({ project_id: projectId, ...params, format });
  return `/api/activity/export?${q.toString()}`;
}
