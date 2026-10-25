/**
 * Where a link to a retired console surface lands now.
 *
 * Workflow Studio folded four places into two: Agent Studio and Team structure left the nav,
 * the Workflow area's five tabs became Runs and Library, and three v2 screens became links
 * back into the console. Every bookmark, chat answer and stored notification that names one of
 * the old surfaces must still open the thing it meant. ONE resolver says where each goes, and
 * every door that reads a URL asks it: the server render (page.tsx), the shell's in-console
 * link handler and its URL sync (ConsoleShell), and the v2 route pages. A second copy of this
 * table somewhere else is how a door ends up on a blank area.
 */

import { AREA_IDS, isAreaId, type AreaId } from "../lib/area-ids";

/** Parameters only the retired surfaces read. Dropped when the link is rewritten. */
const STUDIO_ONLY = ["studio_mode", "mode", "studio_workflow", "workflow", "studio_role"];

/** The Workflow area's old tab ids, and what each became. */
const OLD_WORKFLOW_TABS: Record<string, Record<string, string>> = {
  running: { tab: "runs" },
  yours: { tab: "library" },
  catalogue: { tab: "library" },
  design: { tab: "library", wf: "new" },
  steps: { tab: "library", lib: "steps" },
};

/** System Configuration's old `infra_view=` sub-views, and the Infra › Cloud sub-view each became
 *  (infra-observability spec §4.1). `mcp` left the area altogether: Skills & MCP owns servers. */
const OLD_INFRA_VIEWS: Record<string, string> = {
  connections: "accounts", resources: "resources", scaling: "capacity", finops: "spend",
};
/** Parameters only the retired Infra/Observability surfaces read. */
const INFRA_ONLY = ["infra_view", "provider_account_id"];

/** Setup's old `step=` names, and the board row each one is. */
const SETUP_STEPS: Record<string, string> = { github: "repository", model: "models" };

/** A rail label as typed: case, spaces, hyphens, underscores and `&` do not matter. */
export function railLabelKey(value: string): string {
  return value.trim().toLowerCase().replace(/&/g, " ").replace(/[\s_-]+/g, "_").replace(/^_+|_+$/g, "");
}

/** Rail labels (railLabelKey form) whose key is not already the area id. */
export const RAIL_LABEL_AREAS: Readonly<Record<string, AreaId>> = {
  command_center: "overview",
  factory: "code_tasks",
  agent_studio: "workforce",
  graph: "knowledge",
  knowledge_graph: "knowledge",
  approvals: "decisions",
  activity_log: "activity",
  research: "discovery",
  panels: "meetings",
  boss_agent: "agent",
  mission_dashboard: "dashboard",
  task_board: "work",
  jobs: "work",
  work_allocation: "portfolio",
  agent_processes: "processes",
  task_shepherd: "shepherd",
  ai_sre: "ai-sre",
  evidence: "reports",
  qa: "reports",
  repo_and_models: "setup",
  skills_mcp: "skills",
  skills_and_mcp: "skills",
  communications: "comms",
  operations: "ops",
  access_and_packs: "org",
  identity: "enterprise",
};

function copyWithout(params: URLSearchParams, drop: string[]): URLSearchParams {
  const next = new URLSearchParams();
  params.forEach((value, key) => {
    if (!drop.includes(key)) next.append(key, value);
  });
  return next;
}

/**
 * The parameters a legacy console URL should carry instead, or null when it is current.
 *
 * Everything the old link carried that is not specific to the retired surface (the org, the
 * project, the actor, a run id) is kept, so following an old link never signs anybody out.
 */
export function resolveLegacyArea(params: URLSearchParams): URLSearchParams | null {
  const area = params.get("area");

  if (area === "studio") {
    const next = copyWithout(params, STUDIO_ONLY);
    const run = params.get("wfr");
    const workflow = params.get("studio_workflow") || params.get("workflow");
    const flows = params.get("studio_mode") === "workflows" || params.get("mode") === "workflows";
    const role = params.get("studio_role");
    if (run) {
      next.set("area", "delivery_workflows");
    } else if (workflow || flows) {
      next.set("area", "delivery_workflows");
      if (workflow) next.set("wf", workflow);
      else next.set("tab", "library");
    } else {
      next.set("area", "workforce");
      if (role) next.set("role", role);
    }
    return next;
  }

  // GOVERN HAS ONE ID, `budget`; "govern" is what people type and what the spec, the agents
  // and the chat call it. A link naming it lands on the same screen, keeping its tab and focus.
  if (area === "govern") {
    const next = copyWithout(params, []);
    next.set("area", "budget");
    return next;
  }

  // THE CODING PIPELINE PAGE FOLDED INTO MODEL ROUTING (docs/product/MODEL_ROUTER.md): its
  // profiles are the floor presets there, its default is the preset's picker.
  if (area === "pipeline") {
    const next = copyWithout(params, []);
    next.set("area", "setup");
    next.set("focus", "routing");
    return next;
  }

  if (area === "org_chart") {
    const next = copyWithout(params, []);
    next.set("area", "workforce");
    next.set("view", "team");
    return next;
  }

  // REPO AND MODELS LANDS ON THE ROW. `step=` was read by nothing, so every deep link into
  // setup landed at the top of the page; the board reads `focus=`.
  if (area === "setup" && params.get("step")) {
    const focus = SETUP_STEPS[params.get("step") ?? ""];
    const next = copyWithout(params, ["step"]);
    if (focus && !next.get("focus")) next.set("focus", focus);
    return next;
  }

  if (area === "capabilities") {
    // Skills & MCP folded Capabilities in (skills-mcp spec §5.1): its MCP servers are managed in
    // the Library's MCP list, the one place servers are managed.
    const next = copyWithout(params, []);
    next.set("area", "skills");
    next.set("tab", "library");
    next.set("lib", "mcp");
    return next;
  }

  // INFRA AND OBSERVABILITY (infra-observability spec §4.1). The rail's "Infra" was area
  // `delivery` and its "Observability" was `monitoring`, while `?area=infra` was System
  // Configuration. The ids say what the screens are now; every old link lands on its tab.
  if (area === "delivery") {
    const next = copyWithout(params, []);
    next.set("area", "infra");
    if (!next.get("view")) next.set("view", "fleet");
    return next;
  }

  if (area === "monitoring") {
    // `watch=<app>|<env>` (the workflow "Set up monitoring" step) is kept: Observability › Apps
    // resolves it to that app's drawer once its rows are in hand, or to a Find for the app.
    const next = copyWithout(params, []);
    next.set("area", "observability");
    if (!next.get("view")) next.set("view", "apps");
    return next;
  }

  if (area === "infra" && params.get("infra_view") === "mcp") {
    const next = copyWithout(params, INFRA_ONLY);
    next.set("area", "skills");
    next.set("tab", "library");
    next.set("lib", "mcp");
    return next;
  }

  if (area === "infra" && (params.get("infra_view") !== null || params.get("provider_account_id") !== null)) {
    const next = copyWithout(params, INFRA_ONLY);
    next.set("view", "cloud");
    const sub = OLD_INFRA_VIEWS[params.get("infra_view") ?? ""];
    if (sub) next.set("cloud", sub);
    const account = params.get("provider_account_id");
    if (account) {
      next.set("account", account);
      if (!next.get("cloud")) next.set("cloud", "accounts");
    }
    return next;
  }

  // Govern links to `?area=observability&tab=models` (activity-govern spec): `view=` is ours.
  if (area === "observability" && params.get("tab") !== null && params.get("view") === null) {
    const next = copyWithout(params, ["tab"]);
    next.set("view", params.get("tab") ?? "apps");
    // Govern's spend-vs-cap link means the month: with no window named, Models opens on 30d.
    if (params.get("tab") === "models" && !next.get("win")) next.set("win", "30d");
    return next;
  }

  // `area=workflows` is the name the compose links use (lib/compose-links.ts on the Command
  // Center: "Add a feature" on an app card). It is the Workflows area; everything else the link
  // carries — `compose`, `app`, `preview` — is the composer's to read.
  if (area === "workflows") {
    const next = copyWithout(params, []);
    next.set("area", "delivery_workflows");
    return next;
  }

  // AN AREA NAMED BY ITS RAIL LABEL (round 3, W3). `?area=factory` is what a person, an agent or
  // a walk types for the rail's "Factory", whose id is `code_tasks`; it fell back to the Command
  // Center, which has no PR column, so "the Factory shows the PR" could never be seen. Every rail
  // label (ConsoleShell AREAS, the Community copy in lib/edition.ts) lands on its area —
  // tests/an-area-opens-by-its-rail-label.test.ts keeps this table whole. An id is never re-read.
  if (area !== null && !isAreaId(area)) {
    // The table for labels that are not their id; otherwise the label's own key ("Infra" → infra,
    // "Govern" → govern, which the next pass resolves like any alias).
    const key = railLabelKey(area);
    const id = RAIL_LABEL_AREAS[key] ?? (key && key !== area ? key : undefined);
    if (id) {
      const next = copyWithout(params, []);
      next.set("area", id);
      return next;
    }
  }

  if (area === "delivery_workflows") {
    const tab = params.get("tab") ?? "";
    const becomes = OLD_WORKFLOW_TABS[tab];
    if (!becomes) return null;
    const next = copyWithout(params, ["tab"]);
    for (const [key, value] of Object.entries(becomes)) {
      // A link that already names a workflow keeps it: `tab=design&wf=K` opens K, not a new one.
      if (key === "wf" && next.get("wf")) continue;
      next.set(key, value);
    }
    return next;
  }

  return null;
}

/** Areas the console renders: every known id that is not itself an alias (`delivery`,
 *  `monitoring`, `capabilities` stay in AREA_IDS for old links and are always rewritten). */
const RENDERED = new Set<string>(
  AREA_IDS.filter((a) => resolveLegacyArea(new URLSearchParams(`area=${a}`)) === null));

/**
 * THE PARAMETERS A CONSOLE URL MEANS, for every door that reads one (the middleware's 307, the
 * page render, the shell's URL sync). Aliases are resolved (more than once: `govern` → `budget`
 * is one step, an old tab inside it another), and an area nobody renders falls back to the
 * Command Center rather than to a blank screen. `changed` says whether the URL should be
 * rewritten to the result.
 */
export function consoleParams(params: URLSearchParams): { params: URLSearchParams; changed: boolean } {
  let current = params;
  let changed = false;
  for (let i = 0; i < 4; i += 1) {
    const next = resolveLegacyArea(current);
    if (!next) break;
    current = next;
    changed = true;
  }
  const area = current.get("area");
  if (area !== null && area.trim() && !RENDERED.has(area.trim())) {
    current = copyWithout(current, []);
    current.set("area", "overview");
    changed = true;
  }
  return { params: current, changed };
}

/** The v2 routes that became console areas, by path. */
const V2_ROUTES: Record<string, Record<string, string>> = {
  // v2 Settings (roles, "what this deployment does not have") is Repo and models now.
  "/v2/settings": { area: "setup" },
  "/v2/boss": { area: "delivery_workflows", tab: "library" },
  "/v2/factory": { area: "workforce" },
  "/v2/org": { area: "workforce", view: "team" },
  // v2 "Infra & Models" (Hosts, Apps, Models this month, Integrations) is Infra now.
  "/v2/infra": { area: "infra" },
};

/** Who you are travels with you: the console gates on these. */
const IDENTITY = ["org", "org_id", "project_id", "actor"];

/**
 * Where a retired v2 route sends its visitor: a console href, or null when the path is not
 * one of them. Only identity crosses over; v2's own parameters mean nothing to the console.
 */
export function resolveLegacyRoute(pathname: string, params: URLSearchParams): string | null {
  const target = V2_ROUTES[pathname.replace(/\/+$/, "")];
  if (!target) return null;
  const next = new URLSearchParams();
  for (const key of IDENTITY) {
    const value = params.get(key);
    if (value) next.set(key, value);
  }
  for (const [key, value] of Object.entries(target)) next.set(key, value);
  return `/?${next.toString()}`;
}

/** Search params as Next hands them to a page, flattened to the first value of each. */
export function searchParamsFrom(
  record: Record<string, string | string[] | undefined> | undefined,
): URLSearchParams {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(record ?? {})) {
    const one = Array.isArray(value) ? value[0] : value;
    if (typeof one === "string") params.set(key, one);
  }
  return params;
}
