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

/** Setup's old `step=` names, and the board row each one is. */
const SETUP_STEPS: Record<string, string> = { github: "repository", model: "models" };

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

/** The v2 routes that became console areas, by path. */
const V2_ROUTES: Record<string, Record<string, string>> = {
  // v2 Settings (roles, "what this deployment does not have") is Repo and models now.
  "/v2/settings": { area: "setup" },
  "/v2/boss": { area: "delivery_workflows", tab: "library" },
  "/v2/factory": { area: "workforce" },
  "/v2/org": { area: "workforce", view: "team" },
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
