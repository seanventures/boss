/**
 * The tool-call audit (BOSSAPP-819): every tool call BOSS decided, as GET /v1/tool-calls returns
 * it. Read through the console's own route handlers (/api/tool-calls), so the console's
 * credentials never reach the page. Arguments are the redacted preview, never the raw call.
 */

/** Every filter the API understands; a key missing here is dropped by the proxy. */
export const TOOL_CALL_FORWARD = [
  "project_id", "run_id", "server_id", "tool", "decision", "outcome", "screen", "since", "until",
  "cursor", "limit",
] as const;

export type ToolCallFilterKey = "run_id" | "server_id" | "tool" | "decision" | "outcome" | "screen";

export type ToolCallScreen = { mode?: string; verdict?: string; rules?: string[] } | null;

export type ToolCall = {
  id: string;
  projectId: string;
  runId: string | null;
  agent: string | null;
  principal: string | null;
  serverId: string | null;
  tool: string;
  argsSha256: string | null;
  argsRedacted: Record<string, unknown>;
  decision: string;
  matchedPolicyIds: string[];
  decisionId: string | null;
  outcome: string;
  latencyMs: number;
  recordedAt: string;
  screen: ToolCallScreen;
  explanation: string | null;
};

export type ToolCallPage = { items: ToolCall[]; next: string | null };

function str(v: unknown): string | null {
  return typeof v === "string" && v ? v : null;
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return v != null && typeof v === "object" && !Array.isArray(v);
}

export function mapToolCall(raw: unknown): ToolCall | null {
  if (!isRecord(raw) || typeof raw.id !== "string" || typeof raw.tool !== "string") return null;
  const screen = isRecord(raw.screen) ? {
    mode: str(raw.screen.mode) ?? undefined,
    verdict: str(raw.screen.verdict) ?? undefined,
    rules: Array.isArray(raw.screen.rules) ? raw.screen.rules.map(String) : undefined,
  } : null;
  return {
    id: raw.id,
    projectId: String(raw.project_id ?? ""),
    runId: str(raw.run_id),
    agent: str(raw.agent),
    principal: str(raw.principal),
    serverId: str(raw.server_id),
    tool: raw.tool,
    argsSha256: str(raw.args_sha256),
    argsRedacted: isRecord(raw.args_redacted) ? raw.args_redacted : {},
    decision: String(raw.decision ?? ""),
    matchedPolicyIds: Array.isArray(raw.matched_policy_ids) ? raw.matched_policy_ids.map(String) : [],
    decisionId: str(raw.decision_id),
    outcome: String(raw.outcome ?? ""),
    latencyMs: Number(raw.latency_ms ?? 0) || 0,
    recordedAt: String(raw.recorded_at ?? ""),
    screen,
    explanation: str(raw.explanation),
  };
}

export function mapToolCallPage(body: unknown): ToolCallPage {
  const items = isRecord(body) && Array.isArray(body.items) ? body.items : [];
  return {
    items: items.map(mapToolCall).filter((x): x is ToolCall => x !== null),
    next: isRecord(body) ? str(body.next) : null,
  };
}

export type ToolCallFilters = Partial<Record<ToolCallFilterKey, string>>;

function query(projectId: string, filters: ToolCallFilters, extra: Record<string, string> = {}): string {
  const q = new URLSearchParams({ project_id: projectId });
  for (const [k, v] of Object.entries({ ...filters, ...extra })) if (v) q.set(k, v);
  return q.toString();
}

export async function fetchToolCalls(
  projectId: string, filters: ToolCallFilters, cursor: string | null, signal?: AbortSignal,
): Promise<{ ok: true; data: ToolCallPage } | { ok: false; error: string }> {
  try {
    const r = await fetch(`/api/tool-calls?${query(projectId, filters, cursor ? { cursor } : {})}`,
                          { cache: "no-store", signal });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      const said = isRecord(body) ? body.error ?? body.detail : null;
      return { ok: false, error: typeof said === "string" ? said : `BOSS answered ${r.status}` };
    }
    return { ok: true, data: mapToolCallPage(body) };
  } catch (e) {
    return { ok: false, error: String((e as Error)?.message ?? e) };
  }
}

export function toolCallExportHref(projectId: string, filters: ToolCallFilters): string {
  return `/api/tool-calls/export?${query(projectId, filters)}`;
}

/** The words a decision / outcome / screen verdict reads as. */
export const DECISION_WORD: Record<string, string> = {
  allow: "Allowed", ask: "Asked", deny: "Denied", off: "Off", refused: "Refused",
  deferred: "Held", unreviewed: "Unreviewed", arguments: "Changed args",
};

export const OUTCOME_TONE: Record<string, "ok" | "warn" | "bad"> = {
  ok: "ok", success: "ok", succeeded: "ok", completed: "ok",
  refused: "bad", failed: "bad", error: "bad",
  deferred: "warn", deferred_for_approval: "warn", skipped: "warn",
};

export function toneOf(call: ToolCall): "ok" | "warn" | "bad" {
  if (call.screen?.verdict === "suspicious") return "warn";
  return OUTCOME_TONE[call.outcome] ?? "ok";
}
