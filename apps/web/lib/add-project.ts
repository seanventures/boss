/**
 * ADD A PROJECT v2 (docs/product/wireframes/project-experience/new-project-v2): the one screen's
 * pure rules and its calls. The screen is app/start-project/add-project.tsx.
 *
 *   detect()       one multi-line input: an idea, a repository (owner/repo, a GitHub URL) or a
 *                  folder (/path) — BOSS tells which; the kind pill forces another reading
 *   marks()        Helm's additions in the brief, dotted-underlined
 *   the calls      POST …/add-project/understand · …/connectors · GET …/preview · …/repo/{id}
 *                  and POST /v1/add-project (Create and start: checked whole, then committed)
 */
import { enc, idGet, idPost, IdentityError } from "./identity/api";

export type Kind = "idea" | "repo" | "folder";

/** What the words are. A repository is one line that is `owner/repo` or a github.com URL (more
 *  lines under it are a note to Helm); a folder is an absolute path; everything else is an idea.
 *  A bare single word with no space reads as a repository search (`helpdesk`). */
export function detect(text: string): { kind: Kind; ref: string; query: string; note: string } {
  const lines = text.replace(/\r/g, "").split("\n");
  const first = (lines[0] ?? "").trim();
  const note = lines.slice(1).join("\n").trim();
  const url = first.match(/^(?:https?:\/\/)?(?:www\.)?github\.com\/([\w.-]+)\/([\w.-]+?)(?:\.git)?(?:[/?#].*)?$/i);
  if (url) return { kind: "repo", ref: `${url[1]}/${url[2]}`, query: url[2], note };
  if (/^\/[^\s]*$/.test(first)) return { kind: "folder", ref: first, query: first.split("/").filter(Boolean).pop() ?? "", note };
  const slug = first.match(/^([\w.-]+)\/([\w.-]*)$/);
  if (slug && !/\s/.test(first)) return { kind: "repo", ref: first, query: slug[2] || slug[1], note };
  if (first && !/\s/.test(first) && lines.filter((l) => l.trim()).length === 1 && /^[\w.-]{2,}$/.test(first) && /[-_]|^[a-z0-9]+$/.test(first)) {
    return { kind: "repo", ref: "", query: first, note };
  }
  return { kind: "idea", ref: "", query: "", note: "" };
}

/** The input's line count, for "4 lines · grows" (blank trailing lines not counted). */
export function lineCount(text: string): number {
  return text.replace(/\s+$/, "").split("\n").length;
}

export type Question = { question: string; options: string[]; answer: string | null };
export type Brief = {
  title: string; problem: string; who_uses: string; must_haves: string[]; out_of_scope: string[];
  done_when: string; open_questions: Question[]; added: string[]; pinned: string[]; raw?: boolean;
};
export type Pick = { key: string; because: string };
export type Picks = { stack: Pick | null; connectors: Pick[]; workflow: Pick | null };
export type Understanding = {
  status: "ok" | "no_model" | "timeout" | "empty"; brief: Brief; picks: Picks; message?: string;
  by?: { engine?: string; model?: string };
};

export const BRIEF_FIELDS = ["title", "problem", "who_uses", "must_haves", "out_of_scope", "done_when", "open_questions"] as const;
export type BriefField = (typeof BRIEF_FIELDS)[number];

/** A project name from the brief's title ("HelpDesk AI: email support…" → "HelpDesk AI"). */
export function nameFromTitle(title: string): string {
  const head = title.split(/[:—––—|]/)[0].trim();
  return (head.length <= 40 ? head : head.split(/\s+/).slice(0, 4).join(" ")).slice(0, 60);
}

// ── what BOSS will do ───────────────────────────────────────────────────────────────────────

export type Face = { agent_id?: string | null; role_key?: string | null; name?: string | null; role?: string | null } | null;
export type Step = { id: string; label: string; you: boolean; agent: Face };
export type Estimate = { minutes: number | null; minutes_basis: string | null; minutes_runs: number; usd: number | null; usd_runs: number } | null;
export type Workflow = {
  key: string; name: string; short: string; detail: string; work_class: string | null; path: boolean;
  steps: number; step_list: Step[]; approvals: number; approve_words: string[];
  input_schema: unknown; estimate: Estimate;
};
export type Candidate = { agent_id: string; role_key: string; name: string; role: string; captain_capable?: boolean; recommended?: boolean; line?: string };
export type Host = { host_id: string; host: string; address: string | null; port: number | null; suggested: boolean; shared: boolean };
export type Dot = { key: string; label: string; ok: boolean; detail: string; href: string | null };
export type Preview = {
  mode: Kind; workflows: Workflow[]; default_workflow: string | null;
  captain: { role_key: string | null; name: string | null; role?: string | null; squad: { role_key: string; name: string; role?: string }[]; candidates: Candidate[] };
  staging: Host[]; readiness: Dot[]; default_repository: string | null;
  stacks: { id: string; label: string; ready: boolean }[];
};

export type RepoRead = {
  facts: { repo_id: string; full_name: string; branch: string; facts: { language: string | null; framework: string | null; build: string | null; tests: boolean | null }; files_total?: number | null; suggested_name: string; mapping: { state: string; percent: number; done: boolean; files?: number | null; parsed?: number | null } };
  what_it_is: string; parts: { path: string; files: number }[]; files_total: number | null;
  health: { label: string; tone: string }[]; onboarding_will: string[];
};

/** "≈ $4–7 · ≈ 35 min to PR" from the workflow's history (or its typical minutes); null when
 *  nothing is known — never an invented figure. */
export function estimateWords(w: Workflow | null | undefined, kind: Kind): string | null {
  if (kind !== "idea") return "≈ 5 min to ready";
  const e = w?.estimate;
  if (!e) return null;
  const parts: string[] = [];
  if (e.usd != null) parts.push(`≈ $${e.usd < 1 ? e.usd.toFixed(2) : Math.round(e.usd)}`);
  if (e.minutes) parts.push(`≈ ${e.minutes} min to PR`);
  return parts.length ? parts.join(" · ") : null;
}

export function estimateTip(w: Workflow | null | undefined): string {
  const e = w?.estimate;
  if (!e) return "Cost after its first run here";
  const t = e.minutes_basis === "history" ? `time from ${e.minutes_runs} runs here` : "time from the steps' typical minutes";
  const c = e.usd != null ? `cost from ${e.usd_runs} runs here` : "cost after its first run here";
  return `Builds like this: ${t}; ${c}.`;
}

/** The connector stack every picked connector supports, for the adapter picked (or Auto). */
export function stackLanguage(adapterId: string | null | undefined, languages: Record<string, string>): string | null {
  if (!adapterId) return null;
  return languages[adapterId] ?? null;
}

// ── the calls ───────────────────────────────────────────────────────────────────────────────

export type CreateBody = {
  context_project_id: string; kind: Kind; name: string; words: string; brief?: Brief | null; note?: string | null;
  repo_ids?: string[]; folder?: { host_id: string; path: string } | null; captain?: string | null; crew?: string[] | null;
  stack?: string | null; stack_language?: string | null; connectors?: string[]; connector_secrets?: Record<string, string>; accept_egress?: string[];
  workflow_key?: string | null; inputs?: Record<string, unknown>; notify_when?: string[]; audience?: string | null;
  done_when?: string[]; read_first?: string[]; talks_to?: Record<string, unknown>[];
  staging?: { host_id: string; port: number | null } | null; map_rules?: boolean; start_anyway?: string[];
  dedupe_key?: string | null; project_id?: string | null;
};
export type CreateResult = {
  created: true; project_id: string; name: string; kind: Kind; href: string;
  steps: { key: string; ok: boolean; label: string }[]; started: { run_id?: string | null; onboarding?: { run_id: string | null } };
};
export type CreateRefusal = {
  step: string; message: string; created: boolean; project_id?: string; retry?: { project_id: string };
  readiness?: unknown; error?: string; fix_href?: string;
};

export const addProjectApi = {
  understand: (projectId: string, body: { words: string; hint?: string; previous?: Brief | null; pinned?: Partial<Record<BriefField, unknown>> }) =>
    idPost<Understanding>(`/v1/projects/${enc(projectId)}/add-project/understand`, body),
  connectors: (projectId: string, brief: Brief, words: string) =>
    idPost<{ status: string; picks: Picks | null; message?: string }>(`/v1/projects/${enc(projectId)}/add-project/connectors`, { brief, words }),
  preview: (projectId: string, mode: Kind, appName = "", signal?: AbortSignal) =>
    idGet<Preview>(`/v1/projects/${enc(projectId)}/add-project/preview?mode=${mode}&app_name=${enc(appName)}`, { signal }),
  repo: (projectId: string, repoId: string, signal?: AbortSignal) =>
    idGet<RepoRead>(`/v1/projects/${enc(projectId)}/add-project/repo/${enc(repoId)}`, { signal }),
  /** Create and start. A refusal comes back as {step, message, …} (never thrown), so the sheet
   *  can say it inline; a step-up or consent is the caller's (useGuardedCall / withConsent). */
  create: async (body: CreateBody, onKept?: (projectId: string) => void): Promise<{ ok: true; data: CreateResult } | { ok: false; refusal: CreateRefusal; status: number }> => {
    try {
      return { ok: true, data: await idPost<CreateResult>("/v1/add-project", body) };
    } catch (e) {
      // A refusal only the start could make keeps the project: every retry names it.
      const kept = e instanceof IdentityError ? (e.body.retry as { project_id?: unknown } | undefined)?.project_id : null;
      if (typeof kept === "string" && kept) onKept?.(kept);
      if (e instanceof IdentityError && (e.code === "step_up_required" || e.body.error === "consent_required" || e.status === 401)) throw e;
      if (e instanceof IdentityError && typeof e.body.step === "string") {
        return { ok: false, status: e.status, refusal: { ...(e.body as unknown as CreateRefusal), message: typeof e.body.message === "string" && e.body.message ? e.body.message : e.message } };
      }
      throw e;
    }
  },
};

/** Where the Moved banner and the doors send a new app: the one Add a project sheet. */
export const ADD_PROJECT_TIP = "Add a project: an idea, a repo or a folder — one screen";
