/**
 * START A PROJECT (BOSSAPP-810): the console's New project flow — Try a sample, Import a repo,
 * New app from an idea, or a preset — and the project page's "What should we add?".
 *
 * Reads: GET /v1/projects/{id}/start (the project page) and GET /v1/github/repos/{id}/facts
 * (Import › BOSS reads it), both apps/api/app/routers/start_project.py. Every write is a route
 * that already exists: POST /v1/projects, PUT /v1/projects/{id}/repos/{repo} (lib/multirepo),
 * POST /v1/templates/{id}/apply (the sample) and POST /v1/delivery-workflows/start (Start).
 * Calls go same-origin through app/v1/[...path] (lib/identity/api); writes are wrapped by the
 * caller in useGuardedCall so a step-up is asked for.
 */
import { IDEAS } from "../app/ask-boss-hero";
import { buildBrief } from "../app/template-details";
import { enc, idGet, idPost, IdentityError } from "./identity/api";
import type { Registry, RegistryRepo } from "./multirepo";

// ── The URL: /?new_project=sample|import|idea (or 1) opens the flow at that choice ─────────────

/** `form` is the name-and-description form (both project types; a knowledge graph or a blank
 *  project), reached from the idea step. */
export type StartStep = "choose" | "sample" | "import" | "idea" | "form";
export const NEW_PROJECT_PARAM = "new_project";

/** The step a `?new_project=` value opens on, or null when the URL does not ask for the flow. */
export function stepFromParam(value: string | null | undefined): StartStep | null {
  if (value === null || value === undefined) return null;
  const v = value.trim().toLowerCase();
  if (v === "sample" || v === "import" || v === "idea") return v;
  return "choose";
}

/** A link that opens the flow at a choice (the Welcome screen links to these). */
export function newProjectHref(step: "sample" | "import" | "idea" | "choose" = "choose"): string {
  return `/?${NEW_PROJECT_PARAM}=${step === "choose" ? "1" : step}`;
}

/** Leave the URL as it was before the flow was opened. */
export function clearStepParam(): void {
  try {
    const url = new URL(window.location.href);
    if (!url.searchParams.has(NEW_PROJECT_PARAM)) return;
    url.searchParams.delete(NEW_PROJECT_PARAM);
    window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
  } catch { /* non-browser */ }
}

// ── The three choices and the presets ─────────────────────────────────────────────────────────

export const CHOICES = [
  { key: "sample", time: "8 min", title: "Try a sample",
    line: "Watch BOSS build a Hello World app from start to finish.", cta: "Start", highlight: true },
  { key: "import", time: "2 min", title: "Import a repo",
    line: "Bring an app you already have, then add features to it.", cta: "Choose repo", highlight: false },
  { key: "idea", time: "1 min", title: "New app from an idea",
    line: "Describe it in a sentence. BOSS plans it, you approve, it builds.", cta: "Describe it", highlight: false },
] as const;

export type Preset =
  | { key: string; kind: "app"; title: string; label: string; brief: string }
  | { key: string; kind: "knowledge_graph"; title: string; label: string; tab: "documents" };

/** The banking knowledge-graph presets: a knowledge-graph project, opened on its Documents tab,
 *  where each one's sample loads (AML alerts, the regulation trace, the DQ rulebook). */
export const KG_PRESETS: Preset[] = [
  { key: "aml", kind: "knowledge_graph", title: "AML alert investigation", label: "Banking, knowledge graph", tab: "documents" },
  { key: "regulatory", kind: "knowledge_graph", title: "Regulatory change", label: "Banking, knowledge graph", tab: "documents" },
  { key: "dq", kind: "knowledge_graph", title: "Data quality RCA", label: "Banking, knowledge graph", tab: "documents" },
];

export function slugOf(title: string): string {
  return title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

/** Every preset: the sixteen written app briefs (the Ask BOSS gallery's) and the banking ones. */
export function allPresets(): Preset[] {
  const apps: Preset[] = IDEAS.map((i) => ({
    key: slugOf(i.title), kind: "app", title: i.title, label: "Web app",
    brief: buildBrief(i.title, i.brief),
  }));
  return [...apps, ...KG_PRESETS];
}

/** The grid's first row: three app briefs, then the banking presets — seven, and "See all". */
export function shownPresets(all: Preset[] = allPresets()): Preset[] {
  const pick = ["bug-triage-board", "incident-timeline-builder", "decision-log", "change-calendar"];
  const apps = pick.map((k) => all.find((p) => p.key === k)).filter((p): p is Preset => !!p);
  return [...apps, ...all.filter((p) => p.kind === "knowledge_graph")];
}

/** A project name from a one-sentence idea: its first few words, without a leading verb or
 *  article ("Build a shift-swap board for…" → "Shift-swap board"). Editable before Create. */
export function nameFromIdea(sentence: string): string {
  const words = sentence.trim().replace(/[.!?].*$/s, "")
    .replace(/^(please\s+)?(build|make|create|write|add)\s+/i, "")
    .replace(/^(a|an|the|my|our)\s+/i, "")
    .split(/\s+/).filter(Boolean);
  const cut = words.findIndex((w, i) => i > 0 && /^(for|that|where|which|to|with|so|who|from|in|on|of)$/i.test(w));
  const name = (cut > 0 ? words.slice(0, cut) : words).slice(0, 5).join(" ");
  return name ? (name[0].toUpperCase() + name.slice(1)).slice(0, 60) : "";
}

// ── Import: accounts, repos, facts ───────────────────────────────────────────────────────────

/** The GitHub accounts the org's registry reaches (each repo's owner), the connection's first. */
export function accountsOf(registry: Registry | null): string[] {
  if (!registry) return [];
  const owners = registry.items.map((r) => r.full_name.split("/")[0]).filter(Boolean);
  const first = registry.connection?.account_login ?? null;
  const all = [...new Set([...(first ? [first] : []), ...owners])];
  return all.filter((a) => owners.includes(a) || a === first);
}

/** The repositories BOSS may import from one account: allowed, reachable, not archived. */
export function importable(registry: Registry | null, account: string): RegistryRepo[] {
  return (registry?.items ?? []).filter((r) => r.full_name.split("/")[0] === account && r.allowed
    && !r.archived && r.reachable !== false).sort((a, b) => a.full_name.localeCompare(b.full_name));
}

export type RepoFacts = {
  repo_id: string; full_name: string; branch: string; description?: string | null; private?: boolean | null;
  facts: { language: string | null; framework: string | null; build: string | null; tests: boolean | null;
           runs_as: string | null };
  read_from: "graph" | "github" | "given" | null;
  mapping: Mapping; in_project: boolean; suggested_name: string;
  /** How many files the repository has (Map code "0 / 214 files"). */
  files_total?: number | null;
};
export type Mapping = { state: "not_started" | "queued" | "running" | "done" | "failed"; percent: number; done: boolean;
                        error?: string | null; files?: number | null; parsed?: number | null };

/** The rows of "BOSS reads it", each said or left as "At first build". */
export function factRows(f: RepoFacts): { label: string; value: string; known: boolean }[] {
  const lang = [f.facts.language, f.facts.framework].filter(Boolean).join(" · ");
  const build = f.facts.build ? `${f.facts.build}${f.facts.tests ? " ✓" : ""}` : "";
  return [
    { label: "Language", value: lang || "At first build", known: !!lang },
    { label: "Build & test", value: build || "At first build", known: !!build },
    { label: "Runs as", value: f.facts.runs_as || "At first build", known: !!f.facts.runs_as },
    { label: "Branch", value: f.branch, known: true },
  ];
}

export function mappingWords(m: Mapping | null | undefined): string {
  if (!m) return "Mapping the code";
  if (m.done) return "Code mapped ✓";
  if (m.state === "running") return `Mapping the code · ${m.percent}%`;
  if (m.state === "failed") return "Mapping stopped";
  if (m.state === "queued") return "Mapping the code · starting";
  return "Mapping starts when you create it";
}

// ── The project page ─────────────────────────────────────────────────────────────────────────

export type WorkRow = {
  run_id: string; title: string; workflow: string; workflow_key?: string | null; status: string; state: string;
  tone: "amber" | "green" | "blue" | "red" | "grey";
  action: { kind: "approve" | "pr" | "watch" | "open"; label: string; url?: string | null };
  pull_request_url?: string | null; created_at?: string | null;
};
export type StartPlan = {
  mode: "feature" | "new_app"; work_class: string | null; workflow_key: string | null;
  workflow_name?: string | null; resolved_key?: string | null; minutes?: number | null;
  /** To the pull request and its preview (the steps after a person's merge are not counted). */
  minutes_to_pr?: number | null; minutes_low?: number | null; minutes_high?: number | null;
  cost?: { runs: number; low_usd: number; high_usd: number; metered?: boolean } | null;
};
export type StartView = {
  project_id: string; name: string; description?: string | null; project_type: string; archived?: boolean;
  repo: { full_name: string; branch: string; mapping: Mapping } | null;
  start: StartPlan; examples: string[];
  next: { key: string; label: string; who: string; you: boolean }[];
  work: WorkRow[];
  sample: { template: string; name: string; stack: string; workflow_key: string };
};

function dollars(n: number): string { return n < 1 ? `$${n.toFixed(2)}` : `$${Math.round(n)}`; }

/** "Estimate: ~25 min · ~$3 or included in subscription". */
export function estimateLine(plan: StartPlan | null | undefined): string {
  if (!plan) return "";
  const parts: string[] = [];
  if (plan.minutes_low && plan.minutes_high) parts.push(`${plan.minutes_low}–${plan.minutes_high} min to a pull request`);
  else if (plan.minutes) parts.push(`~${plan.minutes} min`);
  const c = plan.cost;
  if (c && c.runs > 0) {
    const mid = (c.low_usd + c.high_usd) / 2;
    parts.push(`~${dollars(mid)} or included in subscription`);
  } else {
    parts.push("included in subscription or pay per use");
  }
  return `Estimate: ${parts.join(" · ")}`;
}

/** The start door's body for Start on the project page (POST /v1/delivery-workflows/start). */
export function startBody(view: StartView, text: string): Record<string, unknown> {
  const directive = text.trim();
  const title = directive.split("\n")[0].replace(/[.!?]+$/, "").slice(0, 80) || view.name;
  const input: Record<string, unknown> = { directive, title };
  if (view.repo) input.repository = view.repo.full_name;
  return {
    project_id: view.project_id,
    ...(view.start.workflow_key ? { workflow_key: view.start.workflow_key } : { work_class: view.start.work_class ?? "project.new" }),
    input,
  };
}

/** Where a work row's action goes: the run, the decision, or the PR on GitHub. */
export function workHref(row: WorkRow, orgId?: string | null): string {
  if (row.action.kind === "pr" && row.action.url) return row.action.url;
  const p = new URLSearchParams();
  if (orgId) p.set("org", orgId);
  p.set("area", "delivery_workflows");
  p.set("wfr", row.run_id);
  return `/?${p.toString()}`;
}

// ── Calls ────────────────────────────────────────────────────────────────────────────────────

export type CreatedProject = { id: string; name: string; project_type?: string | null };

export const startProjectApi = {
  view: (projectId: string, signal?: AbortSignal) =>
    idGet<StartView>(`/v1/projects/${enc(projectId)}/start`, { signal }),
  facts: (projectId: string, repoId: string, signal?: AbortSignal) =>
    idGet<RepoFacts>(`/v1/github/repos/${enc(repoId)}/facts?project_id=${enc(projectId)}`, { signal }),
  /** POST /v1/projects. A name in use comes back as the existing project's id (`exists`). */
  create: async (name: string, opts: { description?: string; type?: "app" | "knowledge_graph" } = {}) => {
    try {
      return await idPost<CreatedProject>("/v1/projects", {
        name, ...(opts.description ? { description: opts.description } : {}),
        ...(opts.type === "knowledge_graph" ? { project_type: "knowledge_graph" } : {}),
      });
    } catch (e) {
      if (e instanceof IdentityError && e.status === 409) {
        const d = (e.body.detail ?? {}) as { error?: string; project_id?: string | null };
        if (d.error === "project_exists") throw Object.assign(new Error(`${name} exists`), { exists: d.project_id ?? null });
      }
      throw e;
    }
  },
};

/** Create a project under `name`, or `name 2`, `name 3`… when the name is taken (the sample and
 *  the presets: pressing Try a sample twice makes a second Hello World, never an error). */
export async function createFresh(name: string, opts: { description?: string; type?: "app" | "knowledge_graph" } = {},
                                  create = startProjectApi.create): Promise<CreatedProject> {
  for (let n = 1; n <= 20; n++) {
    try {
      return await create(n === 1 ? name : `${name} ${n}`, opts);
    } catch (e) {
      if ((e as { exists?: unknown }).exists === undefined) throw e;
    }
  }
  throw new Error(`Every name from ${name} to ${name} 20 is taken. Choose another.`);
}

/** Where a project opens (its page), keeping the org. */
export function projectPageHref(projectId: string, homeId: string, area: "project" | "overview" = "project",
                                tab: "onboarding" | null = null): string {
  const url = typeof window === "undefined" ? new URL("http://x/") : new URL(window.location.href);
  const p = new URLSearchParams(url.search);
  for (const k of [NEW_PROJECT_PARAM, "task", "run", "build", "role", "kg_tab", "kg_node", "kg_scan", "kg_sub", "wfr"]) p.delete(k);
  if (projectId === homeId) p.delete("project_id"); else p.set("project_id", projectId);
  // THE PROJECT PAGE (BOSSAPP-914): a new project opens where Helm's onboarding shows. From the
  // first run (`first_run=1`) it opens the guided first run beside it instead.
  if (p.get("first_run") === "1" && p.get("area") === "projects") { p.set("project_id", projectId); return `/?${p.toString()}`; }
  p.set("area", area);
  p.delete("tab"); p.delete("drawer");
  // A project made from a repository lands on Helm's onboarding (screen 10), which starts by itself.
  if (tab && area === "project") p.set("tab", tab);
  return `/?${p.toString()}`;
}
