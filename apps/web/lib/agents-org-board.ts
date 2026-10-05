/**
 * Agents › Organization: the frozen contract (org-view spec §6.1), beside
 * engine/boss_engine/services/agents_org_board.py.
 *
 * The wire is snake_case; `parseOrgBoard` is the only place it is mapped, and it never invents
 * a field the server did not send: a number that is missing stays absent (a row with nothing
 * true to say hides), never zero-filled into a fact. Nothing here is a percent, an ETA, a
 * capacity or a score.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { isConsoleLive, subscribeConsoleLive } from "./console-live";
import { onChanged } from "../app/changed";

export type OrgDays = 1 | 7 | 30;
export const ORG_WINDOWS: readonly OrgDays[] = [1, 7, 30];
export const ORG_WINDOW_LABEL: Record<OrgDays, string> = { 1: "24h", 7: "7d", 30: "30d" };
export const ORG_POLL_MS = 30_000;

export type OrgWork = { done: number; failed: number; open: number };
export type OrgSpend = { meteredUsd: number; planCalls: number };
export type OrgModel = { model: string; calls: number };
export type OrgOpenItem = {
  roleKey: string; actId: string; status: string; label: string; since: string | null;
  runId: string | null; attentionId: string | null;
};
export type OrgAbilityGap = { ability: string; label: string; roles: string[] };
export type OrgStepGap = {
  workflowKey: string; workflowName: string; stepId: string; stepTitle: string; roleKey: string;
  ability: string; label: string;
};
export type OrgStepRef = { workflowKey: string; workflowName: string; stepId: string; stepTitle: string };

export type OrgTeam = {
  key: string; label: string; tip: string | null; members: string[];
  /** The team's agents' own open asks, as items (a gate is yours, never the team's). */
  needsYou: number;
  flowCol: number | null; selfHandoffs: number;
  work: OrgWork; waiting: OrgOpenItem[]; waitingTotal: number;
  membersLoad: { roleKey: string; busySeconds: number }[];
  spend: OrgSpend; models: OrgModel[]; configuredModels: string[];
  gaps: { abilities: OrgAbilityGap[]; stepGaps: OrgStepGap[]; missingRoles: number;
          kitOk: boolean; kitReason: string | null };
};
export type OrgAgent = {
  roleKey: string; team: string; registered: boolean; work: OrgWork; busySeconds: number;
  spend: OrgSpend; models: OrgModel[]; configuredModel: string | null;
  open: OrgOpenItem[]; openTotal: number; namedOn: OrgStepRef[];
  gaps: { abilities: { ability: string; label: string }[]; stepGaps: OrgStepGap[] };
};
export type OrgEdge = {
  from: string; to: string; handoffs: number; failed: number; waiting: number; stale: number;
  lastAt: string | null; how?: string | null; label?: string | null;
};
export type OrgMissingRole = { roleKey: string; team: string; steps: OrgStepRef[] };
export type OrgUnplannedPair = {
  a: string; b: string; handoffs: number; parent: string; child: string; parentIs: "manager" | "sender";
};
export type OrgDrift = {
  plannedUnused: { agent: string; reportsTo: string }[];
  unplannedPairs: OrgUnplannedPair[];
  lines: number;
};
export type OrgTotals = {
  meteredUsd: number; planCalls: number; untiedUsd: number; untiedCalls: number;
  spendOk: boolean; spendReason: string | null;
};
export type OrgBoard = {
  projectId: string; days: OrgDays; generatedAt: string; etag: string;
  totals: OrgTotals; teams: OrgTeam[]; agents: OrgAgent[];
  teamEdges: OrgEdge[]; agentEdges: OrgEdge[]; truncated: boolean;
  missingRoles: OrgMissingRole[]; drift: OrgDrift;
};

type Wire = Record<string, unknown>;
const obj = (v: unknown): Wire => (v && typeof v === "object" && !Array.isArray(v) ? v as Wire : {});
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const str = (v: unknown): string => (typeof v === "string" ? v : v == null ? "" : String(v));
const strOrNull = (v: unknown): string | null => (typeof v === "string" && v ? v : null);
const num = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) ? v : 0);

function work(v: unknown): OrgWork {
  const o = obj(v);
  return { done: num(o.done), failed: num(o.failed), open: num(o.open) };
}
function spend(v: unknown): OrgSpend {
  const o = obj(v);
  return { meteredUsd: num(o.metered_usd), planCalls: num(o.plan_calls) };
}
function models(v: unknown): OrgModel[] {
  return arr(v).map((m) => ({ model: str(obj(m).model), calls: num(obj(m).calls) })).filter((m) => m.model);
}
function openItem(v: unknown): OrgOpenItem {
  const o = obj(v);
  return { roleKey: str(o.role_key), actId: str(o.act_id), status: str(o.status), label: str(o.label),
           since: strOrNull(o.since), runId: strOrNull(o.run_id), attentionId: strOrNull(o.attention_id) };
}
function stepRef(v: unknown): OrgStepRef {
  const o = obj(v);
  return { workflowKey: str(o.workflow_key), workflowName: str(o.workflow_name), stepId: str(o.step_id),
           stepTitle: str(o.step_title) };
}
function stepGap(v: unknown): OrgStepGap {
  const o = obj(v);
  return { ...stepRef(o), roleKey: str(o.role_key), ability: str(o.ability), label: str(o.label) || str(o.ability) };
}
function edge(v: unknown): OrgEdge {
  const o = obj(v);
  return { from: str(o.from), to: str(o.to), handoffs: num(o.handoffs), failed: num(o.failed),
           waiting: num(o.waiting), stale: num(o.stale), lastAt: strOrNull(o.last_at),
           how: strOrNull(o.how), label: strOrNull(o.label) };
}

export function parseOrgBoard(body: unknown): OrgBoard {
  const o = obj(body);
  const t = obj(o.totals);
  const d = obj(o.declared_vs_observed);
  const days = ORG_WINDOWS.includes(num(o.days) as OrgDays) ? num(o.days) as OrgDays : 7;
  return {
    projectId: str(o.project_id), days, generatedAt: str(o.generated_at), etag: str(o.etag),
    totals: { meteredUsd: num(t.metered_usd), planCalls: num(t.plan_calls), untiedUsd: num(t.untied_usd),
              untiedCalls: num(t.untied_calls), spendOk: t.spend_ok !== false,
              spendReason: strOrNull(t.spend_reason) },
    teams: arr(o.teams).map((raw) => {
      const x = obj(raw);
      const g = obj(x.gaps);
      return {
        key: str(x.key), label: str(x.label) || str(x.key), tip: strOrNull(x.tip),
        members: arr(x.members).map(str).filter(Boolean), needsYou: num(x.needs_you),
        flowCol: typeof x.flow_col === "number" ? x.flow_col : null, selfHandoffs: num(x.self_handoffs),
        work: work(x.work), waiting: arr(x.waiting).map(openItem), waitingTotal: num(x.waiting_total),
        membersLoad: arr(x.members_load).map((m) => ({ roleKey: str(obj(m).role_key), busySeconds: num(obj(m).busy_seconds) })),
        spend: spend(x.spend), models: models(x.models), configuredModels: arr(x.configured_models).map(str),
        gaps: {
          abilities: arr(g.abilities).map((a) => ({ ability: str(obj(a).ability), label: str(obj(a).label) || str(obj(a).ability),
                                                    roles: arr(obj(a).roles).map(str) })),
          stepGaps: arr(g.step_gaps).map(stepGap), missingRoles: num(g.missing_roles),
          kitOk: g.kit_ok !== false, kitReason: strOrNull(g.kit_reason),
        },
      };
    }).filter((x) => x.key),
    agents: arr(o.agents).map((raw) => {
      const x = obj(raw);
      const g = obj(x.gaps);
      return {
        roleKey: str(x.role_key), team: str(x.team), registered: x.registered !== false,
        work: work(x.work), busySeconds: num(x.busy_seconds), spend: spend(x.spend), models: models(x.models),
        configuredModel: strOrNull(x.configured_model), open: arr(x.open).map(openItem),
        openTotal: num(x.open_total), namedOn: arr(x.named_on).map(stepRef),
        gaps: { abilities: arr(g.abilities).map((a) => ({ ability: str(obj(a).ability), label: str(obj(a).label) || str(obj(a).ability) })),
                stepGaps: arr(g.step_gaps).map(stepGap) },
      };
    }).filter((x) => x.roleKey),
    teamEdges: arr(o.team_edges).map(edge).filter((e) => e.from && e.to),
    agentEdges: arr(o.agent_edges).map(edge).filter((e) => e.from && e.to),
    truncated: o.truncated === true,
    missingRoles: arr(o.missing_roles).map((m) => ({ roleKey: str(obj(m).role_key), team: str(obj(m).team) || "yours",
                                                      steps: arr(obj(m).steps).map(stepRef) })).filter((m) => m.roleKey),
    drift: {
      plannedUnused: arr(d.planned_unused).map((p) => ({ agent: str(obj(p).agent), reportsTo: str(obj(p).reports_to) })),
      unplannedPairs: arr(d.unplanned_pairs).map((p) => {
        const x = obj(p);
        return { a: str(x.a), b: str(x.b), handoffs: num(x.handoffs), parent: str(x.parent), child: str(x.child),
                 parentIs: x.parent_is === "manager" ? "manager" as const : "sender" as const };
      }),
      lines: num(d.lines),
    },
  };
}

// ── The poll: 30s, visible-only, pause-aware, ETag'd ───────────────────────────────────────

export type OrgBoardState = {
  board: OrgBoard | null;
  /** The last failure's reason while the board could not be read. Cleared by an answer. */
  error: string | null;
  loading: boolean;
  /** When the board on screen was last confirmed (an answer or a 304). */
  confirmedAt: number | null;
  /** Ask now, past the server's 15s cache: after a write. */
  refresh: () => void;
};

export function orgBoardUrl(projectId: string, days: OrgDays, fresh = false): string {
  return `/v1/agents/org-board?project_id=${encodeURIComponent(projectId)}&days=${days}${fresh ? "&fresh=1" : ""}`;
}

export function useOrgBoard(projectId: string, days: OrgDays, enabled = true): OrgBoardState {
  const [board, setBoard] = useState<OrgBoard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [confirmedAt, setConfirmedAt] = useState<number | null>(null);
  const etag = useRef<string | null>(null);
  const kick = useRef<(fresh: boolean) => void>(() => {});

  useEffect(() => {
    if (!projectId || !enabled) return undefined;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let inflight = false;
    let gen = 0;
    etag.current = null; // a new window is a new answer
    const hidden = () => typeof document !== "undefined" && document.visibilityState === "hidden";

    const tick = async (mine: number, fresh: boolean) => {
      if (stopped || mine !== gen) return;
      if (!hidden() && isConsoleLive() && !inflight) {
        inflight = true;
        try {
          const r = await fetch(orgBoardUrl(projectId, days, fresh), {
            cache: "no-store", headers: etag.current && !fresh ? { "If-None-Match": etag.current } : {},
          });
          if (stopped || mine !== gen) return;
          if (r.status === 304) {
            setError(null);
            setConfirmedAt(Date.now());
          } else if (r.ok) {
            etag.current = r.headers.get("etag");
            setBoard(parseOrgBoard(await r.json()));
            setError(null);
            setConfirmedAt(Date.now());
          } else {
            let said = "";
            try { said = String(((await r.json()) as { detail?: unknown }).detail ?? ""); } catch { /* none */ }
            setError(said || `The API answered ${r.status}.`);
          }
        } catch (e) {
          if (!stopped) setError(e instanceof Error ? e.message : "The API did not answer.");
        } finally {
          inflight = false;
          if (!stopped) setLoading(false);
        }
      }
      if (!stopped && mine === gen) timer = setTimeout(() => void tick(mine, false), ORG_POLL_MS);
    };
    const restart = (fresh: boolean) => {
      if (stopped || hidden() || !isConsoleLive()) return;
      if (timer) clearTimeout(timer);
      gen += 1;
      void tick(gen, fresh);
    };
    kick.current = restart;
    const onVis = () => restart(false);
    if (typeof document !== "undefined") document.addEventListener("visibilitychange", onVis);
    const offChanged = onChanged(() => restart(true));
    const offPause = subscribeConsoleLive((on) => { if (on) restart(false); });
    void tick(gen, false);
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      if (typeof document !== "undefined") document.removeEventListener("visibilitychange", onVis);
      offChanged();
      offPause();
    };
  }, [projectId, days, enabled]);

  const refresh = useCallback(() => kick.current(true), []);
  return { board, error, loading: loading && !board && !error, confirmedAt, refresh };
}

// ── The Edge panel's evidence ───────────────────────────────────────────────────────────────

export type OrgHandoff = {
  actId: string; from: string; to: string; status: string; label: string; how: string | null;
  startedAt: string | null; runId: string | null;
};

export async function listEdgeHandoffs(projectId: string, days: OrgDays, sources: string[], targets: string[],
                                       limit = 10): Promise<{ ok: true; items: OrgHandoff[] } | { ok: false; error: string }> {
  const q = new URLSearchParams({ project_id: projectId, days: String(days), source: sources.join(","),
                                  target: targets.join(","), limit: String(limit) });
  try {
    const r = await fetch(`/v1/agents/org-board/handoffs?${q.toString()}`, { cache: "no-store" });
    const body = obj(await r.json().catch(() => ({})));
    if (!r.ok) return { ok: false, error: str(body.detail) || `The API answered ${r.status}.` };
    return { ok: true, items: arr(body.items).map((raw) => {
      const x = obj(raw);
      return { actId: str(x.act_id), from: str(x.from), to: str(x.to), status: str(x.status), label: str(x.label),
               how: strOrNull(x.how), startedAt: strOrNull(x.started_at), runId: strOrNull(x.run_id) };
    }) };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : "The API did not answer." };
  }
}

// ── Words (one place) ───────────────────────────────────────────────────────────────────────

/** `9h`, `40m`, `0`: busy time, said plainly. */
export function busyWords(seconds: number): string {
  if (seconds < 60) return "0";
  const m = Math.round(seconds / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return h >= 48 ? `${Math.round(h / 24)}d` : `${h}h`;
}

/** `1h 12m`, `3d 2h`, `40m`: how long an open item has waited. */
export function ageWords(since: string | null, now = Date.now()): string {
  const t = since ? Date.parse(since) : NaN;
  if (Number.isNaN(t)) return "";
  const m = Math.max(0, Math.floor((now - t) / 60_000));
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return m % 60 ? `${h}h ${m % 60}m` : `${h}h`;
  const d = Math.floor(h / 24);
  return h % 24 ? `${d}d ${h % 24}h` : `${d}d`;
}

/** `$4.10`: metered money only. Under a cent says `<$0.01`, never `$0.00` for real spend. */
export function usdWords(usd: number): string {
  if (usd > 0 && usd < 0.01) return "<$0.01";
  return `$${usd.toFixed(2)}`;
}

/** The status word for an open item: `Needs you` / `Working` / `Not started`. */
export function openStatusWord(status: string): string {
  if (status === "awaiting_input") return "Needs you";
  if (status === "pending") return "Not started";
  return "Working";
}

export function windowWords(days: OrgDays): string {
  return ORG_WINDOW_LABEL[days];
}
