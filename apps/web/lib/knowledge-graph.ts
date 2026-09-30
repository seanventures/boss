import { API_URL, PROJECT_ID, authHeaders, resolveProjectId } from "./client";

export type KgNode = {
  id: string;
  kind: string;
  node_key: string;
  name?: string;
  layer?: string;
  confidence?: number;
  attrs?: Record<string, unknown>;
};

export type KgEdge = {
  from?: string;
  to?: string;
  kind: string;
  dir?: string;
  confidence?: number;
  src_id?: string;
  dst_id?: string;
};

export type GraphHealth = {
  project_id: string;
  healthy: boolean;
  status: string;
  node_count: number;
  edge_count: number;
  nodes_by_kind: Record<string, number>;
  nodes_by_layer: Record<string, number>;
  edges_by_kind: Record<string, number>;
  last_node_seen?: string | null;
  last_edge_seen?: string | null;
  hints: string[];
};

export type RcaPack = {
  project_id: string;
  found: boolean;
  root: KgNode | null;
  subgraph: {
    nodes: KgNode[];
    edges: KgEdge[];
    node_count: number;
    edge_count: number;
  };
  known_causes: Array<{
    kind: string;
    node_key: string;
    name?: string;
    confidence?: number;
  }>;
  known_causes_count: number;
  graph_health: {
    status: string;
    node_count: number;
    edge_count: number;
  };
};

export type GraphSummary = {
  project_id: string;
  health: GraphHealth;
  rca?: RcaPack;
};

export type KnowledgeGraphFeed = {
  projectId: string;
  apiReachable: boolean;
  health: GraphHealth | null;
  nodes: KgNode[];
};

async function getJson<T>(path: string): Promise<T | null> {
  try {
    const r = await fetch(`${API_URL}${path}`, {
      headers: authHeaders(),
      cache: "no-store",
    });
    if (!r.ok) return null;
    return (await r.json()) as T;
  } catch {
    return null;
  }
}

async function postJson<T>(path: string, body: unknown): Promise<T | null> {
  try {
    const r = await fetch(`${API_URL}${path}`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
      cache: "no-store",
    });
    if (!r.ok) return null;
    return (await r.json()) as T;
  } catch {
    return null;
  }
}

export async function fetchGraphHealth(
  projectId = PROJECT_ID,
): Promise<GraphHealth | null> {
  const q = `project_id=${encodeURIComponent(resolveProjectId(projectId))}`;
  return getJson<GraphHealth>(`/v1/knowledge/graph/health?${q}`);
}

export async function fetchGraphNodes(
  projectId = PROJECT_ID,
  opts?: { kind?: string; limit?: number },
): Promise<KgNode[]> {
  const q = new URLSearchParams({
    project_id: resolveProjectId(projectId),
    limit: String(opts?.limit ?? 50),
  });
  if (opts?.kind) q.set("kind", opts.kind);
  const body = await getJson<{ items: KgNode[] }>(
    `/v1/knowledge/graph/nodes?${q}`,
  );
  return body?.items ?? [];
}

export async function fetchGraphSummary(
  projectId = PROJECT_ID,
  opts?: { kind?: string; nodeKey?: string; depth?: number },
): Promise<GraphSummary | null> {
  const q = new URLSearchParams({
    project_id: resolveProjectId(projectId),
  });
  if (opts?.kind) q.set("kind", opts.kind);
  if (opts?.nodeKey) q.set("node_key", opts.nodeKey);
  if (opts?.depth != null) q.set("depth", String(opts.depth));
  return getJson<GraphSummary>(`/v1/observability-suite/graph-summary?${q}`);
}

export async function fetchKnownCauses(
  projectId: string,
  service: string,
  limit = 10,
): Promise<KgNode[]> {
  const q = new URLSearchParams({
    project_id: resolveProjectId(projectId),
    service,
    limit: String(limit),
  });
  const body = await getJson<{ items: KgNode[] }>(
    `/v1/knowledge/graph/known-causes?${q}`,
  );
  return body?.items ?? [];
}

export async function seedGraphFromInfra(
  projectId = PROJECT_ID,
  actor = "executive_console",
): Promise<{ seeded?: { resources: number }; health?: GraphHealth } | null> {
  return postJson(`/v1/observability-suite/graph-seed`, {
    project_id: resolveProjectId(projectId),
    actor,
  });
}

export async function fetchKnowledgeGraphFeed(
  projectId = PROJECT_ID,
  active = true,
): Promise<KnowledgeGraphFeed> {
  const resolved = resolveProjectId(projectId);
  // Not rendered this request. Same shape the unreachable path returns, no round trip.
  if (!active) {
    return { projectId: resolved, apiReachable: false, health: null, nodes: [] };
  }
  const [health, nodes] = await Promise.all([
    fetchGraphHealth(resolved),
    fetchGraphNodes(resolved, { limit: 30 }),
  ]);
  return {
    projectId: resolved,
    apiReachable: health !== null,
    health,
    nodes,
  };
}
