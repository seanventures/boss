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
  // A cut never ends on a comma or a dash ("Support helpdesk with AI-drafted," → "… AI-drafted").
  return (head.length <= 40 ? head : head.split(/\s+/).slice(0, 4).join(" ")).slice(0, 60).replace(/[\s,;:.–—-]+$/, "");
}

// ── what BOSS will do ───────────────────────────────────────────────────────────────────────

export type Face = { agent_id?: string | null; role_key?: string | null; name?: string | null; role?: string | null } | null;
/** `short`: what a person approves here, in the strip legend's few words ("PRD", "deploy"). */
export type Step = { id: string; label: string; short?: string; you: boolean; agent: Face };
export type Estimate = { minutes: number | null; minutes_basis: string | null; minutes_runs: number; usd: number | null; usd_runs: number } | null;
export type Workflow = {
  key: string; name: string; short: string; detail: string; work_class: string | null; path: boolean;
  steps: number; step_list: Step[]; approvals: number; approve_words: string[];
  input_schema: unknown; estimate: Estimate;
};
export type Candidate = { agent_id: string; role_key: string; name: string; role: string; captain_capable?: boolean; recommended?: boolean; line?: string };
export type Host = { host_id: string; host: string; address: string | null; port: number | null; suggested: boolean; shared: boolean };

/** THE way a staging host and its port are written: host:port ("203.0.113.20:8006"), the host
 *  alone with no port; an IPv6 literal is bracketed so its port stays readable. */
export function hostPort(host: string, port: number | null | undefined): string {
  if (!port) return host;
  return `${host.includes(":") && !host.startsWith("[") ? `[${host}]` : host}:${port}`;
}
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
  repo_ids?: string[];
  /** A folder on a server: read in place (today), or copied here (`import_code`, local first). */
  folder?: { host_id: string; path: string; import_code?: boolean; keep_history?: boolean } | null;
  /** BOSSAPP-918: "local" keeps a new app's code on this machine; "github" with `new_repo` has BOSS make the repo. */
  code_location?: "local" | "github" | null;
  /** BOSSAPP-917: the GitHub repo BOSS makes at Create (a person pressing Create is the decision). */
  new_repo?: { owner: string; repo: string; private: boolean } | null;
  /** BOSSAPP-923: a folder on this machine (inside the install's import directory), imported. */
  local_folder?: { path: string; keep_history: boolean } | null;
  captain?: string | null; crew?: string[] | null;
  stack?: string | null; stack_language?: string | null; connectors?: string[]; connector_secrets?: Record<string, string>; accept_egress?: string[];
  workflow_key?: string | null; inputs?: Record<string, unknown>; notify_when?: string[]; audience?: string | null;
  done_when?: string[]; read_first?: string[]; talks_to?: Record<string, unknown>[];
  staging?: { host_id: string; port: number | null } | null; map_rules?: boolean; start_anyway?: string[];
  dedupe_key?: string | null; project_id?: string | null;
  /** A repo already in a project: only after "Make a separate project anyway". */
  allow_shared_repo?: boolean;
};
export type CreateResult = {
  created: true; project_id: string; name: string; kind: Kind; href: string;
  steps: { key: string; ok: boolean; label: string }[]; started: { run_id?: string | null; onboarding?: { run_id: string | null } };
  /** The new GitHub repo (BOSSAPP-917): made and switched to, or refused in words (the project stays local). */
  publish?: { ok: boolean; full_name?: string; message?: string; error?: string; owner?: string; repo?: string } | null;
};
export type CreateRefusal = {
  step: string; message: string; created: boolean; project_id?: string; retry?: { project_id: string };
  readiness?: unknown; error?: string; fix_href?: string;
};

// ── two doors (BOSSAPP-916) and where a new app's code lives (917/918/923) ─────────────────

/** The sheet's first screen picks a door; each door has its own short flow. */
export type Door = "choose" | "new" | "existing";
/** Where a NEW app's code lives: on this machine (local first), a new GitHub repo BOSS makes, or
 *  a folder in a repo you already have (a monorepo: today's behaviour). */
export type Where = "local" | "github_new" | "repo";
/** How existing code in a folder comes in: keep building on its GitHub repo, copy it here (local
 *  first, history kept), or read it in place on its server (read-only, as before). */
export type Bring = "github" | "copy" | "in_place";

/** The door a `?new_project=` start opens on: an idea (or words handed over) → Start a new app,
 *  import → Bring existing code, the sample and the plain link → the two doors. */
export function doorOf(startKind: string | null | undefined, hasText = false): Door {
  if (hasText || startKind === "idea") return "new";
  if (startKind === "repo" || startKind === "folder" || startKind === "import") return "existing";
  return "choose";
}

/** A GitHub repository name from the project's name: "Expense Tracker: receipts" → "expense-tracker". */
export function repoSlug(name: string): string {
  const head = name.split(/[:—–|]/)[0];
  return head.toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "").replace(/[^a-z0-9._-]+/g, "-")
    .replace(/-{2,}/g, "-").replace(/^[-.]+|[-.]+$/g, "").slice(0, 60) || "app";
}

/** The default Where: on this machine when no GitHub account is connected; otherwise a new
 *  GitHub repo — unless BOSS cannot make one (its check said so): then on this machine. */
export function defaultWhere(hasGitHub: boolean, check: RepoCheck | null): Where {
  if (!hasGitHub) return "local";
  // Only what BOSS cannot do moves the default (no credential, a personal account …); a name
  // that is taken is renamed for the person (nextFreeName), and GitHub not answering is not a no.
  if (check && !check.ok && !["exists", "failed"].includes(check.reason)) return "local";
  return "github_new";
}

/** The repo name BOSS proposes when the one from the brief is taken: name-2, name-3 … */
export function nextFreeName(slug: string, bump: number): string {
  return bump ? `${slug}-${bump + 1}` : slug;
}

/** The ways a picked folder can come in, and the one picked for the person. A GitHub remote
 *  offers "keep using GitHub" (its repo, when BOSS may use it) and "copy it here"; a server's
 *  folder can also be read in place (today's read-only path). */
export function bringChoices(source: "local" | "host", github: string | null | undefined, inRegistry: boolean): { choices: Bring[]; pick: Bring } {
  const choices: Bring[] = [];
  if (github) choices.push("github");
  choices.push("copy");
  if (source === "host") choices.push("in_place");
  const pick: Bring = github && inRegistry ? "github" : "copy";
  return { choices, pick };
}

export type LocalFolder = { path: string; name: string; git: boolean; github: string | null; branch: string | null; files: number;
  /** Read the way a server's folder is: its top-level names, stack and build words. */
  top?: string[]; stack?: string[]; build?: string | null };
export type LocalFolders = { configured: boolean; root?: string; items: LocalFolder[]; words?: string };
export type RepoCheck = { ok: boolean; full_name: string; reason: string; words: string; exists: boolean; owner_type: string | null };

/** The one fix for a GitHub refusal: Repo and models for a missing credential or permission;
 *  GitHub's own new-repository page for a personal account (an App cannot create there). */
export function repoCheckFix(check: { ok: boolean; full_name?: string; reason?: string; error?: string } | null): { label: string; href: string; external: boolean } | null {
  if (!check || check.ok) return null;
  const reason = check.reason || check.error || "";
  const [owner, repo] = String(check.full_name ?? "").split("/");
  if (reason === "personal_account") {
    return { label: "Create it on GitHub", external: true,
             href: `https://github.com/new?owner=${encodeURIComponent(owner ?? "")}&name=${encodeURIComponent(repo ?? "")}&visibility=private` };
  }
  if (reason === "no_github_credential" || reason === "no_admin_permission" || reason === "github_not_connected") {
    return { label: "Repo and models", href: "?area=setup&focus=repository", external: false };
  }
  return null;
}

export const addProjectApi = {
  understand: (projectId: string, body: { words: string; hint?: string; previous?: Brief | null; pinned?: Partial<Record<BriefField, unknown>> }) =>
    idPost<Understanding>(`/v1/projects/${enc(projectId)}/add-project/understand`, body),
  connectors: (projectId: string, brief: Brief, words: string) =>
    idPost<{ status: string; picks: Picks | null; message?: string }>(`/v1/projects/${enc(projectId)}/add-project/connectors`, { brief, words }),
  preview: (projectId: string, mode: Kind, appName = "", signal?: AbortSignal) =>
    idGet<Preview>(`/v1/projects/${enc(projectId)}/add-project/preview?mode=${mode}&app_name=${enc(appName)}`, { signal }),
  repo: (projectId: string, repoId: string, signal?: AbortSignal) =>
    idGet<RepoRead>(`/v1/projects/${enc(projectId)}/add-project/repo/${enc(repoId)}`, { signal }),
  /** Bring existing code › This machine: the import directory's folders (read-only). */
  localFolders: (projectId: string, q = "", signal?: AbortSignal) =>
    idGet<LocalFolders>(`/v1/projects/${enc(projectId)}/local-folders${q ? `?q=${enc(q)}` : ""}`, { signal }),
  /** Can BOSS make owner/repo on GitHub (or publish into it)? Reads only: the engine's words. */
  checkRepo: (projectId: string, owner: string, repo: string, purpose: "create" | "existing" = "create", signal?: AbortSignal) =>
    idGet<RepoCheck>(`/v1/projects/${enc(projectId)}/github-repository/check?owner=${enc(owner)}&repo=${enc(repo)}&purpose=${purpose}`, { signal }),
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
export const ADD_PROJECT_TIP = "Add a project: start a new app, or bring existing code";
