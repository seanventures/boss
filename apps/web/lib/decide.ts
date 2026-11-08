/**
 * WHERE PEOPLE DECIDE — the console's typed client (Govern › Where people decide; the API is
 * apps/api/app/routers/decide_list.py, the list itself engine/boss_engine/services/decide_list.py).
 *
 * Reads go to the same-origin `/v1` proxy. Every write goes through obsWrite → apiWrite (one press,
 * one effect; a `step_up_required` opens "Confirm it's you" — changing the list is ON the list, so
 * every save asks for the passkey).
 */
import { obsWrite, type WriteResult } from "../app/observability/obs-write";

const s = (v: unknown, d = ""): string => (typeof v === "string" ? v : v == null ? d : String(v));
const n = (v: unknown, d = 0): number => (typeof v === "number" && Number.isFinite(v) ? v : d);
const a = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const o = (v: unknown): Record<string, unknown> => (v && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {});
const sn = (v: unknown): string | null => (typeof v === "string" && v ? v : null);

export type Window = { from_day: string; from_time: string; to_day: string; to_time: string; tz: string };
export type Who = { person?: string; name?: string; seat?: string } | null;
export type Rule = {
  always_person: boolean; decider: Who; backup: Who; backup_after_minutes: number; quiet: Window[];
  step_up: "" | "passkey" | "typed_name"; approvers: number; cap_usd?: number; cap_per?: string; scope?: string;
};
export type Person = { id: string; name: string; email?: string };
export type TrailItem = { at: string | null; type: "decision" | "change"; id: string; project_id?: string; who: string | null;
  who_name: string | null; status?: string; via?: string; scope?: string; what: string };
export type Card = {
  key: string; title: string; icon: string; fixed: boolean; why: string; line: string;
  rule: Rule | null; here: Rule | null;
  decider: { kind: "seat" | "person"; id: string; label: string; people: Person[]; vacant?: boolean };
  backup: Who; scope: { per_project: boolean; projects: string[] };
  count_24h: number; bars: number[]; agents_may: string; step_up: string;
  delegate: { allowed: string; mine: number }; trail: TrailItem[];
  last_change: { at: string | null; who: string | null; kind: string } | null;
};
export type Summary = { hours: number; handled_by_agents: number; asked_a_person: number; waiting: number;
  oldest: { id: string; project_id: string; question: string; at: string | null } | null };
export type Board = { cards: Card[]; people: Person[]; backstop: { spend_usd: number }; activity: Summary | null };
export type AgentRow = { role: string; alone: number; waited: number; lane: Array<[number, number]> };
export type Activity = { summary: Summary; agents: AgentRow[] };
export type Edit = { kind: string; field: string; value: unknown; scope: string; title: string; says: string };
export type Said = { says: string; kind: string };
export type ReplayRow = { kind: string; field: string; n: number; words: string };
export type Understood = { status: "understood" | "no_model" | "empty"; edits: Edit[]; already_allowed: Said[];
  cannot: Said[]; reply: string; replay: ReplayRow[] };

const q = (params: Record<string, string>) => new URLSearchParams(params).toString();
export const boardPath = (projectId: string) => `/v1/decide?${q({ project_id: projectId })}`;
export const activityPath = (projectId: string) => `/v1/decide/activity?${q({ project_id: projectId })}`;

const parseWho = (w: unknown): Who => {
  const x = o(w);
  if (x.person) return { person: s(x.person), name: s(x.name || x.person) };
  if (x.seat) return { seat: s(x.seat) };
  return null;
};
export const parseWindow = (w: unknown): Window => {
  const x = o(w);
  return { from_day: s(x.from_day, "fri"), from_time: s(x.from_time, "15:00"), to_day: s(x.to_day, "mon"),
           to_time: s(x.to_time, "08:00"), tz: s(x.tz, "UTC") };
};
const parseRule = (w: unknown): Rule | null => {
  if (!w || typeof w !== "object") return null;
  const x = o(w);
  return { always_person: x.always_person !== false, decider: parseWho(x.decider), backup: parseWho(x.backup),
           backup_after_minutes: n(x.backup_after_minutes, 120), quiet: a(x.quiet).map(parseWindow),
           step_up: (s(x.step_up) as Rule["step_up"]) || "", approvers: n(x.approvers, 1),
           cap_usd: typeof x.cap_usd === "number" ? x.cap_usd : undefined, cap_per: sn(x.cap_per) ?? undefined,
           scope: sn(x.scope) ?? undefined };
};
const parsePerson = (w: unknown): Person => ({ id: s(o(w).id), name: s(o(w).name || o(w).id), email: s(o(w).email) });
export const parseSummary = (w: unknown): Summary | null => {
  if (!w || typeof w !== "object") return null;
  const x = o(w);
  const old = o(x.oldest);
  return { hours: n(x.hours, 24), handled_by_agents: n(x.handled_by_agents), asked_a_person: n(x.asked_a_person),
           waiting: n(x.waiting),
           oldest: old.id ? { id: s(old.id), project_id: s(old.project_id), question: s(old.question), at: sn(old.at) } : null };
};
export function parseBoard(w: unknown): Board {
  const x = o(w);
  return {
    cards: a(x.cards).map((c) => {
      const y = o(c);
      const d = o(y.decider);
      const lc = o(y.last_change);
      return {
        key: s(y.key), title: s(y.title), icon: s(y.icon), fixed: !!y.fixed, why: s(y.why), line: s(y.line),
        rule: parseRule(y.rule), here: parseRule(y.here),
        decider: { kind: d.kind === "person" ? "person" : "seat", id: s(d.id), label: s(d.label, "Owner"),
                   people: a(d.people).map(parsePerson), vacant: !!d.vacant },
        backup: parseWho(y.backup),
        scope: { per_project: !!o(y.scope).per_project, projects: a(o(y.scope).projects).map((p) => s(p)) },
        count_24h: n(y.count_24h), bars: a(y.bars).map((b) => n(b)), agents_may: s(y.agents_may), step_up: s(y.step_up),
        delegate: { allowed: s(o(y.delegate).allowed, "never"), mine: n(o(y.delegate).mine) },
        trail: a(y.trail).map((t) => {
          const z = o(t);
          return { at: sn(z.at), type: z.type === "change" ? "change" : "decision", id: s(z.id), project_id: sn(z.project_id) ?? undefined,
                   who: sn(z.who), who_name: sn(z.who_name), status: sn(z.status) ?? undefined, via: sn(z.via) ?? undefined,
                   scope: sn(z.scope) ?? undefined, what: s(z.what) } as TrailItem;
        }),
        last_change: lc.at ? { at: sn(lc.at), who: sn(lc.who), kind: s(lc.kind) } : null,
      };
    }),
    people: a(x.people).map(parsePerson),
    backstop: { spend_usd: n(o(x.backstop).spend_usd, 500) },
    activity: parseSummary(x.activity),
  };
}
export function parseActivity(w: unknown): Activity {
  const x = o(w);
  return {
    summary: parseSummary(x.summary) ?? { hours: 24, handled_by_agents: 0, asked_a_person: 0, waiting: 0, oldest: null },
    agents: a(x.agents).map((r) => {
      const y = o(r);
      return { role: s(y.role), alone: n(y.alone), waited: n(y.waited),
               lane: a(y.lane).map((p) => [n(a(p)[0]), n(a(p)[1])] as [number, number]) };
    }),
  };
}
export function parseUnderstood(w: unknown): Understood {
  const x = o(w);
  const said = (v: unknown): Said[] => a(v).map((y) => ({ says: s(o(y).says), kind: s(o(y).kind) })).filter((y) => y.says);
  const status = s(x.status);
  return {
    status: status === "understood" ? "understood" : status === "empty" ? "empty" : "no_model",
    edits: a(x.edits).map((e) => {
      const y = o(e);
      return { kind: s(y.kind), field: s(y.field), value: y.value, scope: s(y.scope, "install"), title: s(y.title || y.kind),
               says: s(y.says) };
    }),
    already_allowed: said(x.already_allowed), cannot: said(x.cannot), reply: s(x.reply),
    replay: a(x.replay).map((r) => ({ kind: s(o(r).kind), field: s(o(r).field), n: n(o(r).n), words: s(o(r).words) })),
  };
}

export async function readJson<T>(path: string, parse: (w: unknown) => T, signal?: AbortSignal): Promise<{ ok: true; data: T } | { ok: false; error: string; status?: number }> {
  try {
    const r = await fetch(path, { cache: "no-store", signal });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      const d = o(body).detail;
      return { ok: false, status: r.status, error: typeof d === "string" ? d : `BOSS answered ${r.status}` };
    }
    return { ok: true, data: parse(body) };
  } catch (e) {
    if ((e as Error)?.name === "AbortError") return { ok: false, error: "aborted" };
    return { ok: false, error: String((e as Error)?.message ?? e) };
  }
}

export function saveRule(projectId: string, kind: string, rule: Partial<Rule>, scope = "install"): Promise<WriteResult> {
  return obsWrite(`/v1/decide/rules/${encodeURIComponent(kind)}`, { project_id: projectId, scope, rule },
                  { scope: `decide:${kind}`, label: "Save" }, "PUT");
}
export async function understandWords(projectId: string, words: string, tz: string): Promise<Understood | { error: string }> {
  const r = await obsWrite<Record<string, unknown>>("/v1/decide/understand", { project_id: projectId, words, tz },
                                                    { scope: "decide:words", label: "Read it" });
  return r.ok ? parseUnderstood(r.body) : { error: r.error ?? "Helm could not read it" };
}
export async function replayEdits(projectId: string, edits: Edit[]): Promise<ReplayRow[] | null> {
  const r = await obsWrite<Record<string, unknown>>("/v1/decide/replay", { project_id: projectId, edits },
                                                    { scope: "decide:replay", label: "Replay" });
  return r.ok ? a(o(r.body).replay).map((x) => ({ kind: s(o(x).kind), field: s(o(x).field), n: n(o(x).n), words: s(o(x).words) })) : null;
}
export function applyEdits(projectId: string, edits: Edit[], words: string): Promise<WriteResult> {
  return obsWrite("/v1/decide/apply", { project_id: projectId, edits: edits.map(({ kind, field, value, scope }) => ({ kind, field, value, scope })), words },
                  { scope: "decide:apply", label: "Apply" });
}

// ── words ───────────────────────────────────────────────────────────────────────────────────

export const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const;
const cap1 = (d: string) => d.slice(0, 1).toUpperCase() + d.slice(1);
export const windowWords = (w: Window) => `${cap1(w.from_day)} ${w.from_time} → ${cap1(w.to_day)} ${w.to_time}`;
export function whoWords(w: Who, seatLabel?: string): string {
  if (!w) return "";
  if (w.person) return w.name || w.person;
  return seatLabel || SEAT_WORDS[w.seat ?? ""] || w.seat || "";
}
export const SEAT_WORDS: Record<string, string> = { ceo: "Owner seat", cto: "Maintainers", cao: "Admin seat",
  release_manager: "Release seat", vp_agents: "Agents seat", owner: "Owner" };
export const STEP_WORDS: Record<string, string> = { passkey: "passkey", typed_name: "type the name" };

/** "3 d ago", "2 h ago", "just now". */
export function ago(iso: string | null, now = Date.now()): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  const m = Math.max(0, Math.round((now - t) / 60000));
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} h ago`;
  return `${Math.round(h / 24)} d ago`;
}
/** "09:12" today, "Tue" this week, else "3 Nov". */
export function when(iso: string | null, now = new Date()): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const days = (now.getTime() - d.getTime()) / 86_400_000;
  if (days < 1 && d.getDate() === now.getDate()) return d.toTimeString().slice(0, 5);
  if (days < 7) return d.toLocaleDateString("en-GB", { weekday: "short" });
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}
export function browserTz(): string {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch { return "UTC"; }
}
export const ROLE_WORDS: Record<string, string> = {
  software_engineer: "Builder", sre: "SRE on call", security_reviewer: "Security", qa: "QA",
  docs_writer: "Docs", chief_of_staff: "Chief of Staff", finops_sentinel: "FinOps", archivist: "Knowledge",
  infra_steward: "Infra", operations_engineer: "Release", pm: "Product",
};
export const roleWords = (role: string) => ROLE_WORDS[role] ?? role.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
