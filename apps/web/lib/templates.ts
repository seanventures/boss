/**
 * App templates (APP_TEMPLATES_AND_SECURITY_LOOP.md §2): the shapes `/v1/templates*` and an app's
 * `/v1/apps/{key}/{templates,secrets,setup-guide}` answer, read defensively, the calls, and the
 * small rules the gallery, App ▸ Setup and Kit ▸ Templates share.
 *
 * Every call goes same-origin through app/v1/[...path] with the person's session (CSRF on every
 * write, refusals as IdentityError). Writes that need a fresh second factor (app_secret:write,
 * template:approve) are made through useGuardedCall by the screens; a start that needs the
 * person's consent goes through withConsent (the build consent sheet).
 *
 * SECRET VALUES ARE WRITE-ONLY (§2.4, S2.6): nothing here ever reads one back; the API answers
 * presence, mode and a shape verdict only.
 */
import { IdentityError, enc, idDelete, idGet, idPost, idPut } from "./identity/api";

type O = Record<string, unknown>;
const obj = (v: unknown): O => (v && typeof v === "object" && !Array.isArray(v) ? (v as O) : {});
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const str = (v: unknown): string => (typeof v === "string" ? v : v == null ? "" : String(v));
const strs = (v: unknown): string[] => arr(v).map(str).filter(Boolean);

export const STACKS = ["node", "python", "java"] as const;
export type Stack = (typeof STACKS)[number] | string;
export const STACK_LABEL: Record<string, string> = { node: "Node", python: "Python", java: "Java" };
export const stackLabel = (s: string): string => STACK_LABEL[s] ?? s;

export const INSECURE = "insecure_by_design";
export const LABEL_WORD: Record<string, string> = { [INSECURE]: "Insecure by design" };
export const labelWord = (l: string): string => LABEL_WORD[l] ?? l.replace(/_/g, " ");

export const STATUS_WORD: Record<string, string> = {
  active: "Approved", pending_review: "Needs review", rejected: "Rejected", hidden: "Hidden",
};

export type TemplateSecret = { name: string; purpose: string; testDefault: "none" | "generated" | "stub" | string };

export type TemplateCard = {
  id: string; key: string; version: string; source: "builtin" | "git" | string; status: string;
  name: string; summary: string; stacks: string[]; labels: string[]; adds: string[]; egress: string[];
  secrets: TemplateSecret[]; tested: string[]; digest: string; gitUrl: string; commitSha: string;
  importedBy: string; approvedBy: string; createdAt: string; approvedAt: string;
};

export type TrialResult = { status: string; at: string; error: string; stacks: Record<string, { ok: boolean; tests: number; passed: number; error: string }> };

export type TemplateDetail = TemplateCard & {
  brief: string; guide: string; securityNote: string;
  files: { path: string; size: number }[];
  validation: { ok: boolean; reasons: string[]; fileCount: number; totalBytes: number; rejected: { by: string; reason: string } | null };
  trial: TrialResult | null;
  versions: { id: string; version: string; status: string; createdAt: string }[];
  routes: string[]; requires: string[]; worksWith: string[]; frameworks: Record<string, string[]>;
};

function parseSecrets(v: unknown): TemplateSecret[] {
  return arr(v).map((x) => {
    const o = obj(x);
    return { name: str(o.name), purpose: str(o.purpose), testDefault: str(o.test_default ?? o.testDefault) || "none" };
  }).filter((s) => s.name);
}

export function parseCard(v: unknown): TemplateCard {
  const o = obj(v);
  return {
    id: str(o.id), key: str(o.template_key ?? o.key), version: str(o.version), source: str(o.source), status: str(o.status),
    name: str(o.name) || str(o.template_key), summary: str(o.summary), stacks: strs(o.stacks), labels: strs(o.labels),
    adds: strs(o.adds), egress: strs(o.egress), secrets: parseSecrets(o.secrets), tested: strs(o.tested),
    digest: str(o.digest), gitUrl: str(o.git_url), commitSha: str(o.commit_sha), importedBy: str(o.imported_by),
    approvedBy: str(o.approved_by), createdAt: str(o.created_at), approvedAt: str(o.approved_at),
  };
}

export function parseTrial(v: unknown): TrialResult | null {
  if (!v || typeof v !== "object") return null;
  const o = obj(v);
  const stacks: TrialResult["stacks"] = {};
  for (const [k, r] of Object.entries(obj(o.stacks))) {
    const x = obj(r);
    stacks[k] = { ok: x.ok === true, tests: Number(x.tests) || 0, passed: Number(x.passed) || 0, error: str(x.error) };
  }
  return { status: str(o.status), at: str(o.at), error: str(o.error), stacks };
}

export function parseDetail(v: unknown): TemplateDetail {
  const o = obj(v);
  const m = obj(o.manifest);
  const val = obj(o.validation);
  const rej = val.rejected ? obj(val.rejected) : null;
  const frameworks: Record<string, string[]> = {};
  for (const [k, s] of Object.entries(obj(m.stacks))) frameworks[k] = strs(obj(s).frameworks);
  return {
    ...parseCard(v),
    brief: str(o.brief), guide: str(o.guide), securityNote: str(o.security_note),
    files: arr(o.files).map((f) => ({ path: str(obj(f).path), size: Number(obj(f).size) || 0 })),
    validation: { ok: val.ok === true, reasons: strs(val.reasons), fileCount: Number(val.file_count) || 0,
                  totalBytes: Number(val.total_bytes) || 0, rejected: rej ? { by: str(rej.by), reason: str(rej.reason) } : null },
    trial: parseTrial(o.trial),
    versions: arr(o.versions).map((x) => ({ id: str(obj(x).id), version: str(obj(x).version), status: str(obj(x).status), createdAt: str(obj(x).created_at) })),
    routes: strs(m.routes), requires: strs(m.requires), worksWith: strs(m.works_with), frameworks,
  };
}

// ---- an app's side --------------------------------------------------------------------------------

export type SecretBinding = { name: string; env: string; mode: string; shape: string; setAt: string; lastUsedAt: string; liveApproved: boolean | null };
export type DeclaredSecret = { name: string; purpose: string; testDefault: string; bound: boolean };
export type AppSecrets = { bindings: SecretBinding[]; declared: DeclaredSecret[] };
export type Applied = { id: string; templateId: string; key: string; name: string; version: string; stack: string; status: string; workflowRunId: string; codeTaskRunId: string; createdAt: string };
export type Guide = { key: string; name: string; version: string; status: string; guide: string; copy: Record<string, string> };

export function parseSecretsView(v: unknown): AppSecrets {
  const o = obj(v);
  return {
    bindings: arr(o.secrets).map((x) => {
      const b = obj(x);
      return { name: str(b.name), env: str(b.env), mode: str(b.mode), shape: str(b.shape) || "unknown",
               setAt: str(b.set_at), lastUsedAt: str(b.last_used_at),
               liveApproved: typeof b.live_approved === "boolean" ? b.live_approved : null };
    }),
    declared: arr(o.declared).map((x) => {
      const d = obj(x);
      return { name: str(d.name), purpose: str(d.purpose), testDefault: str(d.test_default) || "none", bound: d.bound === true };
    }),
  };
}

export function parseApplied(v: unknown): Applied[] {
  return arr(obj(v).templates).map((x) => {
    const a = obj(x);
    return { id: str(a.id), templateId: str(a.template_id), key: str(a.template_key), name: str(a.name) || str(a.template_key),
             version: str(a.version), stack: str(a.stack), status: str(a.status), workflowRunId: str(a.workflow_run_id),
             codeTaskRunId: str(a.code_task_run_id), createdAt: str(a.created_at) };
  });
}

export function parseGuides(v: unknown): Guide[] {
  return arr(obj(v).guides).map((x) => {
    const g = obj(x);
    const copy: Record<string, string> = {};
    for (const [k, val] of Object.entries(obj(g.copy))) if (typeof val === "string") copy[k] = val;
    return { key: str(g.template_key), name: str(g.name) || str(g.template_key), version: str(g.version),
             status: str(g.status), guide: str(g.guide), copy };
  });
}

/** An applied template's state, in words. */
export const APPLIED_WORD: Record<string, string> = {
  building: "Building", pr_open: "PR open", applied: "Applied", failed: "Failed", abandoned: "Abandoned",
};

/** The three environments a secret is bound for, as the person reads them. */
export const ENVS = ["build", "preview", "deploy"] as const;
export const ENV_WORD: Record<string, string> = { build: "Build", preview: "Preview", deploy: "Deploy" };

/**
 * One secret's state in one environment, in the gallery's and Setup's words:
 * `Test mode` (BOSS provides it: generated or the template's stub), `Set` (a test value is
 * stored), `Live` (a live key, used by deploys once approved), `Needs a key`.
 */
export function secretWord(o: { testDefault: string; binding?: SecretBinding | null }): { word: string; tone: "ok" | "warn" | "live"; tip: string } {
  const b = o.binding;
  if (b?.mode === "live") {
    return b.liveApproved
      ? { word: "Live", tone: "live", tip: "A live key: deploys use it (approved)" }
      : { word: "Live · awaiting approval", tone: "warn", tip: "A live key: no deploy uses it until a person approves the decision" };
  }
  if (b?.mode === "test") return { word: "Set", tone: "ok", tip: "A test value is stored in the vault. It is never shown again." };
  if (b?.mode === "generated") return { word: "Test mode", tone: "ok", tip: "BOSS generated a random test value" };
  if (b?.mode === "stub") return { word: "Test mode", tone: "ok", tip: "The template's test double stands in for the provider" };
  if (o.testDefault === "generated" || o.testDefault === "stub") {
    return { word: "Test mode", tone: "ok", tip: o.testDefault === "generated" ? "BOSS generates a test value when it starts" : "The template's test double stands in for the provider" };
  }
  return { word: "Needs a key", tone: "warn", tip: "Paste a test key: a build needs one" };
}

/** The tested stacks, as the card says them: "Tested ✓ java, node". */
export function testedWord(tested: string[]): string {
  return tested.length ? `Tested ✓ ${tested.map(stackLabel).join(", ")}` : "";
}

/** The trial's own words. */
export function trialWord(t: TrialResult | null): string {
  if (!t) return "Not trialled";
  if (t.status === "passed") return `Trial passed · ${Object.keys(t.stacks).map(stackLabel).join(", ")}`;
  if (t.status === "not_run") return `Trial did not run${t.error ? `: ${t.error}` : ""}`;
  const bad = Object.entries(t.stacks).filter(([, r]) => !r.ok).map(([s]) => stackLabel(s));
  return `Trial failed${bad.length ? ` · ${bad.join(", ")}` : ""}`;
}

/**
 * A refusal in plain words: the API's own sentence (`message`), never a code, never a spec
 * reference like "(D3)" or "(§2.4)", never a traceback's internal path.
 */
export function refusalWords(e: unknown): string {
  if (e instanceof IdentityError) {
    const b = e.body;
    const said = typeof b.message === "string" ? b.message
      : typeof b.detail === "string" ? b.detail
      : (b.detail && typeof b.detail === "object" && typeof (b.detail as O).message === "string") ? String((b.detail as O).message) : "";
    return said ? plainSentence(said) : e.message;
  }
  return e instanceof Error ? plainSentence(e.message) : String(e);
}

/** Drop spec references ("(D3)", "(§2.4)") and capitalise: the person never needs the spec. */
export function plainSentence(s: string): string {
  const t = s.replace(/\s*\((?:D\d+|§[\d.]+[^)]*)\)/g, "")
    .replace(/\bpending_review\b/g, "waiting for review").replace(/\s+/g, " ").trim();
  return t ? t[0].toUpperCase() + t.slice(1) : t;
}

/** The code of a refusal (`secrets_needed`, `stack_mismatch` …), or "". */
export function refusalCode(e: unknown): string {
  return e instanceof IdentityError ? String(e.body.error ?? e.code ?? "") : "";
}

// ---- calls -----------------------------------------------------------------------------------------

const q = (o: Record<string, string | undefined | null>) =>
  Object.entries(o).filter(([, v]) => v).map(([k, v]) => `${k}=${encodeURIComponent(v as string)}`).join("&");

export type ApplyBody = {
  project_id: string; stack: string; app_key?: string; extends_run_id?: string; new_app?: { name: string };
  framework?: string; note?: string; accept_egress?: boolean | string[]; start_anyway?: string[]; dedupe_key?: string;
};
export type Applied201 = { workflow_run_id: string | null; application_id: string; app_key: string;
  project_id?: string; lab?: { project_id: string; name: string; why?: string } };

export const templatesApi = {
  list: async (filter: { stack?: string; status?: string } = {}) =>
    arr(obj(await idGet(`/v1/templates${filter.stack || filter.status ? `?${q(filter)}` : ""}`)).templates).map(parseCard),
  describe: async (id: string) => parseDetail(await idGet(`/v1/templates/${enc(id)}`)),
  importFromGit: (gitUrl: string, ref: string, acknowledgeInsecure = false) =>
    idPost<O>("/v1/templates/import", { git_url: gitUrl, ...(ref.trim() ? { ref: ref.trim() } : {}),
                                       ...(acknowledgeInsecure ? { acknowledge_insecure: true } : {}) }),
  approve: (id: string) => idPost<O>(`/v1/templates/${enc(id)}/approve`, {}),
  reject: (id: string, reason: string) => idPost<O>(`/v1/templates/${enc(id)}/reject`, { reason }),
  trial: async (id: string) => parseTrial(obj(await idPost<O>(`/v1/templates/${enc(id)}/trial`, {})).trial),
  hide: (id: string) => idDelete<O>(`/v1/templates/${enc(id)}?hide=true`),
  remove: (id: string) => idDelete<O>(`/v1/templates/${enc(id)}`),
  apply: (id: string, body: ApplyBody) => idPost<Applied201>(`/v1/templates/${enc(id)}/apply`, body),
  /** The lab projects this project's insecure-template apps are built in (each its own project). */
  labs: async (projectId: string) =>
    arr(obj(await idGet(`/v1/templates/labs?${q({ project_id: projectId })}`)).labs).map((l) => {
      const o = obj(l);
      return { projectId: String(o.project_id ?? ""), name: String(o.name ?? ""),
               appName: o.app_name == null ? null : String(o.app_name) };
    }).filter((l) => l.projectId),
  // An app's side. `appKey` is the app's key (the factory row's `app_key`).
  applied: async (projectId: string, appKey: string) =>
    parseApplied(await idGet(`/v1/apps/${enc(appKey)}/templates?${q({ project_id: projectId })}`)),
  secrets: async (projectId: string, appKey: string) =>
    parseSecretsView(await idGet(`/v1/apps/${enc(appKey)}/secrets?${q({ project_id: projectId })}`)),
  guides: async (projectId: string, appKey: string) =>
    parseGuides(await idGet(`/v1/apps/${enc(appKey)}/setup-guide?${q({ project_id: projectId })}`)),
  putSecret: (projectId: string, appKey: string, name: string, b: { env: string; mode: string; value?: string; template_id?: string }) =>
    idPut<O>(`/v1/apps/${enc(appKey)}/secrets/${enc(name)}`, { project_id: projectId, ...b }),
  deleteSecret: (projectId: string, appKey: string, name: string, env: string) =>
    idDelete<O>(`/v1/apps/${enc(appKey)}/secrets/${enc(name)}?${q({ project_id: projectId, env })}`),
};

/** An app's key from any of its handles (a run id, its name), for an API older than the factory
 *  rows' `app_key`: the links route resolves the same way the engine files apps. */
export async function resolveAppKey(projectId: string, handle: string): Promise<string | null> {
  try {
    const r = await idGet<{ app_key?: string }>(`/v1/apps/${enc(handle)}/links?${q({ project_id: projectId })}`);
    return typeof r.app_key === "string" && r.app_key ? r.app_key : null;
  } catch {
    return null;
  }
}

/** Toolchains this install has (GET /v1/templates' `toolchains`), or null when it cannot say —
 *  then nothing is greyed, and the API still refuses a stack it cannot build. */
export async function installedStacks(): Promise<Set<string> | null> {
  try {
    const list = arr(obj(await idGet("/v1/templates?status=active")).toolchains);
    if (!list.length) return null;
    return new Set(list.filter((t) => obj(t).installed === true).map((t) => str(obj(t).name)));
  } catch {
    return null;
  }
}
