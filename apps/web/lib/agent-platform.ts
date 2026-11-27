/**
 * AGENT PLATFORM CONSOLE CLIENT (AP-P3, BOSSAPP-1054).
 *
 * All reads detect 401/403 (`isLocked`) for `<LockedPanel>` / `<ReadFailure>`, and all 18 writes
 * pass `actor` from the signed-in caller through `apiWrite` (never a literal actor) and are
 * registered in `lib/identity/write-permissions.ts`.
 */
import { couldNot, whatToDo } from "./action-result";
import { errorSentence } from "./agents";
import { apiWrite, type Press } from "./api-write";
import { lockedReason, readFailureLabel } from "../components/locked-panel";

export type ReadResult<T> =
  | { ok: true; data: T }
  | { ok: false; locked: true; status: number; reason: string; error: string }
  | { ok: false; locked?: false; status: number; error: string };

export type WriteResult<T = Record<string, unknown>> =
  | { ok: true; status: number; data: T }
  | { ok: false; status: number; error: string; data?: Record<string, unknown> };

const enc = encodeURIComponent;
const press = (scope: string, label: string): Press => ({ scope: `ap:${scope}`, label });

async function readJson<T>(url: string, actionLabel: string): Promise<ReadResult<T>> {
  try {
    const res = await fetch(url, { cache: "no-store" });
    if (readFailureLabel(res.status) === "locked") {
      const reason = await lockedReason(res);
      return { ok: false, locked: true, status: res.status, reason, error: reason };
    }
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      const msg = errorSentence(data, res.status) || whatToDo(res.status);
      return { ok: false, status: res.status, error: couldNot(actionLabel, msg) };
    }
    return { ok: true, data: data as T };
  } catch (e) {
    return { ok: false, status: 0, error: couldNot(actionLabel, String((e as Error)?.message ?? e)) };
  }
}

async function writeJson<T = Record<string, unknown>>(
  url: string,
  method: "POST" | "PUT" | "PATCH" | "DELETE",
  body: Record<string, unknown> | undefined,
  p: Press,
  actionLabel: string,
): Promise<WriteResult<T>> {
  try {
    const res = await apiWrite(url, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    }, { press: p });
    const data = (await res.json().catch(() => ({}))) as Record<string, unknown>;
    if (!res.ok) {
      const msg = errorSentence(data, res.status) || whatToDo(res.status);
      return { ok: false, status: res.status, error: couldNot(actionLabel, msg), data };
    }
    return { ok: true, status: res.status, data: data as T };
  } catch (e) {
    return { ok: false, status: 0, error: couldNot(actionLabel, String((e as Error)?.message ?? e)) };
  }
}

// ── Types (parity with agent_platform.py & a2a.py) ────────────────────────────────────────────

export type OverviewLiveEnv = { environment: string; version: number; deployment_id: string };
export type OverviewAgent = {
  key: string; name: string; initials: string; kind: "assistant" | "bot"; kind_detail: string;
  live: OverviewLiveEnv[]; health: { state: string; label: string };
  cost_7d_usd: number; last_run_at: string | null; runner: string;
};
export type OverviewData = {
  summary: { live_agents: number; bots_running: number; need_attention: number; spend_7d_usd: number };
  agents: OverviewAgent[];
  note?: string;
};
export type McpTool = { tool: string; agent_key: string; name: string; version: number };

export type EnvDeployment = {
  environment: "dev" | "staging" | "prod";
  deployed: boolean;
  deployment_id?: string | null;
  version?: number | null;
  state?: string | null;
  previous_version?: number | null;
  host?: { id?: string | null; label?: string | null } | null;
  runs_7d?: number;
  ok_rate?: number | null;
  cost_7d_usd?: number;
  in_flight?: number;
  promoted_at?: string | null;
  promoted_by?: string | null;
  next?: { action: "promote"; to: string } | { action: "rollback"; to_version: number } | null;
};
export type DeploymentHistoryItem = {
  id: string; environment: string; action: string; status: string;
  from_version: number | null; to_version: number | null;
  checks?: unknown; decision_id?: string | null; actor: string;
  created_at: string; applied_at?: string | null;
};
export type AgentDeploymentsData = {
  agent_key: string;
  environments: EnvDeployment[];
  draft: { version: number; state: string; note?: string | null } | null;
  versions: Array<{ version: number; active: boolean; content_hash?: string; environments: string[] }>;
  history: DeploymentHistoryItem[];
};
export type PromotionCheck = { key: string; ok: boolean | null; label: string; detail: string };
export type PromotionChecksData = {
  agent_key: string; from_environment: string; to_environment: string;
  version: number; live_version: number | null;
  checks: PromotionCheck[]; can_promote: boolean; blocked_by: string[];
  approval: { required: boolean; who: string | null };
  in_flight: { runs: number; version: number | null; note: string };
};

export type TriggerDescribe = {
  id: string; agent_key: string; environment: string; kind: string; kind_label?: string;
  name: string; status: string; on?: boolean; paused_reason?: string | null;
  concurrency?: string; config: Record<string, unknown>; input?: Record<string, unknown>;
  url_path?: string | null; next_fires?: Array<{ at: string; label: string }>;
  fires?: number; skips?: number; failure_streak?: number; last_fired_at?: string | null;
  connection_id?: string | null; priority?: number; identity_mode?: string | null;
  thread_key?: string | null; limits?: Record<string, unknown>; quiet_hours?: Record<string, unknown>;
  secret?: string; secret_shown_once?: boolean;
};
export type TriggersData = {
  agent_key: string; triggers: TriggerDescribe[];
  kinds: string[]; concurrency: string[];
  route_options?: Record<string, unknown>;
};

export type HealthDeployment = {
  deployment_id: string; agent_key: string; name: string; environment: string; version: number;
  runner: string; kind: string; state: string; label: string;
  runs: number; ok: number; failed: number; failure_streak: number;
  ok_rate: number | null; p95_ms: number | null; cost_usd: number;
  last_run_at: string | null; interrupt_backlog: number; stuck_runs: number;
  paused: boolean; paused_reason?: string | null;
  triggers: Array<{ id: string; kind: string; status: string }>;
  last_failure?: { run_id: string; at: string; cause: string; same_cause_count: number; error: string } | null;
  actions?: string[];
};
export type HealthAlert = { deployment_id: string; agent_key: string; title: string; detail: string; since: string; trace_run_id?: string | null };
export type SystemServiceHealth = {
  key: string; name: string; kind: string; runs_in: string; state: string; ok: boolean;
  interval_seconds: number; last_beat_at: string | null; last_ok_at: string | null;
  last_error?: string | null; host?: string | null;
};
export type WorkersSummary = {
  live_workers: number; slots: number; busy_slots: number; free_slots: number;
  queued_runs: number; waiting_runs: number; desired_slots: number; signal: string; words: string;
};
export type PlatformHealthData = {
  hours: number; deployments: HealthDeployment[]; alerts: HealthAlert[];
  system_services: SystemServiceHealth[]; workers: WorkersSummary;
};
export type WorkerScalingData = WorkersSummary & {
  project_id: string;
  workers: Array<{
    id: string; worker_id: string; status: string; liveness: string;
    slots: number; busy_slots: number; host?: string | null; last_seen_at?: string | null;
  }>;
};

export type EndpointsData = {
  agent_key: string; environment: string; version: number | null;
  rest: { enabled: boolean; create_thread?: string; start_run?: string; stream?: string; wait?: string; permission?: string };
  mcp: { enabled: boolean; tool: string; note?: string };
  a2a: { enabled: boolean; card_path: string; endpoint?: string | null; may_enable?: boolean | null; note?: string };
  callers: Array<{ principal_id: string; name: string; kind: string; via: string; may: string; last_used_at: string | null }>;
  try_it: string;
};
export type RunOut = {
  run_id: string; thread_id: string; status: string; boss_status?: string;
  agent_key: string; version?: number; environment?: string;
  output?: unknown; error?: string | null; timed_out?: boolean;
  interrupts?: Array<{ decision_id: string; interrupt_id: string; status: string; question: string }> | null;
  created_at?: string; updated_at?: string;
};
export type ThreadHistoryData = {
  thread_id: string;
  runs: RunOut[];
  checkpoints: Array<{ checkpoint_id: string; run_id: string; parent_checkpoint_id?: string | null; step: number; source: string; status: string; created_at: string }>;
};
export type StreamFrame = { event: string; data: unknown };

// ── Reads ─────────────────────────────────────────────────────────────────────────────────────

export function readOverview(projectId: string, opts: { environment?: string; kind?: string } = {}): Promise<ReadResult<OverviewData>> {
  const q = new URLSearchParams({ project_id: projectId });
  if (opts.environment) q.set("environment", opts.environment);
  if (opts.kind) q.set("kind", opts.kind);
  return readJson<OverviewData>(`/v1/agent-platform/agents?${q}`, "load agent deployments");
}

export function readMcpTools(projectId: string): Promise<ReadResult<{ tools: McpTool[] }>> {
  return readJson<{ tools: McpTool[] }>(`/v1/agent-platform/mcp-tools?project_id=${enc(projectId)}`, "load MCP tools");
}

export function readAgentDeployments(projectId: string, roleKey: string): Promise<ReadResult<AgentDeploymentsData>> {
  return readJson<AgentDeploymentsData>(`/v1/agents/${enc(roleKey)}/deployments?project_id=${enc(projectId)}`, "load deployments");
}

export function readPromotionChecks(
  projectId: string,
  roleKey: string,
  opts: { from_environment: string; to_environment: string; version?: number },
): Promise<ReadResult<PromotionChecksData>> {
  const q = new URLSearchParams({
    project_id: projectId,
    from_environment: opts.from_environment,
    to_environment: opts.to_environment,
  });
  if (opts.version !== undefined) q.set("version", String(opts.version));
  return readJson<PromotionChecksData>(`/v1/agents/${enc(roleKey)}/promotion-checks?${q}`, "run promotion checks");
}

export function readTriggers(projectId: string, roleKey: string, opts: { kind?: string; connection_id?: string } = {}): Promise<ReadResult<TriggersData>> {
  const q = new URLSearchParams({ project_id: projectId });
  if (opts.kind) q.set("kind", opts.kind);
  if (opts.connection_id) q.set("connection_id", opts.connection_id);
  return readJson<TriggersData>(`/v1/agents/${enc(roleKey)}/triggers?${q}`, "load triggers");
}

export function previewTriggerSchedule(
  projectId: string,
  params: { kind: string; cron?: string; every_seconds?: number; timezone?: string; count?: number },
): Promise<ReadResult<{ kind: string; config: Record<string, unknown>; next: Array<{ at: string; label: string }> }>> {
  const q = new URLSearchParams({ project_id: projectId, kind: params.kind, timezone: params.timezone || "UTC", count: String(params.count ?? 3) });
  if (params.cron) q.set("cron", params.cron);
  if (params.every_seconds) q.set("every_seconds", String(params.every_seconds));
  return readJson(`/v1/agent-triggers/preview?${q}`, "preview trigger schedule");
}

export function readHealth(projectId: string, hours = 24): Promise<ReadResult<PlatformHealthData>> {
  return readJson<PlatformHealthData>(`/v1/agent-platform/health?project_id=${enc(projectId)}&hours=${hours}`, "load agent health");
}

export function readWorkerScaling(projectId: string): Promise<ReadResult<WorkerScalingData>> {
  return readJson<WorkerScalingData>(`/v1/workers/scaling?project_id=${enc(projectId)}`, "load worker scaling");
}

export function readEndpoints(projectId: string, roleKey: string, environment = "prod"): Promise<ReadResult<EndpointsData>> {
  return readJson<EndpointsData>(`/v1/agents/${enc(roleKey)}/endpoints?project_id=${enc(projectId)}&environment=${enc(environment)}`, "load endpoints");
}

export function readThread(projectId: string, threadId: string): Promise<ReadResult<Record<string, unknown>>> {
  return readJson(`/v1/threads/${enc(threadId)}?project_id=${enc(projectId)}`, "load thread");
}

export function readThreadHistory(projectId: string, threadId: string): Promise<ReadResult<ThreadHistoryData>> {
  return readJson<ThreadHistoryData>(`/v1/threads/${enc(threadId)}/history?project_id=${enc(projectId)}`, "load thread history");
}

export function readRun(projectId: string, threadId: string, runId: string): Promise<ReadResult<RunOut>> {
  return readJson<RunOut>(`/v1/threads/${enc(threadId)}/runs/${enc(runId)}?project_id=${enc(projectId)}`, "load run");
}

// ── Writes (18 registered helpers, all via apiWrite carrying signed-in actor) ─────────────────

export function deployVersion(
  projectId: string, actor: string, roleKey: string,
  body: { environment: string; version: number; host_id?: string; host_label?: string },
) {
  return writeJson(`/v1/agents/${enc(roleKey)}/deployments`, "POST",
    { project_id: projectId, actor, ...body }, press(`deploy:${roleKey}:${body.environment}`, "Deploy"), "deploy agent version");
}

export function promoteVersion(
  projectId: string, actor: string, roleKey: string,
  body: { from_environment: string; to_environment: string; version?: number },
): Promise<WriteResult<{ status: string; deployment?: EnvDeployment; decision_id?: string; checks?: PromotionCheck[]; already_asked?: boolean }>> {
  return writeJson(`/v1/agents/${enc(roleKey)}/promote`, "POST",
    { project_id: projectId, actor, ...body }, press(`promote:${roleKey}:${body.to_environment}`, "Promote"), "promote agent version");
}

export function rollbackDeployment(
  projectId: string, actor: string, roleKey: string,
  body: { environment: string; to_version?: number },
) {
  return writeJson(`/v1/agents/${enc(roleKey)}/rollback`, "POST",
    { project_id: projectId, actor, ...body }, press(`rollback:${roleKey}:${body.environment}`, "Rollback"), "roll back deployment");
}

export function createTrigger(projectId: string, actor: string, roleKey: string, body: Record<string, unknown>): Promise<WriteResult<TriggerDescribe>> {
  return writeJson<TriggerDescribe>(`/v1/agents/${enc(roleKey)}/triggers`, "POST",
    { project_id: projectId, actor, ...body }, press(`trigger:new:${roleKey}`, "Create trigger"), "create trigger");
}

export function previewTriggerMatch(projectId: string, actor: string, roleKey: string, body: Record<string, unknown>) {
  return writeJson(`/v1/agents/${enc(roleKey)}/triggers/preview-match`, "POST",
    { project_id: projectId, actor, ...body }, press(`trigger:preview:${roleKey}`, "Preview match"), "preview trigger match");
}

export function updateTrigger(projectId: string, actor: string, triggerId: string, patch: Record<string, unknown>): Promise<WriteResult<TriggerDescribe>> {
  return writeJson<TriggerDescribe>(`/v1/agent-triggers/${enc(triggerId)}`, "PATCH",
    { project_id: projectId, actor, ...patch }, press(`trigger:edit:${triggerId}`, "Save trigger"), "update trigger");
}

export function deleteTrigger(projectId: string, actor: string, triggerId: string) {
  return writeJson(`/v1/agent-triggers/${enc(triggerId)}?project_id=${enc(projectId)}&actor=${enc(actor)}`, "DELETE",
    undefined, press(`trigger:del:${triggerId}`, "Delete trigger"), "delete trigger");
}

export function setTriggerPaused(projectId: string, actor: string, triggerId: string, paused: boolean, reason = ""): Promise<WriteResult<TriggerDescribe>> {
  return writeJson<TriggerDescribe>(`/v1/agent-triggers/${enc(triggerId)}/${paused ? "pause" : "resume"}`, "POST",
    { project_id: projectId, actor, reason }, press(`trigger:pause:${triggerId}`, paused ? "Pause" : "Resume"), paused ? "pause trigger" : "resume trigger");
}

export function rotateTriggerSecret(projectId: string, actor: string, triggerId: string): Promise<WriteResult<TriggerDescribe>> {
  return writeJson<TriggerDescribe>(`/v1/agent-triggers/${enc(triggerId)}/rotate-secret`, "POST",
    { project_id: projectId, actor }, press(`trigger:rotate:${triggerId}`, "Rotate secret"), "rotate webhook secret");
}

export function setDeploymentPaused(projectId: string, actor: string, roleKey: string, environment: string, paused: boolean, reason = "") {
  return writeJson(`/v1/agents/${enc(roleKey)}/deployments/${enc(environment)}/${paused ? "pause" : "resume"}`, "POST",
    { project_id: projectId, actor, reason }, press(`dep:pause:${roleKey}:${environment}`, paused ? "Pause" : "Resume"), paused ? "pause deployment" : "resume deployment");
}

export function tickBots(projectId?: string, limit = 50) {
  return writeJson("/v1/bots/tick", "POST",
    { ...(projectId ? { project_id: projectId } : {}), limit }, press("bots:tick", "Run bot clock"), "run bot clock");
}

export function createProtocolThread(
  projectId: string, actor: string, roleKey: string,
  body: { environment?: string; double_text?: string; metadata?: Record<string, unknown> } = {},
): Promise<WriteResult<{ thread_id: string; agent_key: string; status: string }>> {
  return writeJson(`/v1/agents/${enc(roleKey)}/threads`, "POST",
    { project_id: projectId, actor, environment: body.environment || "prod", double_text: body.double_text || "enqueue", metadata: body.metadata || {} },
    press(`thread:new:${roleKey}`, "New thread"), "create thread");
}

export function startProtocolRun(
  projectId: string, actor: string, threadId: string,
  body: { input: Record<string, unknown>; double_text?: string; timeout?: number },
): Promise<WriteResult<RunOut>> {
  return writeJson<RunOut>(`/v1/threads/${enc(threadId)}/runs`, "POST",
    { project_id: projectId, actor, ...body }, press(`run:start:${threadId}`, "Start run"), "start run");
}

export async function streamProtocolRun(
  projectId: string,
  actor: string,
  path: string,
  body: Record<string, unknown>,
  onEvent: (frame: StreamFrame) => void,
  signal?: AbortSignal,
): Promise<WriteResult<{ frames: number }>> {
  try {
    const res = await apiWrite(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ project_id: projectId, actor, ...body }),
      signal,
    }, { press: press(`stream:${path}`, "Stream run") });
    if (!res.ok) {
      const data = (await res.json().catch(() => ({}))) as Record<string, unknown>;
      return { ok: false, status: res.status, error: couldNot("stream run", errorSentence(data, res.status) || whatToDo(res.status)), data };
    }
    if (!res.body) return { ok: true, status: res.status, data: { frames: 0 } };
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let frames = 0;
    const emitBlock = (rawBlock: string) => {
      const lines = rawBlock.split(/\r?\n/);
      let evName = "message";
      const dataLines: string[] = [];
      for (const line of lines) {
        if (line.startsWith("event:")) evName = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
      }
      if (!dataLines.length) return;
      const joined = dataLines.join("\n");
      let parsed: unknown = joined;
      try { parsed = JSON.parse(joined); } catch { /* plain text */ }
      frames += 1;
      onEvent({ event: evName, data: parsed });
    };
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split(/\r?\n\r?\n/);
      buffer = parts.pop() ?? "";
      for (const block of parts) if (block.trim()) emitBlock(block);
    }
    if (buffer.trim()) emitBlock(buffer);
    return { ok: true, status: res.status, data: { frames } };
  } catch (e) {
    return { ok: false, status: 0, error: couldNot("stream run", String((e as Error)?.message ?? e)) };
  }
}

export function waitProtocolRun(projectId: string, actor: string, path: string, body: Record<string, unknown>): Promise<WriteResult<RunOut>> {
  return writeJson<RunOut>(path, "POST", { project_id: projectId, actor, ...body }, press(`wait:${path}`, "Wait run"), "run agent");
}

export function cancelProtocolRun(projectId: string, actor: string, threadId: string, runId: string): Promise<WriteResult<RunOut>> {
  return writeJson<RunOut>(`/v1/threads/${enc(threadId)}/runs/${enc(runId)}/cancel`, "POST",
    { project_id: projectId, actor }, press(`run:cancel:${runId}`, "Cancel run"), "cancel run");
}

export function forkProtocolRun(
  projectId: string, actor: string, threadId: string, runId: string,
  body: { checkpoint_id?: string; tool_results?: Record<string, unknown> } = {},
): Promise<WriteResult<RunOut>> {
  return writeJson<RunOut>(`/v1/threads/${enc(threadId)}/runs/${enc(runId)}/fork`, "POST",
    { project_id: projectId, actor, ...body }, press(`run:fork:${runId}`, "Fork run"), "fork run");
}

export function setA2AExposure(projectId: string, actor: string, roleKey: string, environment: string, enabled: boolean) {
  return writeJson(`/v1/agents/${enc(roleKey)}/deployments/${enc(environment)}/a2a`, "PUT",
    { project_id: projectId, actor, enabled }, press(`a2a:${roleKey}:${environment}`, enabled ? "Enable A2A" : "Disable A2A"), "update A2A exposure");
}
