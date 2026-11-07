/**
 * Waiting for what, and what the Shepherd found (engine/boss_engine/services/stuck_work.py).
 *
 * The server says both from real state; the web maps none of its own. Every surface that shows a
 * build that is not working (the build page header and Now card, Project › Work rows and drawer,
 * Agents at work, a Needs-you card) reads these two shapes and draws them with
 * components/stuck-work.tsx — never a bare "Queued" / "Waiting".
 */

/** Whose move it is: a person's, BOSS's (it is on it / will retry), the queue's, or nobody's. */
export type WaitWho = "you" | "boss" | "queue" | "stuck";

export type WaitReason = {
  code: string;
  who: WaitWho;
  words: string;
  detail: string;
  since: string | null;
  etaMinutes: number | null;
};

export type DiagnosisStepKey = "stuck" | "checked" | "cause" | "tried" | "next";

export type DiagnosisStep = { k: DiagnosisStepKey; label: string; detail: string; who?: string };

export type DiagnosisLinks = { log?: string; run?: string };

export type Diagnosis = {
  at: string | null;
  headline: string;
  cause: string;
  chosen: string;
  acted: boolean;
  tried: string;
  next: string;
  who: string;
  needsPerson: boolean;
  cardId: string | null;
  by: { agent?: string; engine?: string; model?: string; source?: string };
  steps: DiagnosisStep[];
  /** Why BOSS did not fix it itself (only when a person must act). */
  whyNotFixed: string;
  links: DiagnosisLinks;
  /** Fixes the Shepherd has taken on this build, and its cap. */
  fixes: number; maxFixes: number;
};

const o = (v: unknown): Record<string, unknown> => (v && typeof v === "object" ? (v as Record<string, unknown>) : {});
const s = (v: unknown): string => (typeof v === "string" ? v : "");
const WHO = new Set(["you", "boss", "queue", "stuck"]);
const STEPS: DiagnosisStepKey[] = ["stuck", "checked", "cause", "tried", "next"];

export function parseWaitReason(v: unknown): WaitReason | null {
  const x = o(v);
  const words = s(x.words);
  if (!words) return null;
  const who = s(x.who);
  return {
    code: s(x.code), who: (WHO.has(who) ? who : "boss") as WaitWho, words, detail: s(x.detail),
    since: s(x.since) || null,
    etaMinutes: typeof x.eta_minutes === "number" && Number.isFinite(x.eta_minutes) ? x.eta_minutes : null,
  };
}

export function parseDiagnosis(v: unknown): Diagnosis | null {
  const x = o(v);
  const steps = (Array.isArray(x.steps) ? x.steps : []).map((st) => {
    const y = o(st);
    return { k: s(y.k) as DiagnosisStepKey, label: s(y.label), detail: s(y.detail), who: s(y.who) || undefined };
  }).filter((st) => STEPS.includes(st.k) && st.label);
  if (!steps.length && !s(x.headline)) return null;
  const by = o(x.by);
  return {
    at: s(x.at) || null, headline: s(x.headline), cause: s(x.cause), chosen: s(x.chosen),
    acted: x.acted === true, tried: s(x.tried), next: s(x.next), who: s(x.who),
    needsPerson: x.needs_person === true, cardId: s(x.card_id) || null,
    by: { agent: s(by.agent) || undefined, engine: s(by.engine) || undefined,
          model: s(by.model) || undefined, source: s(by.source) || undefined },
    steps, whyNotFixed: s(x.why_not_fixed),
    links: { log: s(o(x.links).log) || undefined, run: s(o(x.links).run) || undefined },
    fixes: typeof x.fixes === "number" ? x.fixes : 0, maxFixes: typeof x.max_fixes === "number" ? x.max_fixes : 3,
  };
}

/** "Shepherd · claude-sonnet" / "Shepherd · BOSS's rules (no model answered)". */
export function diagnosedBy(d: Diagnosis): string {
  const agent = d.by.agent || "Shepherd";
  const how = d.by.model || d.by.engine || d.by.source;
  return how ? `${agent} · ${how}` : agent;
}

/** One build on the Stalled work board (engine stuck_work.board). */
export type StalledEntry = {
  runId: string; title: string; status: string; at: string | null; cause: string; next: string; who: string;
  tried: string; whyNotFixed: string; fixes: number; maxFixes: number; cardId: string | null;
  links: DiagnosisLinks; steps: DiagnosisStep[];
};
export type StalledBoard = {
  owner: string; maxFixes: number;
  fixed: StalledEntry[]; trying: StalledEntry[]; needsPerson: StalledEntry[];
};

const entry = (v: unknown): StalledEntry => {
  const x = o(v);
  const d = parseDiagnosis({ ...x, headline: s(x.cause) || "-" });
  return {
    runId: s(x.run_id), title: s(x.title), status: s(x.status), at: s(x.at) || null, cause: s(x.cause), next: s(x.next),
    who: s(x.who), tried: s(x.tried), whyNotFixed: s(x.why_not_fixed),
    fixes: typeof x.fixes === "number" ? x.fixes : 0, maxFixes: typeof x.max_fixes === "number" ? x.max_fixes : 3,
    cardId: s(x.card_id) || null, links: d?.links ?? {}, steps: d?.steps ?? [],
  };
};

export function parseStalledBoard(v: unknown): StalledBoard {
  const x = o(v);
  const list = (k: string) => (Array.isArray(x[k]) ? (x[k] as unknown[]) : []).map(entry).filter((e) => e.runId);
  return { owner: s(x.owner) || "Shepherd", maxFixes: typeof x.max_fixes === "number" ? x.max_fixes : 3,
           fixed: list("fixed"), trying: list("trying"), needsPerson: list("needs_person") };
}

export const stalledPath = (projectId: string) => `/v1/ambient/stalled-work?${new URLSearchParams({ project_id: projectId }).toString()}`;
