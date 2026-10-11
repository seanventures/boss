/**
 * The work record off the wire: snake_case columns (services/agent_world_types.py `Act`) into
 * the frozen web `Act` (lib/agent-world-types.ts). Server-safe (no React), so the Credits image
 * route maps a chain exactly as the console does.
 *
 * THE EDGE LABEL IS THE SERVER'S. An act without `edge_label` maps to "" and shows no How; the
 * browser never composes one.
 */
import type { Act, ActActor, ActHow, ActKind, ActStatus, Chain, StepSpend } from "../../lib/agent-world-types";

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
    ...(num(pick(w, "included_usd", "includedUsd")) ? { includedUsd: num(pick(w, "included_usd", "includedUsd")) } : {}),
    ...(toActor(pick(w, "actor")) ? { actor: toActor(pick(w, "actor")) } : {}),
  };
}

/** The server's resolved actor (agent_acts.resolve_actors), or undefined. */
function toActor(v: unknown): ActActor | undefined {
  if (!v || typeof v !== "object") return undefined;
  const a = v as Wire;
  const kind = String(a.kind ?? "");
  if (kind !== "agent" && kind !== "person" && kind !== "boss") return undefined;
  const roleKey = str(a.role_key ?? a.roleKey) ?? null;
  if (kind === "agent" && !roleKey) return undefined;
  const personId = str(a.person_id ?? a.personId);
  const name = str(a.name);
  const panel = Array.isArray(a.panel) ? (a.panel as unknown[]).filter((x): x is string => typeof x === "string" && !!x) : [];
  return {
    kind, roleKey,
    ...(personId ? { personId } : {}),
    ...(kind === "person" && name ? { name } : {}),
    ...(panel.length ? { panel } : {}),
  };
}

function toSteps(v: unknown): Record<string, StepSpend> {
  const out: Record<string, StepSpend> = {};
  if (!v || typeof v !== "object") return out;
  for (const [id, raw] of Object.entries(v as Wire)) {
    if (!raw || typeof raw !== "object") continue;
    const e = raw as Wire;
    out[id] = {
      costUsd: num(e.cost_usd ?? e.costUsd), includedUsd: num(e.included_usd ?? e.includedUsd),
      attempts: num(e.attempts),
      actIds: Array.isArray(e.act_ids ?? e.actIds) ? ((e.act_ids ?? e.actIds) as unknown[]).map(String) : [],
    };
  }
  return out;
}

/** Principal id → display name, from every person the chain's actors name. */
function toPeople(acts: Act[], given: unknown): Record<string, string> {
  const out: Record<string, string> = {};
  if (given && typeof given === "object") {
    for (const [id, name] of Object.entries(given as Wire)) if (typeof name === "string" && name) out[id] = name;
  }
  for (const a of acts) if (a.actor?.personId && a.actor.name) out[a.actor.personId] ??= a.actor.name;
  return out;
}

/** `GET /v1/workflow-runs/{id}/chain`, mapped. Acts keep the API's order. */
export function toChain(w: Wire): Chain {
  const totals = (w.totals ?? {}) as Wire;
  const acts = Array.isArray(w.acts) ? (w.acts as Wire[]).map(toAct).filter((a) => a.actId) : [];
  return {
    acts,
    accountable: String(w.accountable ?? ""),
    totals: { steps: num(totals.steps), minutes: num(totals.minutes), cost: num(totals.cost),
              ...(num(totals.included) ? { included: num(totals.included) } : {}) },
    steps: toSteps(w.steps),
    people: toPeople(acts, w.people),
    credits: Array.isArray(w.credits)
      ? (w.credits as Wire[]).map((c) => {
        const personId = str(c.person_id ?? c.personId);
        return { roleKey: str(c.role_key ?? c.roleKey) ?? null, line: String(c.line ?? ""), ...(personId ? { personId } : {}) };
      })
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

