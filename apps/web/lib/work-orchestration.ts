import { API_URL, authHeaders, resolveProjectId } from "./client";

export type RankedWorkItem = {
  id: string;
  subject_type: string;
  subject_id: string;
  title: string;
  description?: string;
  status: string;
  priority_band: "p0" | "p1" | "p2" | "p3" | "p4";
  effective_score: number;
  strategic_value: number;
  customer_impact: number;
  urgency: number;
  cost_of_delay: number;
  risk_reduction: number;
  dependency_pressure: number;
  confidence: number;
  manual_adjustment: number;
  rationale: string;
  workload_class: string;
  estimated_units: number;
  allocated_units: number;
  allocation_status: string;
  owner_actor?: string;
  due_at?: string;
};

export type ResourceQuota = {
  id: string;
  scope_type: string;
  scope_id: string;
  resource_kind: string;
  reserved_min: number;
  soft_limit: number;
  hard_limit: number;
  burst_limit: number;
  allocation_weight: number;
  borrow_enabled: boolean;
  status: string;
  used_units: number;
  granted_units: number;
};

export type WorkAllocation = {
  id: string;
  work_item_id: string;
  work_title: string;
  resource_kind: string;
  requested_units: number;
  granted_units: number;
  used_units: number;
  status: string;
  reason: string;
  score_snapshot: number;
};

export type OrchestratedJob = {
  id: string;
  kind: string;
  status: string;
  priority: number;
  priority_score?: number;
  workload_class: string;
  resource_units: number;
  lease_owner?: string;
  lease_expires_at?: string;
  heartbeat_at?: string;
  run_after: string;
  work_item_id?: string;
  work_title?: string;
  tracker_binding_id?: string;
  allocation_id?: string;
  external_key?: string;
  external_url?: string;
  tracker_provider?: string;
};

export type TrackerProvider = {
  id: string;
  provider_key: string;
  display_name: string;
  status: string;
  is_default: boolean;
  enforcement_mode: string;
  capabilities: string[];
  configuration: Record<string, unknown>;
  binding_count: number;
};

export type TrackerBinding = {
  id: string;
  work_item_id: string;
  work_title: string;
  provider_key: string;
  provider_name: string;
  enforcement_mode: string;
  external_key?: string;
  external_url?: string;
  external_state?: string;
  sync_status: string;
  priority_band: string;
  effective_score: number;
};

export type DeliveryPlugin = {
  provider: string;
  display_name: string;
  capabilities: string[];
};

export type BotIdentity = {
  id: string;
  provider: string;
  agent_key: string;
  display_name: string;
  external_identity?: string;
  status: string;
};

export type StatusChannel = {
  id: string;
  provider: string;
  channel_key: string;
  display_name: string;
  purpose: string;
  external_channel_id?: string;
  external_url?: string;
  bot_identity_id?: string;
  bot_name?: string;
  agent_key?: string;
  event_kinds: string[];
  status: string;
};

export type PresenceEvent = {
  id: string;
  provider: string;
  event_kind: string;
  subject_type: string;
  subject_id: string;
  status: string;
  summary: string;
  external_url?: string;
  channel_name?: string;
  bot_name?: string;
  agent_key?: string;
  created_at: string;
};

export type WorkOrchestrationFeed = {
  projectId: string;
  apiReachable: boolean;
  blockedReason?: string;
  summary: {
    ranked_work: number;
    p0_p1_work: number;
    active_jobs: number;
    tracked_jobs: number;
    throttled_work: number;
    pending_tracker_sync: number;
    active_channels: number;
  };
  workItems: RankedWorkItem[];
  quotas: ResourceQuota[];
  allocations: WorkAllocation[];
  jobs: OrchestratedJob[];
  trackerProviders: TrackerProvider[];
  trackerBindings: TrackerBinding[];
  deliveryPlugins: DeliveryPlugin[];
  botIdentities: BotIdentity[];
  statusChannels: StatusChannel[];
  presenceEvents: PresenceEvent[];
};

export async function fetchWorkOrchestrationFeed(
  projectId?: string,
): Promise<WorkOrchestrationFeed> {
  const resolved = resolveProjectId(projectId);
  try {
    const response = await fetch(
      `${API_URL}/v1/work-orchestration/overview?project_id=${encodeURIComponent(resolved)}`,
      { headers: authHeaders(), cache: "no-store" },
    );
    if (!response.ok) {
      return emptyFeed(resolved, `Work Orchestration API returned ${response.status}.`);
    }
    const payload = (await response.json()) as Record<string, unknown>;
    return {
      projectId: resolved,
      apiReachable: true,
      summary: payload.summary as WorkOrchestrationFeed["summary"],
      workItems: (payload.work_items as RankedWorkItem[]) ?? [],
      quotas: (payload.quotas as ResourceQuota[]) ?? [],
      allocations: (payload.allocations as WorkAllocation[]) ?? [],
      jobs: (payload.jobs as OrchestratedJob[]) ?? [],
      trackerProviders: (payload.tracker_providers as TrackerProvider[]) ?? [],
      trackerBindings: (payload.tracker_bindings as TrackerBinding[]) ?? [],
      deliveryPlugins: (payload.delivery_plugins as DeliveryPlugin[]) ?? [],
      botIdentities: (payload.bot_identities as BotIdentity[]) ?? [],
      statusChannels: (payload.status_channels as StatusChannel[]) ?? [],
      presenceEvents: (payload.presence_events as PresenceEvent[]) ?? [],
    };
  } catch (error) {
    return emptyFeed(
      resolved,
      error instanceof Error ? error.message : "Work Orchestration API is unavailable.",
    );
  }
}

function emptyFeed(projectId: string, blockedReason: string): WorkOrchestrationFeed {
  return {
    projectId,
    apiReachable: false,
    blockedReason,
    summary: {
      ranked_work: 0,
      p0_p1_work: 0,
      active_jobs: 0,
      tracked_jobs: 0,
      throttled_work: 0,
      pending_tracker_sync: 0,
      active_channels: 0,
    },
    workItems: [],
    quotas: [],
    allocations: [],
    jobs: [],
    trackerProviders: [],
    trackerBindings: [],
    deliveryPlugins: [],
    botIdentities: [],
    statusChannels: [],
    presenceEvents: [],
  };
}
