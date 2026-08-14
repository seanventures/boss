/**
 * Metatron Captain UI contracts.
 *
 * The captain is Community-ok (self-hosted). Every action is validated; this
 * mapper never invents a successful session, goal, watch, or overwatch when
 * the API is missing. Loopback hosts are never operator URLs.
 */

export const CAPTAIN_UNAVAILABLE_404 =
  "Metatron captain is unavailable (HTTP 404). No session, goals, or watch were invented.";

export const GOAL_KIND_OPTIONS = [
  "custom",
  "working_software",
  "budget",
  "queue",
  "quality",
] as const;

export type MetatronCaptainMessage = {
  id?: string;
  role: string;
  content: string;
  actions?: Array<{ kind?: string }>;
  createdAt?: string;
};

export type MetatronCaptainMemory = {
  id?: string;
  kind?: string;
  content: string;
  source?: string;
};

export type MetatronCaptainGoal = {
  id: string;
  title: string;
  kind: string;
  status: string;
  description?: string;
  lastScore?: number | null;
  lastMeasuredAt?: string;
};

export type MetatronWatchFinding = {
  id: string;
  severity: string;
  title: string;
  action?: string;
  autoSafe?: boolean;
};

export type MetatronWatchTick = {
  id?: string;
  createdAt?: string;
  cycle?: string;
  findings: MetatronWatchFinding[];
  proposedMoves: Array<{ kind?: string; status?: string; detail?: string }>;
  receiptId?: string;
  verifyOk?: boolean | null;
};

export type MetatronDriveResult = {
  status: string;
  auto: boolean;
  applied: unknown[];
  moves: Array<{
    kind?: string;
    status?: string;
    detail?: string;
    requiresHuman?: boolean;
  }>;
  note?: string;
  watchId?: string;
};

export type MetatronCaptainState = {
  apiReachable: boolean;
  blockedReason?: string;
  threadId?: string;
  messages: MetatronCaptainMessage[];
  memories: MetatronCaptainMemory[];
  goals: MetatronCaptainGoal[];
  watch: MetatronWatchTick | null;
  lastDrive: MetatronDriveResult | null;
  lastOverwatch: MetatronDriveResult | null;
};

export function emptyMetatronCaptain(): MetatronCaptainState {
  return {
    apiReachable: false,
    messages: [],
    memories: [],
    goals: [],
    watch: null,
    lastDrive: null,
    lastOverwatch: null,
  };
}

export function unavailableMetatronCaptain(reason: string): MetatronCaptainState {
  return {
    ...emptyMetatronCaptain(),
    apiReachable: false,
    blockedReason: reason,
  };
}

export function describeMetatronCaptainUnavailable(
  status?: number,
  detail?: string,
): string {
  if (status === 404) {
    return CAPTAIN_UNAVAILABLE_404;
  }
  if (status === 501) {
    return "Metatron captain is not mounted on this control plane (HTTP 501).";
  }
  if (status === 503) {
    return "Control plane returned 503. Captain session, goals, and watch are unavailable.";
  }
  if (status === 403) {
    return "Captain request denied (HTTP 403). The actor needs agent_chat on this project.";
  }
  if (status && status >= 400) {
    const extra = detail?.trim() ? `: ${detail.trim()}` : "";
    return `Captain request failed (HTTP ${status})${extra}. No mock session was invented.`;
  }
  const extra = detail?.trim() ? ` (${detail.trim()})` : "";
  return `Captain control-plane request failed${extra}. Session, goals, and watch are unavailable.`;
}

export function isCaptainUnavailableStatus(status?: number): boolean {
  return status === 404 || status === 501 || status === 503;
}

export function isLoopbackOperatorUrl(url: string | null | undefined): boolean {
  if (!url) return false;
  const lowered = url.trim().toLowerCase();
  return (
    lowered.includes("://127.") ||
    lowered.includes("://localhost") ||
    lowered.includes("://[::1]") ||
    lowered.includes("://0.0.0.0") ||
    lowered.startsWith("127.") ||
    lowered.startsWith("localhost") ||
    lowered.startsWith("[::1]") ||
    lowered.startsWith("0.0.0.0")
  );
}

/** Operator href. Loopback is never a captain View / operator URL. */
export function captainOperatorHref(url: string | null | undefined): string | null {
  const candidate = (url || "").trim();
  if (!candidate || isLoopbackOperatorUrl(candidate)) return null;
  return candidate;
}

export function mapMetatronCaptainSession(
  payload: Record<string, unknown>,
): MetatronCaptainState {
  return {
    apiReachable: true,
    threadId: asString(payload.thread_id),
    messages: asMessages(payload.messages),
    memories: asMemories(payload.memories),
    goals: asGoals(payload.goals),
    watch: asWatch(payload.watch),
    lastDrive: null,
    lastOverwatch: payload.last_overwatch
      ? mapMetatronDriveResult(payload.last_overwatch as Record<string, unknown>)
      : null,
  };
}

export function mapMetatronDriveResult(
  payload: Record<string, unknown>,
): MetatronDriveResult {
  return {
    status: asString(payload.status) || "proposed",
    auto: payload.auto === true,
    applied: Array.isArray(payload.applied) ? payload.applied : [],
    moves: asMoves(payload.moves),
    note: asString(payload.note),
    watchId: asString(payload.watch_id),
  };
}

export function mapMetatronWatchTick(
  payload: Record<string, unknown> | null | undefined,
): MetatronWatchTick | null {
  return asWatch(payload);
}

function asString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function asMessages(value: unknown): MetatronCaptainMessage[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const row = item as Record<string, unknown>;
    const content = typeof row.content === "string" ? row.content : "";
    const role = typeof row.role === "string" ? row.role : "assistant";
    const actions = Array.isArray(row.actions)
      ? row.actions.flatMap((action) => {
          if (!action || typeof action !== "object") return [];
          return [{ kind: asString((action as { kind?: unknown }).kind) }];
        })
      : undefined;
    return [
      {
        id: asString(row.id),
        role,
        content,
        actions,
        createdAt: asString(row.created_at),
      },
    ];
  });
}

function asMemories(value: unknown): MetatronCaptainMemory[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const row = item as Record<string, unknown>;
    const content = typeof row.content === "string" ? row.content : "";
    if (!content) return [];
    return [
      {
        id: asString(row.id),
        kind: asString(row.kind),
        content,
        source: asString(row.source),
      },
    ];
  });
}

function asGoals(value: unknown): MetatronCaptainGoal[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item, index) => {
    if (!item || typeof item !== "object") return [];
    const row = item as Record<string, unknown>;
    const title = typeof row.title === "string" ? row.title : "";
    if (!title) return [];
    const score = row.last_score;
    return [
      {
        id: asString(row.id) || `goal-${index}`,
        title,
        kind: asString(row.kind) || "custom",
        status: asString(row.status) || "active",
        description: asString(row.description),
        lastScore: typeof score === "number" ? score : score == null ? null : Number(score),
        lastMeasuredAt: asString(row.last_measured_at),
      },
    ];
  });
}

function asWatch(value: unknown): MetatronWatchTick | null {
  if (!value || typeof value !== "object") return null;
  const row = value as Record<string, unknown>;
  const findings = Array.isArray(row.findings)
    ? row.findings.flatMap((item, index) => {
        if (!item || typeof item !== "object") return [];
        const finding = item as Record<string, unknown>;
        const title = typeof finding.title === "string" ? finding.title : "";
        if (!title) return [];
        return [
          {
            id: asString(finding.id) || `finding-${index}`,
            severity: asString(finding.severity) || "info",
            title,
            action: asString(finding.action),
            autoSafe: finding.auto_safe === true,
          },
        ];
      })
    : [];
  return {
    id: asString(row.id),
    createdAt: asString(row.created_at),
    cycle: asString(row.cycle),
    findings,
    proposedMoves: asMoves(row.proposed_moves),
    receiptId: asString(row.receipt_id),
    verifyOk:
      row.verify && typeof row.verify === "object"
        ? (row.verify as { ok?: unknown }).ok === true
        : null,
  };
}

function asMoves(
  value: unknown,
): Array<{ kind?: string; status?: string; detail?: string; requiresHuman?: boolean }> {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const row = item as Record<string, unknown>;
    return [
      {
        kind: asString(row.kind),
        status: asString(row.status),
        detail: asString(row.detail),
        requiresHuman: row.requires_human === true || row.status === "needs_human",
      },
    ];
  });
}
