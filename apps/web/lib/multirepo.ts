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
};
export type ChangeSetRow = { id: string; title?: string | null; state?: string | null; repos?: string[];
                             prs?: number | null; created_at?: string | null };
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
