/**
 * MULTI-REPO APPS, as the console reads and writes them (apps/api/app/routes_multirepo.py;
 * docs/product/MULTIREPO.md). Every type and call of the four multi-repo screens lives here:
 *
 *   Repo and models › Repositories   the org's registry: what BOSS may use (repository:register)
 *   Factory › Repositories           the project's repositories and its repo mode (repository:manage)
 *   Factory › Apps › app › Components the app's components, in build order (repository:manage)
 *   New project › Where the code lives / the consent sheet's multi-repo layout
 *
 * Calls go same-origin through app/v1/[...path] with the session's CSRF token (lib/identity/api);
 * a refusal is an IdentityError whose body is the API's {error, message}. Callers wrap writes in
 * useGuardedCall so a step-up is asked for, never shown raw.
 */
import { IdentityError, enc, idGet, idPost, idPut, idPatch, idDelete } from "./identity/api";

// ── Shapes ────────────────────────────────────────────────────────────────────────────────────

export type RepoState = "working" | "read_only" | "needs_fix" | "checking" | "not_allowed" | "archived" | "unreachable";
export type CheckResult = { ok: boolean | null; state?: string | null };
export type RegistryRepo = {
  id: string; full_name: string; description?: string | null; default_branch?: string | null;
  language?: string | null; private?: boolean; archived?: boolean; reachable?: boolean;
  allowed: boolean; source?: string | null;
  checks?: { read?: CheckResult; write?: CheckResult; pull_requests?: CheckResult } | null;
  checked_at?: string | null; state: RepoState;
  used_by?: { project_id: string; name: string }[];
};
export type RegistryConnection = {
  kind: "app" | "token"; account_login?: string | null; account_type?: string | null;
  app_slug?: string | null; change_url?: string | null;
};
export type Registry = {
  org_id?: string; items: RegistryRepo[];
  counts?: { all?: number; allowed?: number; needs_fix?: number; not_allowed?: number };
  in_use?: boolean; connection?: RegistryConnection | null; refreshed?: unknown;
};

export type RoleHint = "contracts" | "backend" | "frontend" | "deploy_config" | "library" | "service" | "other";
export const ROLE_HINTS: { key: RoleHint; label: string }[] = [
  { key: "contracts", label: "Contracts" }, { key: "backend", label: "Backend" },
  { key: "frontend", label: "Frontend" }, { key: "deploy_config", label: "Deploy config" },
  { key: "library", label: "Library" }, { key: "service", label: "Service" }, { key: "other", label: "Other" },
];
export function roleLabel(hint?: string | null): string {
  return ROLE_HINTS.find((r) => r.key === hint)?.label ?? "";
}

export type ProjectRepo = {
  binding_id: string; full_name: string; registry_repo_id?: string | null; language?: string | null;
  description?: string | null; role_hint?: RoleHint | null; is_default?: boolean; base_branch?: string | null;
  state?: RepoState | string | null;
  used_by_apps?: { app_key: string; app_name: string; components?: string[] }[];
  checked_at?: string | null;
};
export type ProjectApp = { app_key: string; app_name: string; chain?: string | null; deploys_with?: string | null;
                           components?: unknown[] };
export type ProjectRepos = {
  project_id: string; repo_mode: "single" | "multi"; items: ProjectRepo[];
  addable: { id: string; full_name: string; language?: string | null; default_branch?: string | null; state?: string | null }[];
  not_allowed_count?: number; registry_in_use?: boolean; apps?: ProjectApp[];
};

export type ShipTarget = { kind: "host" | "package" | "none"; label?: string | null; target_id?: string | null;
                           package?: string | null; url?: string | null };
export type ComponentKind = "code" | "deploy_config";
export type AppComponent = {
  id?: string; name: string; binding_id: string; repository?: string | null; path?: string | null;
  access?: "write" | "read"; kind?: ComponentKind; depends_on?: string[]; ship_target?: ShipTarget | null;
  base_branch?: string | null; repository_active?: boolean; order?: number | null;
  /** What is live (release.component_status): `{}` before anything shipped. */
  status?: ComponentStatus | null;
};
export type ComponentHealth = "healthy" | "failing" | "deploying" | "published";
export type ComponentStatus = { live?: string | null; ships_to?: string | null; health?: ComponentHealth | null;
                                target_id?: string | null };
export type ChangeSetRow = { id: string; title?: string | null; state?: string | null; repos?: string[];
                             prs?: number | null; created_at?: string | null; mode?: string | null; run_id?: string | null };
export type AppComponents = {
  project_id: string; app_key: string; app_name: string; repo_mode?: "single" | "multi";
  components: AppComponent[]; deploy_config: AppComponent[]; waves?: string[][]; change_sets?: ChangeSetRow[];
};
/** One row the API takes on PUT (the whole list). */
export type ComponentInput = { name: string; binding_id: string; path?: string; kind?: ComponentKind;
                               access?: "write" | "read"; depends_on?: string[]; ship_target?: ShipTarget | null };
export type Suggestion = ComponentInput & { repository?: string | null; language?: string | null; reason?: string | null };

/** The consent 409's `multirepo` (engine multirepo/grants.py consent_details). */
export type MultiRepoConsent = {
  app_key: string; app_name: string; run_label: string | null;
  repos: { full_name: string; access: "write" | "read"; components: string[]; words: string }[];
  unreachable: string[]; how: string; lasts: string; never: string;
};

// ── Change sets: one change across the app's repositories (engine multirepo/change_sets.py) ──

export type ChangeSetState = "planned" | "building" | "blocked" | "in_review" | "approved" | "merging" | "merged"
  | "deploying" | "live" | "rolled_back" | "failed" | "rejected" | "cancelled";
export type StepState = "done" | "active" | "waiting" | "failed";
export type StageKey = "plan" | "build" | "review" | "approve" | "deploy";
export type LaneState = "waiting" | "building" | "ready" | "failed" | "unchanged" | "merged" | "reverted";
export type Finding = { severity?: string | null; body: string; path?: string | null; line?: number | null;
                        author?: string | null; status?: string | null; stage?: string | null };
export type PlanItem = { name: string; change?: string | null; interface?: string | null; unchanged?: boolean | null };
export type Lane = {
  id: string; order: number; full_name: string; components: string[]; wave?: number | null; lane_state: LaneState | string;
  steps: { key: string; label: string; state: StepState | string }[];
  code_task_run_id?: string | null; phase?: string | null; pr_number?: number | null; pr_url?: string | null;
  branch?: string | null; base_branch?: string | null; head_sha?: string | null;
  additions?: number | null; deletions?: number | null; changed_files?: number | null;
  ci_state?: string | null; review_state?: string | null; findings: Finding[]; waiting_for: string[];
  error?: string | null; merged_sha?: string | null; revert_pr_number?: number | null; plan?: PlanItem[];
};
export type CrossCheck = {
  verdict: "pass" | "fail" | string; results: { name: string; detail?: string | null; pass: boolean; kind?: string | null }[];
  passed: number; total: number; at?: string | null;
};
export type ChangeSetGrant = {
  repos: { full_name: string; access: "write" | "read" | string }[]; consented_by?: string | null; remember?: boolean;
  expires_at?: string | null; ended_at?: string | null; token_lifetime_minutes?: number | null;
};
export type ChangeSetView = {
  id: string; project_id: string; app_key: string; app_name: string; title: string; mode: "multi" | "single" | string;
  state: ChangeSetState | string; blocked_reason?: string | null; error?: string | null;
  stage: { current: StageKey | null; steps: { key: StageKey | string; label: string; state: StepState | string }[] };
  lanes: Lane[];
  plan: { summary?: string | null; components?: PlanItem[]; by?: string | null; at?: string | null } | null;
  cross_check: CrossCheck | null; grant: ChangeSetGrant | null;
  decision: { id: string; status: string; approved_by?: string | null; approved_at?: string | null; created_at?: string | null } | null;
  approved_shas?: Record<string, string> | null; approved_by?: string | null; approved_at?: string | null;
  release?: Record<string, unknown> | null; cost_usd: number; minutes: number;
  created_at?: string | null; updated_at?: string | null;
};
export type DeployWave = {
  order: number; lane_id: string; components: string[]; repository: string; kind: "host" | "merge" | string;
  ship?: ShipTarget | null; from?: string | null; to?: string | null; status: string; deployment_run_id?: string | null;
  error?: string | null; target_id?: string | null; host?: string | null;
  steps: { icon: "deploy" | "health" | "check" | string; text: string }[]; rollback: string;
};
export type DeployPlan = {
  change_set_id: string; title: string; app_name: string; app_key: string; state: string; status: string;
  environment?: string | null; host?: string | null; ready: boolean; problems: string[]; waves: DeployWave[];
  rollback: { order: number; repository: string; components: string[]; text: string; status: string }[];
  auto_rollback?: string | null; approved_by?: string | null; started_at?: string | null; finished_at?: string | null;
};
export type ChangeSetGrantView = { grant: ChangeSetGrant | null; consent: unknown };

/** The view, with every list present (an older or partial answer reads as empty lists). */
export function asChangeSet(v: Record<string, unknown>): ChangeSetView | null {
  if (typeof v.id !== "string" || !v.id || !Array.isArray(v.lanes)) return null;
  const lanes = (v.lanes as Lane[]).map((l) => ({
    ...l, steps: Array.isArray(l.steps) ? l.steps : [], findings: Array.isArray(l.findings) ? l.findings : [],
    waiting_for: Array.isArray(l.waiting_for) ? l.waiting_for : [], components: Array.isArray(l.components) ? l.components : [],
  }));
  const stage = v.stage && typeof v.stage === "object" ? v.stage as ChangeSetView["stage"] : { current: null, steps: [] };
  return {
    ...(v as unknown as ChangeSetView), lanes,
    stage: { current: stage.current ?? null, steps: Array.isArray(stage.steps) ? stage.steps : [] },
    plan: v.plan && typeof v.plan === "object" ? v.plan as ChangeSetView["plan"] : null,
    cross_check: v.cross_check && typeof v.cross_check === "object"
      ? { ...(v.cross_check as CrossCheck), results: Array.isArray((v.cross_check as CrossCheck).results) ? (v.cross_check as CrossCheck).results : [] }
      : null,
    cost_usd: typeof v.cost_usd === "number" ? v.cost_usd : 0, minutes: typeof v.minutes === "number" ? v.minutes : 0,
  };
}
const asPlan = (v: Record<string, unknown>): DeployPlan | null =>
  (Array.isArray(v.waves) ? { ...(v as unknown as DeployPlan), problems: Array.isArray(v.problems) ? v.problems as string[] : [],
                              rollback: Array.isArray(v.rollback) ? v.rollback as DeployPlan["rollback"] : [],
                              ready: v.ready !== false } : null);

// ── Calls ─────────────────────────────────────────────────────────────────────────────────────

const q = (projectId: string) => `project_id=${enc(projectId)}`;

/** An answer that is not the shape asked for (an older API, a proxy page) reads as "not here". */
async function shaped<T>(p: Promise<unknown>, fix: (v: Record<string, unknown>) => T | null): Promise<T> {
  const v = await p;
  const out = v && typeof v === "object" && !Array.isArray(v) ? fix(v as Record<string, unknown>) : null;
  if (out === null) throw new IdentityError(404, "not_found");
  return out;
}
const asRegistry = (v: Record<string, unknown>): Registry | null =>
  (Array.isArray(v.items) ? (v as unknown as Registry) : null);
const asProjectRepos = (v: Record<string, unknown>): ProjectRepos | null =>
  (Array.isArray(v.items) ? { ...(v as unknown as ProjectRepos), addable: Array.isArray(v.addable) ? v.addable as ProjectRepos["addable"] : [] } : null);
const asComponents = (v: Record<string, unknown>): AppComponents | null =>
  (Array.isArray(v.components) ? { ...(v as unknown as AppComponents),
                                   deploy_config: Array.isArray(v.deploy_config) ? v.deploy_config as AppComponent[] : [] } : null);

export const multirepo = {
  registry: (projectId: string) => shaped(idGet(`/v1/github/repos?${q(projectId)}`), asRegistry),
  refresh: (projectId: string) => shaped(idPost("/v1/github/repos/refresh", { project_id: projectId }), asRegistry),
  setAllowed: (projectId: string, id: string, allowed: boolean) =>
    idPatch<RegistryRepo>(`/v1/github/repos/${enc(id)}`, { project_id: projectId, allowed }),
  saveAllowed: (projectId: string, allowedIds: string[]) =>
    shaped(idPut("/v1/github/repos/allowed", { project_id: projectId, allowed_ids: allowedIds }), asRegistry),
  recheckAll: (projectId: string) => shaped(idPost("/v1/github/repos/recheck", { project_id: projectId }), asRegistry),
  check: (projectId: string, id: string) =>
    idPost<RegistryRepo>(`/v1/github/repos/${enc(id)}/check`, { project_id: projectId }),

  projectRepos: (projectId: string) => shaped(idGet(`/v1/projects/${enc(projectId)}/repos`), asProjectRepos),
  putProjectRepo: (projectId: string, id: string, body: { role_hint?: RoleHint | null; base_branch?: string }) =>
    shaped(idPut(`/v1/projects/${enc(projectId)}/repos/${enc(id)}`, body), asProjectRepos),
  removeProjectRepo: (projectId: string, id: string) =>
    shaped(idDelete(`/v1/projects/${enc(projectId)}/repos/${enc(id)}`), asProjectRepos),
  makeDefaultRepo: async (projectId: string, bindingId: string) => {
    await idPost(`/v1/setup/repository/${enc(bindingId)}/default?project_id=${enc(projectId)}`, {});
    return shaped(idGet(`/v1/projects/${enc(projectId)}/repos`), asProjectRepos);
  },
  setRepoMode: (projectId: string, mode: "single" | "multi") =>
    shaped(idPut(`/v1/projects/${enc(projectId)}/repo-mode`, { repo_mode: mode }), asProjectRepos),

  components: (projectId: string, appKey: string) =>
    shaped(idGet(`/v1/apps/${enc(appKey)}/components?${q(projectId)}`), asComponents),
  /** The whole list. Its answer is not re-checked: a write that succeeded is not "not here". */
  putComponents: (projectId: string, appKey: string, components: ComponentInput[], appName?: string) =>
    idPut<AppComponents>(`/v1/apps/${enc(appKey)}/components`,
                         { project_id: projectId, ...(appName ? { app_name: appName } : {}), components }),
  suggest: (projectId: string, bindingIds: string[]) =>
    idPost<{ components: Suggestion[] }>(`/v1/projects/${enc(projectId)}/suggest-components`, { binding_ids: bindingIds }),
  consent: (projectId: string, appKey: string) =>
    idGet<MultiRepoConsent>(`/v1/apps/${enc(appKey)}/consent?${q(projectId)}`),

  changeSet: (projectId: string, id: string) => shaped(idGet(`/v1/change-sets/${enc(id)}?${q(projectId)}`), asChangeSet),
  /** The change a build belongs to; 404 `no_change_set` for an ordinary build. */
  changeSetOfRun: (projectId: string, runId: string) =>
    shaped(idGet(`/v1/runs/${enc(runId)}/change-set?${q(projectId)}`), asChangeSet),
  changeSets: (projectId: string, states: string[]) =>
    idGet<{ items?: unknown[] }>(`/v1/projects/${enc(projectId)}/change-sets?${q(projectId)}&state=${enc(states.join(","))}&mode=multi`)
      .then((v) => (Array.isArray(v?.items) ? v.items : []).flatMap((x) => {
        const cs = x && typeof x === "object" ? asChangeSet(x as Record<string, unknown>) : null;
        return cs ? [cs] : [];
      })),
  grant: (projectId: string, id: string) => idGet<ChangeSetGrantView>(`/v1/change-sets/${enc(id)}/grant?${q(projectId)}`),
  crossCheck: (projectId: string, id: string) =>
    shaped(idPost(`/v1/change-sets/${enc(id)}/cross-check`, { project_id: projectId }), asChangeSet),
  stop: (projectId: string, id: string) => shaped(idPost(`/v1/change-sets/${enc(id)}/stop`, { project_id: projectId }), asChangeSet),
  deployPlan: (projectId: string, id: string) => shaped(idGet(`/v1/change-sets/${enc(id)}/deploy-plan?${q(projectId)}`), asPlan),
  deploy: (projectId: string, id: string) => shaped(idPost(`/v1/change-sets/${enc(id)}/deploy`, { project_id: projectId }), asPlan),
  rollback: (projectId: string, id: string) => shaped(idPost(`/v1/change-sets/${enc(id)}/rollback`, { project_id: projectId }), asPlan),
  /** "New change": 201 the view; 409 consent_required (with `multirepo`) when no grant covers it. */
  startChangeSet: (projectId: string, appKey: string, body: { title: string; instructions?: string; remember?: boolean }) =>
    shaped(idPost(`/v1/apps/${enc(appKey)}/change-sets`, { project_id: projectId, ...body }), asChangeSet),
};

/** The API's own sentence for a refusal ({error, message}), else the identity words. */
export function refusalMessage(e: unknown): string {
  if (e instanceof IdentityError) {
    const m = e.body?.message;
    if (typeof m === "string" && m.trim()) return m;
    if (e.detail) return e.detail;
    return e.message;
  }
  return e instanceof Error && e.message ? e.message : "That didn't work";
}

/** The refusal's error code, when the API named one (`in_use`, `apps_use_several`, …). */
export function refusalCode(e: unknown): string {
  return e instanceof IdentityError && typeof e.body?.error === "string" ? e.body.error : "";
}

// ── Rules the screens share ───────────────────────────────────────────────────────────────────

/** Registry filters: All / Allowed / Needs a fix / Not allowed. Needs a fix is what the server
 *  counts as one (registry.py: needs_fix and read_only). */
export type RegistryFilter = "all" | "allowed" | "needs_fix" | "not_allowed";
export function matchesFilter(r: RegistryRepo, f: RegistryFilter): boolean {
  if (f === "allowed") return r.allowed;
  if (f === "needs_fix") return r.state === "needs_fix" || r.state === "read_only";
  if (f === "not_allowed") return !r.allowed;
  return true;
}
export function registryCounts(reg: Registry): Record<RegistryFilter, number> {
  const c = reg.counts ?? {};
  const n = (f: RegistryFilter) => reg.items.filter((r) => matchesFilter(r, f)).length;
  return { all: c.all ?? reg.items.length, allowed: c.allowed ?? n("allowed"),
           needs_fix: c.needs_fix ?? n("needs_fix"), not_allowed: c.not_allowed ?? n("not_allowed") };
}

export const STATE_WORDS: Record<string, { label: string; tone: "ok" | "warn" | "bad" | "muted" | "info"; tip: string }> = {
  working: { label: "Working", tone: "ok", tip: "Read, write and pull requests all pass." },
  read_only: { label: "Read only", tone: "warn", tip: "BOSS can read it but cannot push or open pull requests." },
  needs_fix: { label: "Needs a fix", tone: "bad", tip: "A check failed. Fix the App's access in GitHub, then recheck." },
  checking: { label: "Checking", tone: "info", tip: "Asking GitHub now." },
  not_allowed: { label: "Not allowed", tone: "muted", tip: "Not ticked: no project can use it." },
  archived: { label: "Archived", tone: "muted", tip: "Archived on GitHub. It cannot be allowed." },
  unreachable: { label: "Unreachable", tone: "bad", tip: "The GitHub App no longer reaches it." },
};
export function stateWords(state?: string | null) {
  return STATE_WORDS[state ?? ""] ?? { label: state ? state.replace(/_/g, " ") : "—", tone: "muted" as const, tip: "" };
}

/** A language's dot colour (GitHub's palette), muted when unknown. */
const LANG_COLORS: Record<string, string> = {
  typescript: "#3178c6", javascript: "#f1e05a", go: "#00add8", python: "#3572a5", protobuf: "#a78bfa",
  hcl: "#844fba", kotlin: "#a97bff", ruby: "#cc342d", rust: "#dea584", java: "#b07219", swift: "#f05138",
  markdown: "#7f90b0", shell: "#89e051", "c#": "#178600", php: "#4f5d95", html: "#e34c26", css: "#563d7c",
};
export function langColor(lang?: string | null): string {
  return LANG_COLORS[(lang ?? "").toLowerCase()] ?? "var(--faint)";
}

/** "2 min ago", "3 h ago", "4 d ago" — or "" when unknown. */
export function agoWords(iso: string | null | undefined, now: number | null): string {
  if (!iso || now === null) return "";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  const s = Math.max(0, Math.floor((now - t) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

/**
 * Build order: each code component's place (1 = builds first), a topological sort of
 * `depends_on` that keeps the list's own order among equals. Names in a cycle (refused by the API
 * on save) or unknown get no number. Deploy config is not built, so it has no number.
 */
export function buildOrder(rows: { name: string; kind?: ComponentKind; depends_on?: string[] }[]): Map<string, number> {
  const code = rows.filter((r) => (r.kind ?? "code") === "code" && r.name.trim());
  const names = new Set(code.map((r) => r.name.trim()));
  const placed = new Map<string, number>();
  let n = 0;
  let progress = true;
  while (progress && placed.size < code.length) {
    progress = false;
    for (const r of code) {
      const name = r.name.trim();
      if (placed.has(name)) continue;
      const deps = (r.depends_on ?? []).filter((d) => names.has(d));
      if (deps.every((d) => placed.has(d))) { placed.set(name, ++n); progress = true; }
    }
  }
  return placed;
}

/** A component's "Ships to", in words. */
export function shipsTo(t?: ShipTarget | null): string {
  if (!t || t.kind === "none") return "—";
  if (t.kind === "package") return [t.label, t.package].filter(Boolean).join(" ") || "A package";
  return t.label || t.url?.replace(/^https?:\/\//, "") || "A host";
}

/** "a, b and c". */
export function listWords(items: string[]): string {
  if (items.length <= 1) return items[0] ?? "";
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

/** The short name of a repository (`acme/web` → `web`). */
export const shortName = (fullName: string) => fullName.split("/").pop() ?? fullName;

const str = (v: unknown): string => (typeof v === "string" ? v : "");

/** The consent 409's `multirepo`, or null when absent or not that shape. */
export function multirepoConsentFrom(raw: unknown): MultiRepoConsent | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const b = raw as Record<string, unknown>;
  const repos = (Array.isArray(b.repos) ? b.repos : []).flatMap((r) => {
    if (!r || typeof r !== "object") return [];
    const x = r as Record<string, unknown>;
    const full = str(x.full_name);
    if (!full) return [];
    return [{ full_name: full, access: x.access === "read" ? "read" as const : "write" as const,
              components: Array.isArray(x.components) ? x.components.filter((c): c is string => typeof c === "string") : [],
              words: str(x.words) }];
  });
  if (!repos.length) return null;
  return {
    app_key: str(b.app_key), app_name: str(b.app_name) || str(b.app_key) || "this app",
    run_label: str(b.run_label) || null, repos,
    unreachable: Array.isArray(b.unreachable) ? b.unreachable.filter((u): u is string => typeof u === "string") : [],
    how: str(b.how), lasts: str(b.lasts), never: str(b.never),
  };
}

/** Where each multi-repo screen lives in the console (read with app/factory/cockpit-util areaHref). */
export const MULTIREPO_PLACES = {
  registry: { area: "setup", focus: "repositories" },
  projectRepos: { area: "code_tasks", view: "repositories" },
  components: (appKey: string) => ({ area: "code_tasks" as const, view: "components", app: appKey }),
} as const;

/** A change set's state in words, with its tone (Recent changes, the releases list, the cockpit). */
export const CHANGE_WORDS: Record<string, [string, "ok" | "warn" | "bad" | "muted" | "info"]> = {
  planned: ["Planned", "muted"], building: ["Building", "info"], blocked: ["Blocked", "bad"], in_review: ["In review", "info"],
  approved: ["Approved", "ok"], merging: ["Merging", "info"], merged: ["Merged", "ok"], deploying: ["Deploying", "info"],
  live: ["Live", "ok"], rolled_back: ["Rolled back", "warn"], failed: ["Failed", "bad"], rejected: ["Rejected", "muted"],
  cancelled: ["Stopped", "muted"],
};
export function changeWord(state?: string | null): string {
  return CHANGE_WORDS[state ?? ""]?.[0] ?? (state ? state.replace(/_/g, " ") : "—");
}
export function changeTone(state?: string | null) {
  return CHANGE_WORDS[state ?? ""]?.[1] ?? "muted";
}
/** A change that is still moving (polled; Stop offered while it builds or waits). */
export const CHANGE_MOVING = new Set(["planned", "building", "blocked", "in_review", "approved", "merging", "deploying"]);
export const CHANGE_STOPPABLE = new Set(["planned", "building", "blocked", "in_review"]);

/** "+412 −58 · 11 files". */
export function diffWords(l: { additions?: number | null; deletions?: number | null; changed_files?: number | null }): string {
  const parts: string[] = [];
  if (l.additions != null || l.deletions != null) parts.push(`+${l.additions ?? 0} −${l.deletions ?? 0}`);
  if (l.changed_files != null) parts.push(`${l.changed_files} ${l.changed_files === 1 ? "file" : "files"}`);
  return parts.join(" · ");
}

/** A finding still holds the set when it is high/critical and not fixed or dismissed. */
export function isBlocking(f: Finding): boolean {
  const sev = (f.severity ?? "").toLowerCase();
  const open = !["fixed", "addressed", "dismissed", "resolved"].includes((f.status ?? "").toLowerCase());
  return open && (sev === "high" || sev === "critical" || sev === "blocker");
}
export function isFixed(f: Finding): boolean {
  return ["fixed", "addressed", "resolved"].includes((f.status ?? "").toLowerCase());
}

/** Where a change set lives in the console. */
export const CHANGE_SET_PLACES = {
  cockpit: (id: string, tab?: "prs") => ({ area: "code_tasks" as const, changeset: id, ...(tab ? { tab } : {}) }),
  deployPlan: (id: string) => ({ area: "infra" as const, tab: "deploys", changeset: id }),
} as const;
/** A finding still to address: a real issue (not a pass or a note) not fixed or dismissed. */
export function isOpenIssue(f: Finding): boolean {
  const sev = (f.severity ?? "").toLowerCase();
  if (["pass", "note", "info", ""].includes(sev)) return false;
  return !isFixed(f) && (f.status ?? "").toLowerCase() !== "dismissed";
}
