/**
 * The work record off the wire: snake_case columns (services/agent_world_types.py `Act`) into
 * the frozen web `Act` (lib/agent-world-types.ts). Server-safe (no React), so the Credits image
 * route maps a chain exactly as the console does.
 *
 * THE EDGE LABEL IS THE SERVER'S. An act without `edge_label` maps to "" and shows no How; the
 * browser never composes one.
 */
import type { Act, ActHow, ActKind, ActStatus, Chain } from "../../lib/agent-world-types";

export type Wire = Record<string, unknown>;

const KINDS = new Set<ActKind>(["run", "step", "stage", "seat", "chat", "approval", "handoff"]);
const STATUSES = new Set<ActStatus>(["pending", "active", "awaiting_input", "complete", "error", "stale", "skipped"]);
/** The links an act carries to the records it was about, by the column names the API sends. */
const LINK_KEYS = ["workflow_run_id", "step_id", "code_task_run_id", "meeting_run_id", "thread_id",
  "decision_id", "subject_kind", "subject_id", "stage", "receipt_id"] as const;

function str(v: unknown): string | undefined {
  return typeof v === "string" && v ? v : undefined;
}
function num(v: unknown): number {
  const n = typeof v === "number" ? v : typeof v === "string" ? Number(v) : 0;
  return Number.isFinite(n) ? n : 0;
}
function strings(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  return v.map((x) => (typeof x === "string" ? x
    : x && typeof x === "object" ? String((x as Wire).role_key ?? (x as Wire).name ?? (x as Wire).id ?? "") : ""))
    .filter(Boolean);
}
/** Reads a field by its wire name, or by the web name when a client already mapped it. */
function pick(w: Wire, snake: string, camel?: string): unknown {
  return w[snake] !== undefined ? w[snake] : camel ? w[camel] : undefined;
}

/** One act off the wire. Unknown kinds and statuses fall back to the quietest reading. */
export function toAct(w: Wire): Act {
  const kind = String(pick(w, "kind") ?? "step") as ActKind;
  const status = String(pick(w, "status") ?? "active") as ActStatus;
  const handedKind = String(pick(w, "handed_by_kind") ?? (pick(w, "handedBy") as Wire | undefined)?.kind ?? "boss");
  const handedId = str(pick(w, "handed_by_id")) ?? str((pick(w, "handedBy") as Wire | undefined)?.id);
  const scope = (pick(w, "scope") ?? {}) as Wire;
  const links: Record<string, string> = {};
  const given = (pick(w, "links") ?? {}) as Wire;
  for (const k of LINK_KEYS) {
    const v = str(w[k]) ?? str(given[k]);
    if (v) links[k] = v;
  }
  const scopeOk = pick(w, "scope_ok", "scopeOk");
  const version = pick(w, "identity_version", "version");
  return {
    actId: String(pick(w, "act_id", "actId") ?? ""),
    roleKey: str(pick(w, "role_key", "roleKey")) ?? null,
    kind: KINDS.has(kind) ? kind : "step",
    status: STATUSES.has(status) ? status : "active",
    ...(str(pick(w, "parent_act_id", "parentActId")) ? { parentActId: str(pick(w, "parent_act_id", "parentActId")) } : {}),
    rootActId: String(pick(w, "root_act_id", "rootActId") ?? ""),
    handedBy: {
      kind: (handedKind === "person" || handedKind === "agent" ? handedKind : "boss"),
      ...(handedId ? { id: handedId } : {}),
    },
    how: String(pick(w, "how") ?? "named") as ActHow,
    edgeLabel: str(pick(w, "edge_label", "edgeLabel")) ?? "",
    ...(str(pick(w, "why")) ? { why: str(pick(w, "why")) } : {}),
    considered: strings(pick(w, "considered")),
    scope: { skills: strings(scope.skills), mcp: strings(scope.mcp) },
    ...(typeof scopeOk === "boolean" ? { scopeOk } : {}),
    accountable: String(pick(w, "accountable") ?? ""),
    label: String(pick(w, "label") ?? ""),
    links,
    artifacts: Array.isArray(pick(w, "artifacts"))
      ? (pick(w, "artifacts") as Wire[]).filter((a) => a && typeof a === "object").map((a) => ({
          kind: String(a.kind ?? ""), ref: String(a.ref ?? ""), ...(str(a.url) ? { url: str(a.url) } : {}),
        }))
      : [],
    tokensIn: num(pick(w, "tokens_in", "tokensIn")),
    tokensOut: num(pick(w, "tokens_out", "tokensOut")),
    costUsd: num(pick(w, "cost_usd", "costUsd")),
    reconstructed: pick(w, "reconstructed") === true,
    startedAt: String(pick(w, "started_at", "startedAt") ?? ""),
    ...(str(pick(w, "ended_at", "endedAt")) ? { endedAt: str(pick(w, "ended_at", "endedAt")) } : {}),
    ...(typeof version === "number" ? { version } : {}),
  };
}

/** `GET /v1/workflow-runs/{id}/chain`, mapped. Acts keep the API's order. */
export function toChain(w: Wire): Chain {
  const totals = (w.totals ?? {}) as Wire;
  return {
    acts: Array.isArray(w.acts) ? (w.acts as Wire[]).map(toAct).filter((a) => a.actId) : [],
    accountable: String(w.accountable ?? ""),
    totals: { steps: num(totals.steps), minutes: num(totals.minutes), cost: num(totals.cost) },
    credits: Array.isArray(w.credits)
      ? (w.credits as Wire[]).map((c) => ({ roleKey: str(c.role_key ?? c.roleKey) ?? null, line: String(c.line ?? "") }))
      : [],
  };
}

/** One piece of work with what it said and what it led to (`GET /v1/acts/{id}`). */
export type ActDetail = Act & {
  /** The agent's last completion turns for this work, oldest first, already trimmed. */
  turns: { text: string; at?: string }[];
  receiptUrl?: string;
  children: number;
};

export function toActDetail(w: Wire): ActDetail {
  const base = toAct((w.act && typeof w.act === "object" ? w.act : w) as Wire);
  const turns = Array.isArray(w.turns_excerpt) ? (w.turns_excerpt as unknown[]) : [];
  const children = w.children;
  return {
    ...base,
    turns: turns.map((t) => typeof t === "string" ? { text: t }
      : { text: String((t as Wire)?.content ?? (t as Wire)?.text ?? ""),
          ...(str((t as Wire)?.at ?? (t as Wire)?.created_at) ? { at: str((t as Wire)?.at ?? (t as Wire)?.created_at) } : {}) })
      .filter((t) => t.text.trim()).slice(-3),
    ...(str(w.receipt_url ?? (w.receipt as Wire | undefined)?.url) ? { receiptUrl: str(w.receipt_url ?? (w.receipt as Wire | undefined)?.url) } : {}),
    children: Array.isArray(children) ? children.length
      : children && typeof children === "object" ? num((children as Wire).count) : num(children),
  };
}

