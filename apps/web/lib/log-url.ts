/**
 * The Log's URL: ONE reader, ONE writer, ONE link builder (activity-govern spec §4.1, RC10).
 *
 * Six screens link into the Activity log, and they used two link builders and two names for the
 * same scope: the step panel sent `wfr=`, Agent Studio's river sent `workflow_run_id=` — which
 * the Log never read, so "Open in Activity log" from the river landed on the whole project.
 * Every inbound link is now built by `logHref()`, every read goes through `readLogParams()`, and
 * the legacy spellings (`workflow_run_id`, `run_id`, `step_id`, and the Infra/Observability
 * contract's `source`/`subject`/`since`) are read once and rewritten.
 *
 * The URL carries every filter, so any view of the log can be shared or reloaded as it is.
 */
import { consoleHref } from "../app/url-intent";

export const RANGES = ["15m", "1h", "24h", "7d", "30d"] as const;
export type LogRange = (typeof RANGES)[number];
export const DEFAULT_RANGE: LogRange = "24h";

/** Query-bar keys, in the order the bar suggests them. */
export const TOKEN_KEYS = ["level", "src", "agent", "run", "wfr", "step", "act", "actor", "status",
                           "kind", "svc", "subject", "entity"] as const;
export type LogTokenKey = (typeof TOKEN_KEYS)[number];

/** Keys the server can EXCLUDE (`-level:verbose`); every other key refuses a `-`. */
export const NEGATABLE: ReadonlySet<LogTokenKey> = new Set(["level", "src", "agent"]);
/** Keys that take one value, not a set. */
const SINGLE: ReadonlySet<LogTokenKey> = new Set(["run", "wfr", "step", "act", "actor", "status",
                                                  "svc", "subject", "entity"]);
/** Scope keys: a change to these is a new place (pushState), so Back leaves it. */
export const SCOPE_KEYS: ReadonlySet<LogTokenKey> = new Set(["run", "wfr", "step", "act"]);

export type LogToken = { key: LogTokenKey; value: string; negate?: boolean };

export type LogState = {
  tokens: LogToken[];
  q: string;
  range: LogRange | null;
  from: string | null;
  to: string | null;
  row: string | null;
  group: "run" | null;
  /** Verbose lines are hidden unless asked for (`verbose=1`). */
  showVerbose: boolean;
  /** Display hints carried by inbound links until the server's labels arrive. */
  names: { step?: string; run?: string };
};

export type LogView = { key: string; label: string; tip: string; tokens: LogToken[] };

/** Built-in views: each is only a set of tokens; selected while the tokens equal it. */
export const VIEWS: LogView[] = [
  { key: "all", label: "All", tip: "Every line in range", tokens: [] },
  { key: "errors", label: "Errors", tip: "Only lines at Error", tokens: [{ key: "level", value: "error" }] },
  { key: "model-calls", label: "Model calls", tip: "One line per model call", tokens: [{ key: "src", value: "llm_call" }] },
  { key: "deploys", label: "Deploys", tip: "What was deployed, and how", tokens: [{ key: "src", value: "deployment" }] },
  {
    key: "govern-changes", label: "Govern changes", tip: "Every limit someone changed",
    tokens: ["budget.intensity_set", "budget.monthly_cap_set", "settings.changed", "token_policy.changed"]
      .map((value) => ({ key: "kind" as const, value })),
  },
  {
    key: "refusals", label: "Refusals", tip: "Work a limit stopped",
    tokens: ["budget.refused", "budget.reservation_denied"].map((value) => ({ key: "kind" as const, value })),
  },
];

const PARAM_OF: Record<LogTokenKey, string> = {
  level: "level", src: "src", agent: "agent", run: "run", wfr: "wfr", step: "step", act: "act",
  actor: "actor", status: "status", kind: "kind", svc: "svc", subject: "subject", entity: "entity",
};

/** Legacy and sibling-contract spellings, read once and rewritten. */
const ALIASES: Record<string, LogTokenKey> = {
  workflow_run_id: "wfr", run_id: "run", step_id: "step", source: "src", service: "svc",
  entity_type: "entity",
};

/** Every parameter this module owns on the console URL (written, or cleared, by `writeLogParams`). */
export const LOG_PARAMS = [...Object.values(PARAM_OF), ...Object.keys(ALIASES), "q", "range", "from",
                           "to", "since", "until", "row", "group", "verbose", "view", "step_name",
                           "run_name"] as const;

function split(value: string | null): string[] {
  return (value ?? "").split(",").map((v) => v.trim()).filter(Boolean);
}

function isRange(value: string | null): value is LogRange {
  return !!value && (RANGES as readonly string[]).includes(value);
}

/** The Log's state from a query string. Never throws; unknown values are dropped. */
export function readLogParams(search: string): LogState {
  const params = new URLSearchParams(search);
  const tokens: LogToken[] = [];
  const add = (key: LogTokenKey, raw: string) => {
    const negate = raw.startsWith("-") && NEGATABLE.has(key);
    const value = negate ? raw.slice(1) : raw;
    if (!value) return;
    if (SINGLE.has(key)) {
      const at = tokens.findIndex((t) => t.key === key);
      if (at >= 0) tokens.splice(at, 1);
    }
    if (!tokens.some((t) => t.key === key && t.value === value && !!t.negate === negate)) {
      tokens.push(negate ? { key, value, negate } : { key, value });
    }
  };
  for (const key of TOKEN_KEYS) for (const v of split(params.get(PARAM_OF[key]))) add(key, v);
  for (const [alias, key] of Object.entries(ALIASES)) for (const v of split(params.get(alias))) add(key, v);
  const view = VIEWS.find((v) => v.key === params.get("view"));
  if (view && !tokens.length) tokens.push(...view.tokens);
  const range = params.get("range");
  const from = params.get("from") || params.get("since");
  const to = params.get("to") || params.get("until");
  const names: LogState["names"] = {};
  if (params.get("step_name")) names.step = params.get("step_name")!;
  if (params.get("run_name")) names.run = params.get("run_name")!;
  return {
    tokens,
    q: params.get("q") ?? "",
    range: from ? null : (isRange(range) ? range : DEFAULT_RANGE),
    from: from || null,
    to: to || null,
    row: params.get("row") || null,
    group: params.get("group") === "run" ? "run" : null,
    showVerbose: params.get("verbose") === "1",
    names,
  };
}

/** The query string for a Log state, keeping everything else on the URL (org, area…). */
export function writeLogParams(state: LogState, currentSearch: string): string {
  const params = new URLSearchParams(currentSearch);
  for (const key of LOG_PARAMS) params.delete(key);
  params.set("area", "activity");
  const byKey = new Map<string, string[]>();
  for (const t of state.tokens) {
    const p = PARAM_OF[t.key];
    byKey.set(p, [...(byKey.get(p) ?? []), `${t.negate ? "-" : ""}${t.value}`]);
  }
  for (const [p, values] of byKey) params.set(p, values.join(","));
  if (state.q) params.set("q", state.q);
  if (state.from) {
    params.set("from", state.from);
    if (state.to) params.set("to", state.to);
  } else if (state.range && state.range !== DEFAULT_RANGE) {
    params.set("range", state.range);
  }
  if (state.row) params.set("row", state.row);
  if (state.group) params.set("group", state.group);
  if (state.showVerbose) params.set("verbose", "1");
  if (state.names.step && state.tokens.some((t) => t.key === "step")) params.set("step_name", state.names.step);
  if (state.names.run && state.tokens.some((t) => t.key === "wfr" || t.key === "run")) {
    params.set("run_name", state.names.run);
  }
  const rest = params.toString();
  return rest ? `?${rest}` : "";
}

/** What an inbound link may scope the Log to. */
export type LogScope = {
  run?: string | null; wfr?: string | null; step?: string | null; act?: string | null;
  step_name?: string | null; run_name?: string | null;
  src?: string | null; subject?: string | null; since?: string | null;
  kind?: string | null; level?: string | null; view?: string | null; row?: string | null;
  range?: LogRange | null;
};

/**
 * THE way to link to the Log. Keeps who you are (org), names the scope with the Log's own
 * parameter names, and carries display names so the scope reads before the server answers.
 */
export function logHref(scope: LogScope, search: string = typeof window === "undefined" ? "" : window.location.search): string {
  return consoleHref(search, {
    area: "activity",
    run: scope.run || null, wfr: scope.wfr || null, step: scope.wfr ? scope.step || null : null,
    act: scope.act || null,
    step_name: scope.wfr && scope.step ? scope.step_name || null : null,
    run_name: scope.run_name || null,
    src: scope.src || null, subject: scope.subject || null, from: scope.since || null,
    kind: scope.kind || null, level: scope.level || null, view: scope.view || null,
    row: scope.row || null, range: scope.range || null,
  });
}

/** The API's parameters for a Log state (shared by the page, the pulse, the facets, export). */
export function apiParams(state: LogState, skip: ReadonlySet<LogTokenKey> = new Set()): Record<string, string> {
  const out: Record<string, string> = {};
  const join = (key: LogTokenKey, negate: boolean) =>
    state.tokens.filter((t) => t.key === key && !!t.negate === negate && !skip.has(key)).map((t) => t.value).join(",");
  const put = (name: string, value: string) => { if (value) out[name] = value; };
  put("level", join("level", false));
  put("exclude_level", join("level", true));
  put("source", join("src", false));
  put("exclude_source", join("src", true));
  put("agent", join("agent", false));
  put("exclude_agent", join("agent", true));
  put("run_id", join("run", false));
  put("workflow_run_id", join("wfr", false));
  if (out.workflow_run_id) put("step_id", join("step", false));
  put("act", join("act", false));
  put("actor", join("actor", false));
  put("status", join("status", false));
  put("kind", join("kind", false));
  put("service", join("svc", false));
  put("subject", join("subject", false));
  put("entity_type", join("entity", false));
  if (state.q.trim()) out.q = state.q.trim();
  if (state.from) {
    out.since = state.from;
    if (state.to) out.until = state.to;
  } else {
    out.range = state.range ?? DEFAULT_RANGE;
  }
  if (!state.showVerbose && !state.tokens.some((t) => t.key === "level" && !t.negate)) out.hide_verbose = "1";
  return out;
}

/** A key the bar does not let you exclude says so, in the bar's own words. */
export const CANT_EXCLUDE = "Can't exclude this one";

/** Turn typed text into a token, or explain why not. `key:value`, `-key:value`, or a pasted id. */
export function parseToken(text: string): { token: LogToken } | { error: string } | null {
  const raw = text.trim();
  if (!raw) return null;
  const pasted = tokenForId(raw);
  if (pasted) return { token: pasted };
  const m = raw.match(/^(-?)([a-z]+):("?)(.+?)\3$/i);
  if (!m) return null;
  const key = m[2].toLowerCase() as LogTokenKey;
  if (!(TOKEN_KEYS as readonly string[]).includes(key)) return null;
  const negate = m[1] === "-";
  if (negate && !NEGATABLE.has(key)) return { error: CANT_EXCLUDE };
  const value = m[4].trim();
  if (!value) return null;
  return { token: negate ? { key, value, negate } : { key, value } };
}

/** A pasted id becomes its scope: `crun_…` a run, `wfr_…` a workflow run, `act_…` a work item. */
export function tokenForId(text: string): LogToken | null {
  const id = text.trim();
  if (/^crun_[A-Za-z0-9_-]+$/.test(id)) return { key: "run", value: id };
  if (/^wfr_[A-Za-z0-9_-]+$/.test(id)) return { key: "wfr", value: id };
  if (/^act_[A-Za-z0-9_-]+$/.test(id)) return { key: "act", value: id };
  return null;
}

export function sameTokens(a: LogToken[], b: LogToken[]): boolean {
  const key = (t: LogToken) => `${t.negate ? "-" : ""}${t.key}:${t.value}`;
  const left = a.map(key).sort();
  const right = b.map(key).sort();
  return left.length === right.length && left.every((k, i) => k === right[i]);
}

/** The built-in view these tokens are, or null. */
export function viewOf(tokens: LogToken[]): LogView | null {
  return VIEWS.find((v) => sameTokens(v.tokens, tokens)) ?? null;
}

/** Add or remove one token (a rail click, a `+`/`−` on a fact), keeping single-value keys single. */
export function toggleToken(tokens: LogToken[], token: LogToken): LogToken[] {
  const same = (t: LogToken) => t.key === token.key && t.value === token.value && !!t.negate === !!token.negate;
  if (tokens.some(same)) return tokens.filter((t) => !same(t));
  const rest = SINGLE.has(token.key) ? tokens.filter((t) => t.key !== token.key) : tokens;
  // A step only means something inside its workflow run.
  return [...rest.filter((t) => !(t.key === token.key && t.value === token.value)), token];
}

export function removeToken(tokens: LogToken[], token: LogToken): LogToken[] {
  const out = tokens.filter((t) => !(t.key === token.key && t.value === token.value && !!t.negate === !!token.negate));
  // Removing the workflow run removes its step too: `step` is only unique inside its run.
  return token.key === "wfr" ? out.filter((t) => t.key !== "step") : out;
}
