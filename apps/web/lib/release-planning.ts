import { API_URL, PROJECT_ID, authHeaders, resolveProjectId } from "./client";
import { apiWrite } from "./api-write";

export type BoardColumn = "built" | "building" | "planned" | "blocked";

export type BoardItem = {
  id: string;
  projectId: string;
  column: BoardColumn;
  sourceType: string;
  sourceId: string;
  title: string;
  status?: string | null;
  externalUrl?: string | null;
  tracker?: string | null;
  issueKey?: string | null;
  metadata?: Record<string, unknown>;
  sortOrder: number;
};

export type ReleaseIssueLink = {
  tracker: string;
  issueKey: string;
  url?: string | null;
  linkedBy?: string;
  linkedAt?: string;
};

export type ReleasePlan = {
  id: string;
  projectId: string;
  name: string;
  targetDate?: string | null;
  status: string;
  itemIds: string[];
  issueLinks: ReleaseIssueLink[];
  createdBy: string;
  createdAt?: string | null;
  updatedAt?: string | null;
  items?: BoardItem[];
};

export type ReleaseStatusSnapshot = {
  projectId: string;
  builtCount: number;
  todoCount: number;
  inProgress: number;
  blocked: number;
  lastRelease?: ReleasePlan | null;
  lastDeployment?: {
    id: string;
    title?: string;
    status?: string;
    environment?: string;
    pullRequestUrl?: string;
  } | null;
  totals: {
    boardItems: number;
    releasePlans: number;
  };
  apiReachable: boolean;
  blockedReason?: string;
};

export type ReleaseBoard = {
  projectId: string;
  columns: Record<BoardColumn, BoardItem[]>;
  counts: Record<BoardColumn, number>;
  apiReachable: boolean;
  blockedReason?: string;
};

function mapItem(raw: Record<string, unknown>): BoardItem {
  return {
    id: String(raw.id),
    projectId: String(raw.project_id ?? ""),
    column: (raw.column as BoardColumn) || "planned",
    sourceType: String(raw.source_type ?? "unknown"),
    sourceId: String(raw.source_id ?? ""),
    title: String(raw.title ?? raw.id),
    status: raw.status != null ? String(raw.status) : null,
    externalUrl: raw.external_url != null ? String(raw.external_url) : null,
    tracker: raw.tracker != null ? String(raw.tracker) : null,
    issueKey: raw.issue_key != null ? String(raw.issue_key) : null,
    metadata: (raw.metadata as Record<string, unknown>) ?? {},
    sortOrder: Number(raw.sort_order ?? 0),
  };
}

function mapLink(raw: Record<string, unknown>): ReleaseIssueLink {
  return {
    tracker: String(raw.tracker ?? ""),
    issueKey: String(raw.issue_key ?? ""),
    url: raw.url != null ? String(raw.url) : null,
    linkedBy: raw.linked_by != null ? String(raw.linked_by) : undefined,
    linkedAt: raw.linked_at != null ? String(raw.linked_at) : undefined,
  };
}

function mapPlan(raw: Record<string, unknown>): ReleasePlan {
  const links = Array.isArray(raw.issue_links) ? raw.issue_links : [];
  const items = Array.isArray(raw.items) ? raw.items : undefined;
  return {
    id: String(raw.id),
    projectId: String(raw.project_id ?? ""),
    name: String(raw.name ?? ""),
    targetDate: raw.target_date != null ? String(raw.target_date) : null,
    status: String(raw.status ?? "planned"),
    itemIds: Array.isArray(raw.item_ids)
      ? raw.item_ids.map((id) => String(id))
      : [],
    issueLinks: links.map((ln) => mapLink(ln as Record<string, unknown>)),
    createdBy: String(raw.created_by ?? ""),
    createdAt: raw.created_at != null ? String(raw.created_at) : null,
    updatedAt: raw.updated_at != null ? String(raw.updated_at) : null,
    items: items?.map((it) => mapItem(it as Record<string, unknown>)),
  };
}

export function emptyStatus(
  reason?: string,
  projectId = PROJECT_ID,
): ReleaseStatusSnapshot {
  return {
    projectId: resolveProjectId(projectId),
    builtCount: 0,
    todoCount: 0,
    inProgress: 0,
    blocked: 0,
    lastRelease: null,
    lastDeployment: null,
    totals: { boardItems: 0, releasePlans: 0 },
    apiReachable: false,
    blockedReason: reason,
  };
}

export function emptyBoard(
  reason?: string,
  projectId = PROJECT_ID,
): ReleaseBoard {
  return {
    projectId: resolveProjectId(projectId),
    columns: { built: [], building: [], planned: [], blocked: [] },
    counts: { built: 0, building: 0, planned: 0, blocked: 0 },
    apiReachable: false,
    blockedReason: reason,
  };
}

export async function fetchReleaseStatus(
  projectId = PROJECT_ID,
  active = true,
): Promise<ReleaseStatusSnapshot> {
  const pid = resolveProjectId(projectId);
  // Not rendered this request. Same shape the unreachable path returns, no round trip.
  if (!active) return emptyStatus("not requested", pid);
  try {
    const r = await fetch(
      `${API_URL}/v1/release-planning/status?project_id=${encodeURIComponent(pid)}`,
      { cache: "no-store", headers: authHeaders() },
    );
    if (!r.ok) return emptyStatus(`API returned ${r.status}`, pid);
    const raw = (await r.json()) as Record<string, unknown>;
    const last = raw.last_release as Record<string, unknown> | null | undefined;
    const dep = raw.last_deployment as Record<string, unknown> | null | undefined;
    const totals = (raw.totals as Record<string, unknown>) ?? {};
    return {
      projectId: String(raw.project_id ?? pid),
      builtCount: Number(raw.built_count ?? 0),
      todoCount: Number(raw.todo_count ?? 0),
      inProgress: Number(raw.in_progress ?? 0),
      blocked: Number(raw.blocked ?? 0),
      lastRelease: last ? mapPlan(last) : null,
      lastDeployment: dep
        ? {
            id: String(dep.id),
            title: dep.title != null ? String(dep.title) : undefined,
            status: dep.status != null ? String(dep.status) : undefined,
            environment:
              dep.environment != null ? String(dep.environment) : undefined,
            pullRequestUrl:
              dep.pull_request_url != null
                ? String(dep.pull_request_url)
                : undefined,
          }
        : null,
      totals: {
        boardItems: Number(totals.board_items ?? 0),
        releasePlans: Number(totals.release_plans ?? 0),
      },
      apiReachable: true,
    };
  } catch (error) {
    return emptyStatus(
      error instanceof Error ? error.message : "Unknown API error",
      pid,
    );
  }
}

export async function fetchReleaseBoard(
  projectId = PROJECT_ID,
  active = true,
): Promise<ReleaseBoard> {
  const pid = resolveProjectId(projectId);
  // Not rendered this request. Same shape the unreachable path returns, no round trip.
  if (!active) return emptyBoard("not requested", pid);
  try {
    const r = await fetch(
      `${API_URL}/v1/release-planning/board?project_id=${encodeURIComponent(pid)}`,
      { cache: "no-store", headers: authHeaders() },
    );
    if (!r.ok) return emptyBoard(`API returned ${r.status}`, pid);
    const raw = (await r.json()) as Record<string, unknown>;
    const columnsRaw = (raw.columns as Record<string, unknown[]>) ?? {};
    const countsRaw = (raw.counts as Record<string, number>) ?? {};
    const columns = {
      built: (columnsRaw.built ?? []).map((it) =>
        mapItem(it as Record<string, unknown>),
      ),
      building: (columnsRaw.building ?? []).map((it) =>
        mapItem(it as Record<string, unknown>),
      ),
      planned: (columnsRaw.planned ?? []).map((it) =>
        mapItem(it as Record<string, unknown>),
      ),
      blocked: (columnsRaw.blocked ?? []).map((it) =>
        mapItem(it as Record<string, unknown>),
      ),
    };
    return {
      projectId: String(raw.project_id ?? pid),
      columns,
      counts: {
        built: Number(countsRaw.built ?? columns.built.length),
        building: Number(countsRaw.building ?? columns.building.length),
        planned: Number(countsRaw.planned ?? columns.planned.length),
        blocked: Number(countsRaw.blocked ?? columns.blocked.length),
      },
      apiReachable: true,
    };
  } catch (error) {
    return emptyBoard(
      error instanceof Error ? error.message : "Unknown API error",
      pid,
    );
  }
}

export async function fetchReleases(
  projectId = PROJECT_ID,
  active = true,
): Promise<ReleasePlan[]> {
  const pid = resolveProjectId(projectId);
  // Not rendered this request. Same shape the unreachable path returns, no round trip.
  if (!active) return [];
  try {
    const r = await fetch(
      `${API_URL}/v1/release-planning/releases?project_id=${encodeURIComponent(pid)}`,
      { cache: "no-store", headers: authHeaders() },
    );
    if (!r.ok) return [];
    const raw = (await r.json()) as { items?: unknown[] };
    return (raw.items ?? []).map((it) =>
      mapPlan(it as Record<string, unknown>),
    );
  } catch {
    return [];
  }
}

export async function createReleasePlan(input: {
  projectId?: string;
  name: string;
  targetDate?: string;
  itemIds?: string[];
  actor: string;
}): Promise<ReleasePlan | { error: string }> {
  const pid = resolveProjectId(input.projectId);
  try {
    const r = await apiWrite(`${API_URL}/v1/release-planning/releases`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        project_id: pid,
        name: input.name,
        target_date: input.targetDate ?? null,
        item_ids: input.itemIds ?? [],
        actor: input.actor,
      }),
    });
    if (!r.ok) {
      const detail = await r.text();
      return { error: `API ${r.status}: ${detail.slice(0, 200)}` };
    }
    return mapPlan((await r.json()) as Record<string, unknown>);
  } catch (error) {
    return {
      error: error instanceof Error ? error.message : "Unknown API error",
    };
  }
}

export async function linkReleaseIssue(input: {
  projectId?: string;
  releaseId: string;
  tracker: string;
  issueKey: string;
  url?: string;
  actor: string;
}): Promise<ReleasePlan | { error: string }> {
  const pid = resolveProjectId(input.projectId);
  try {
    const r = await apiWrite(
      `${API_URL}/v1/release-planning/releases/${encodeURIComponent(input.releaseId)}/link-issue`,
      {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          project_id: pid,
          tracker: input.tracker,
          issue_key: input.issueKey,
          url: input.url ?? null,
          actor: input.actor,
        }),
      },
    );
    if (!r.ok) {
      const detail = await r.text();
      return { error: `API ${r.status}: ${detail.slice(0, 200)}` };
    }
    return mapPlan((await r.json()) as Record<string, unknown>);
  } catch (error) {
    return {
      error: error instanceof Error ? error.message : "Unknown API error",
    };
  }
}

export async function syncReleaseFromFeatures(
  projectId = PROJECT_ID,
  actor = "ceo",
): Promise<{ ok: boolean; upserted?: number; error?: string }> {
  const pid = resolveProjectId(projectId);
  try {
    const r = await apiWrite(
      `${API_URL}/v1/release-planning/sync-from-features`,
      {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ project_id: pid, actor }),
      },
    );
    if (!r.ok) {
      return { ok: false, error: `API ${r.status}` };
    }
    const body = (await r.json()) as { upserted?: number };
    return { ok: true, upserted: body.upserted };
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error.message : "Unknown API error",
    };
  }
}

export async function syncReleaseFromTrackers(
  projectId = PROJECT_ID,
  actor = "ceo",
): Promise<{ ok: boolean; upserted?: number; error?: string }> {
  const pid = resolveProjectId(projectId);
  try {
    const r = await apiWrite(
      `${API_URL}/v1/release-planning/sync-from-trackers`,
      {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ project_id: pid, actor }),
      },
    );
    if (!r.ok) {
      return { ok: false, error: `API ${r.status}` };
    }
    const body = (await r.json()) as { upserted?: number; ok?: boolean };
    return { ok: body.ok !== false, upserted: body.upserted };
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error.message : "Unknown API error",
    };
  }
}
