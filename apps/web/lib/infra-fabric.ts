/**
 * Mission Control surfaces for multi-cloud catalog, elastic fabric,
 * Goldeneye PaaS, and provider usage ledger.
 *
 * Community-safe: catalog + BYOK self-managed fleets only. No fake managed
 * capacity — real host effects and real provision flags are reported truthfully.
 */

import { BossApiError } from "@boss/api-client";

import {
  API_URL,
  authHeaders,
  client,
  PROJECT_ID,
  resolveProjectId,
} from "./client";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type MultiCloudProviderItem = {
  providerKey: string;
  displayName: string;
  adapterStatus: string;
  categories: string[];
  skillCount: number;
  infraSkillCount: number;
  modelSkillCount: number;
  defaultAuthMethod: string;
  connectionTest?: string;
};

export type MultiCloudCatalogFeed = {
  apiReachable: boolean;
  blockedReason?: string;
  version: number;
  product?: string;
  providerCount: number;
  skillCount: number;
  providers: MultiCloudProviderItem[];
  gates: {
    infraRealProvision?: string;
    sshHostDeploy?: string;
  };
};

export type ElasticInstanceCounts = {
  total: number;
  active: number;
  byStatus: Record<string, number>;
  byKind: Record<string, number>;
  byProvider: Record<string, number>;
};

export type ElasticStatusFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  status: "ok" | "not_configured" | "error";
  configured: boolean;
  hostConnected: boolean;
  poolCount: number;
  instanceCounts: ElasticInstanceCounts;
  /** True only when BOSS_INFRA_REAL_PROVISION_ALLOWED is on — Community default is false. */
  realHostEffects: boolean;
  message?: string;
  pools: Array<{
    id: string;
    poolKey: string;
    kind: string;
    status: string;
    currentWorkers: number;
    desiredWorkers: number;
  }>;
};

export type GoldeneyeStatusFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  status: "ok" | "not_configured" | "error";
  configured: boolean;
  hostConnected: boolean;
  product?: string;
  nodeCount: number;
  appCount: number;
  packageInstallCount: number;
  readyNodes: number;
  /** True only when real SSH/host deploy is allowed — Community default is false. */
  realHostEffects: boolean;
  kitVersion: string;
  modular: boolean;
  packageCount: number;
};

export type ProviderUsageProviderRollup = {
  provider: string;
  eventCount: number;
  costUsdMicros: number;
  categories: Array<{
    category: string;
    unit: string;
    eventCount: number;
    quantitySum: number;
    costUsdMicros: number;
  }>;
  billing?: {
    costEventCount: number;
    amountUsdMicros: number;
  };
};

export type ProviderUsageSummaryFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  status: "ok" | "not_configured" | "error";
  configured: boolean;
  providers: ProviderUsageProviderRollup[];
  generatedAt?: string;
};

export type InfraFabricFeed = {
  projectId: string;
  multiCloud: MultiCloudCatalogFeed;
  elastic: ElasticStatusFeed;
  goldeneye: GoldeneyeStatusFeed;
  providerUsage: ProviderUsageSummaryFeed;
  partialFailures: string[];
  refreshedAt: string;
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function failureReason(error: unknown): string {
  if (error instanceof BossApiError) {
    return `HTTP ${error.status}`;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "unknown error";
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function asNumber(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function asBool(value: unknown, fallback = false): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string");
}

function asCountMap(value: unknown): Record<string, number> {
  const record = asRecord(value);
  const out: Record<string, number> = {};
  for (const [key, raw] of Object.entries(record)) {
    out[key] = asNumber(raw, 0);
  }
  return out;
}

// ---------------------------------------------------------------------------
// Empty / blocked feeds
// ---------------------------------------------------------------------------

export function emptyMultiCloudCatalog(reason?: string): MultiCloudCatalogFeed {
  return {
    apiReachable: false,
    blockedReason: reason,
    version: 0,
    providerCount: 0,
    skillCount: 0,
    providers: [],
    gates: {},
  };
}

export function emptyElasticStatus(
  reason?: string,
  projectId = PROJECT_ID,
  status: ElasticStatusFeed["status"] = "not_configured",
): ElasticStatusFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: status !== "error",
    blockedReason: reason,
    status,
    configured: false,
    hostConnected: false,
    poolCount: 0,
    instanceCounts: {
      total: 0,
      active: 0,
      byStatus: {},
      byKind: {},
      byProvider: {},
    },
    realHostEffects: false,
    pools: [],
  };
}

export function emptyGoldeneyeStatus(
  reason?: string,
  projectId = PROJECT_ID,
  status: GoldeneyeStatusFeed["status"] = "not_configured",
): GoldeneyeStatusFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: status !== "error",
    blockedReason: reason,
    status,
    configured: false,
    hostConnected: false,
    nodeCount: 0,
    appCount: 0,
    packageInstallCount: 0,
    readyNodes: 0,
    realHostEffects: false,
    kitVersion: "",
    modular: false,
    packageCount: 0,
  };
}

export function emptyProviderUsageSummary(
  reason?: string,
  projectId = PROJECT_ID,
  status: ProviderUsageSummaryFeed["status"] = "not_configured",
): ProviderUsageSummaryFeed {
  return {
    projectId: resolveProjectId(projectId),
    apiReachable: status !== "error",
    blockedReason: reason,
    status,
    configured: false,
    providers: [],
  };
}

// ---------------------------------------------------------------------------
// Mappers
// ---------------------------------------------------------------------------

function mapMultiCloudProvider(raw: unknown): MultiCloudProviderItem | null {
  const item = asRecord(raw);
  const providerKey = asString(item.provider_key);
  if (!providerKey) return null;
  const infra = Array.isArray(item.infra_skills) ? item.infra_skills.length : 0;
  const models = Array.isArray(item.model_skills) ? item.model_skills.length : 0;
  return {
    providerKey,
    displayName: asString(item.display_name, providerKey),
    adapterStatus: asString(item.adapter_status, "unknown"),
    categories: asStringArray(item.categories),
    skillCount: infra + models,
    infraSkillCount: infra,
    modelSkillCount: models,
    defaultAuthMethod: asString(item.default_auth_method, "unknown"),
    connectionTest: asString(item.connection_test) || undefined,
  };
}

function mapElasticPools(
  raw: unknown,
): ElasticStatusFeed["pools"] {
  if (!Array.isArray(raw)) return [];
  return raw.map((entry) => {
    const pool = asRecord(entry);
    return {
      id: asString(pool.id),
      poolKey: asString(pool.pool_key ?? pool.poolKey),
      kind: asString(pool.kind, "local"),
      status: asString(pool.status, "unknown"),
      currentWorkers: asNumber(pool.current_workers ?? pool.currentWorkers),
      desiredWorkers: asNumber(pool.desired_workers ?? pool.desiredWorkers),
    };
  });
}

function mapUsageProviders(raw: unknown): ProviderUsageProviderRollup[] {
  if (!Array.isArray(raw)) return [];
  return raw.map((entry) => {
    const item = asRecord(entry);
    const categoriesRaw = Array.isArray(item.categories) ? item.categories : [];
    const billing = asRecord(item.billing);
    return {
      provider: asString(item.provider, "unknown"),
      eventCount: asNumber(item.event_count ?? item.eventCount),
      costUsdMicros: asNumber(item.cost_usd_micros ?? item.costUsdMicros),
      categories: categoriesRaw.map((cat) => {
        const c = asRecord(cat);
        return {
          category: asString(c.category),
          unit: asString(c.unit, "count"),
          eventCount: asNumber(c.event_count ?? c.eventCount),
          quantitySum: asNumber(c.quantity_sum ?? c.quantitySum),
          costUsdMicros: asNumber(c.cost_usd_micros ?? c.costUsdMicros),
        };
      }),
      billing:
        Object.keys(billing).length > 0
          ? {
              costEventCount: asNumber(
                billing.cost_event_count ?? billing.costEventCount,
              ),
              amountUsdMicros: asNumber(
                billing.amount_usd_micros ?? billing.amountUsdMicros,
              ),
            }
          : undefined,
    };
  });
}

// ---------------------------------------------------------------------------
// Fetch helpers
// ---------------------------------------------------------------------------

/**
 * GET /v1/multi-cloud/catalog — unified provider + skill inventory (read-only).
 */
export async function fetchMultiCloudCatalog(): Promise<MultiCloudCatalogFeed> {
  try {
    const response = await fetch(`${API_URL}/v1/multi-cloud/catalog`, {
      headers: authHeaders(),
      cache: "no-store",
    });
    if (!response.ok) {
      return emptyMultiCloudCatalog(
        `Multi-cloud catalog returned HTTP ${response.status}`,
      );
    }
    const payload = asRecord(await response.json());
    const providers = (Array.isArray(payload.providers) ? payload.providers : [])
      .map(mapMultiCloudProvider)
      .filter((item): item is MultiCloudProviderItem => item !== null);
    const skillIndex = asRecord(payload.skill_index);
    const gates = asRecord(payload.gates);
    return {
      apiReachable: true,
      version: asNumber(payload.version, 1),
      product: asString(payload.product) || undefined,
      providerCount: asNumber(payload.provider_count, providers.length),
      skillCount: Object.keys(skillIndex).length,
      providers,
      gates: {
        infraRealProvision: asString(gates.infra_real_provision) || undefined,
        sshHostDeploy: asString(gates.ssh_host_deploy) || undefined,
      },
    };
  } catch (error) {
    return emptyMultiCloudCatalog(failureReason(error));
  }
}

/**
 * GET /v1/elastic/status — pool/instance fabric overview for a project.
 */
export async function fetchElasticStatus(
  projectId = PROJECT_ID,
): Promise<ElasticStatusFeed> {
  const resolved = resolveProjectId(projectId);
  try {
    const payload = asRecord(await client.getElasticStatus(resolved));
    const counts = asRecord(payload.instance_counts);
    const pools = mapElasticPools(payload.pools);
    const host = asRecord(payload.host);
    const configured = pools.length > 0 || asNumber(counts.total) > 0;
    return {
      projectId: asString(payload.project_id, resolved),
      apiReachable: true,
      status: configured ? "ok" : "not_configured",
      configured,
      hostConnected: asBool(host.connected, false),
      poolCount: pools.length,
      instanceCounts: {
        total: asNumber(counts.total),
        active: asNumber(counts.active),
        byStatus: asCountMap(counts.by_status),
        byKind: asCountMap(counts.by_kind),
        byProvider: asCountMap(counts.by_provider),
      },
      realHostEffects: asBool(payload.infra_real_provision_allowed, false),
      message: asString(payload.message) || undefined,
      pools,
    };
  } catch (error) {
    const reason = failureReason(error);
    const schemaMiss =
      error instanceof BossApiError && (error.status === 500 || error.status === 503);
    return emptyElasticStatus(
      schemaMiss ? "Elastic fabric is not configured on this runtime." : reason,
      resolved,
      schemaMiss ? "not_configured" : "error",
    );
  }
}

/**
 * GET /v1/goldeneye/status — bare-metal PaaS fleet overview.
 */
export async function fetchGoldeneyeStatus(
  projectId = PROJECT_ID,
): Promise<GoldeneyeStatusFeed> {
  const resolved = resolveProjectId(projectId);
  try {
    const response = await fetch(
      `${API_URL}/v1/goldeneye/status?project_id=${encodeURIComponent(resolved)}`,
      { headers: authHeaders(), cache: "no-store" },
    );
    if (!response.ok) {
      const schemaMiss = response.status === 500 || response.status === 503;
      return emptyGoldeneyeStatus(
        schemaMiss
          ? "Goldeneye fleet is not configured on this runtime."
          : `Goldeneye status returned HTTP ${response.status}`,
        resolved,
        schemaMiss ? "not_configured" : "error",
      );
    }
    const payload = asRecord(await response.json());
    const kit = asRecord(payload.kit);
    const host = asRecord(payload.host);
    const nodeCount = asNumber(payload.node_count);
    return {
      projectId: asString(payload.project_id, resolved),
      apiReachable: true,
      status: nodeCount > 0 ? "ok" : "not_configured",
      configured: nodeCount > 0,
      hostConnected: asBool(host.connected, false),
      product: asString(payload.product) || undefined,
      nodeCount,
      appCount: asNumber(payload.app_count),
      packageInstallCount: asNumber(payload.package_install_count),
      readyNodes: asNumber(payload.ready_nodes),
      realHostEffects: asBool(payload.real_host_effects, false),
      kitVersion: asString(kit.version, "unknown"),
      modular: asBool(kit.modular, true),
      packageCount: asNumber(kit.package_count),
    };
  } catch (error) {
    return emptyGoldeneyeStatus(failureReason(error), resolved, "error");
  }
}

/**
 * GET /v1/provider-usage/summary — aggregated usage + billing per provider.
 */
export async function fetchProviderUsageSummary(
  projectId = PROJECT_ID,
): Promise<ProviderUsageSummaryFeed> {
  const resolved = resolveProjectId(projectId);
  try {
    const payload = asRecord(await client.getProviderUsageSummary(resolved));
    const providers = mapUsageProviders(payload.providers);
    return {
      projectId: asString(payload.project_id, resolved),
      apiReachable: true,
      status: providers.length > 0 ? "ok" : "not_configured",
      configured: providers.length > 0,
      providers,
      generatedAt: asString(payload.generated_at) || undefined,
    };
  } catch (error) {
    const reason = failureReason(error);
    const schemaMiss =
      error instanceof BossApiError && (error.status === 500 || error.status === 503);
    return emptyProviderUsageSummary(
      schemaMiss ? "Provider usage ledger is not configured on this runtime." : reason,
      resolved,
      schemaMiss ? "not_configured" : "error",
    );
  }
}

/**
 * Combined Mission Control feed for the Infra Fabric panel.
 * Partial failures stay soft — each surface degrades independently.
 */
export async function fetchInfraFabricFeed(
  projectId = PROJECT_ID,
  active = true,
): Promise<InfraFabricFeed> {
  const resolved = resolveProjectId(projectId);
  // Not rendered this request. Four sub-fetches skipped, each replaced by the same
  // not-configured feed its own unreachable path returns.
  if (!active) {
    return {
      projectId: resolved,
      multiCloud: emptyMultiCloudCatalog("not requested"),
      elastic: emptyElasticStatus("not requested", resolved),
      goldeneye: emptyGoldeneyeStatus("not requested", resolved),
      providerUsage: emptyProviderUsageSummary("not requested", resolved),
      partialFailures: [],
      refreshedAt: "",
    };
  }
  const [multiCloud, elastic, goldeneye, providerUsage] = await Promise.all([
    fetchMultiCloudCatalog(),
    fetchElasticStatus(resolved),
    fetchGoldeneyeStatus(resolved),
    fetchProviderUsageSummary(resolved),
  ]);

  const partialFailures: string[] = [];
  if (!multiCloud.apiReachable && multiCloud.blockedReason) {
    partialFailures.push(`Multi-cloud: ${multiCloud.blockedReason}`);
  }
  if (elastic.status === "error" && elastic.blockedReason) {
    partialFailures.push(`Elastic: ${elastic.blockedReason}`);
  }
  if (goldeneye.status === "error" && goldeneye.blockedReason) {
    partialFailures.push(`Goldeneye: ${goldeneye.blockedReason}`);
  }
  if (providerUsage.status === "error" && providerUsage.blockedReason) {
    partialFailures.push(`Provider usage: ${providerUsage.blockedReason}`);
  }

  return {
    projectId: resolved,
    multiCloud,
    elastic,
    goldeneye,
    providerUsage,
    partialFailures,
    refreshedAt: new Date().toISOString(),
  };
}
