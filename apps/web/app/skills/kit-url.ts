/**
 * Skills & MCP's place in the URL (docs/design/SKILLS_AND_MCP_HOME.md, "Old links").
 *
 * The page opens on one home with two sections, MCP servers and Skills. `&tab=who|templates|rules`
 * opens those views. `&view=all` shows every skill instead of the abilities by outcome.
 * `&section=mcp` scrolls to the servers. `&add=mcp|skill` opens that add sheet.
 * `&ability=<key>`, `&skill=<key>`, `&mcp=<id>` open a drawer. `&role=<key>` focuses Who can.
 * `&setup=1` forces the Essentials strip open.
 *
 * OLD LINKS STILL LAND. `&tab=kit` is the home. `&tab=library` is the home with All skills
 * (`&lib=mcp`: the MCP section). `&lib=mcp&add=1` (the old agent-page link) opens Add MCP server.
 * The URL is read once on load and written with replaceState, so a reload lands on the same view.
 */
export type KitTab = "home" | "who" | "templates" | "rules";
export type SkillsView = "outcome" | "all";
export type KitParams = {
  tab: KitTab; view: SkillsView; section: "mcp" | "skills" | null; add: "mcp" | "skill" | null;
  ability: string | null; skill: string | null; mcp: string | null; role: string | null; setup: boolean;
};

/** Sent by search (⌘K) and other screens to open an add sheet on a home already mounted. */
export const KIT_ADD_EVENT = "boss:kit-add";

export function readKitParams(search?: string): KitParams {
  const p = new URLSearchParams(search ?? (typeof window !== "undefined" ? window.location.search : ""));
  const tab = p.get("tab");
  const lib = p.get("lib");
  const ability = p.get("ability");
  const add = p.get("add");
  const mcpAsked = lib === "mcp" || Boolean(p.get("mcp")) || p.get("section") === "mcp";
  return {
    tab: tab === "who" || tab === "templates" || tab === "rules" ? tab : "home",
    // The old Library listed every skill: a link into it (or to one skill) shows All skills.
    view: p.get("view") === "all" || p.get("skill") || (tab === "library" && lib !== "mcp") || lib === "skills"
      ? "all" : "outcome",
    section: mcpAsked ? "mcp" : p.get("section") === "skills" ? "skills" : null,
    add: add === "mcp" || (add === "1" && lib === "mcp") ? "mcp" : add === "skill" ? "skill" : null,
    ability: ability && /^[a-z][a-z0-9_]{1,40}$/.test(ability) ? ability : null,
    skill: p.get("skill"), mcp: p.get("mcp"), role: p.get("role"), setup: p.get("setup") === "1",
  };
}

type Key = "tab" | "view" | "section" | "add" | "lib" | "ability" | "skill" | "mcp" | "role" | "setup";

export function writeKitParams(change: Partial<Record<Key, string | null>>): void {
  if (typeof window === "undefined") return;
  try {
    const url = new URL(window.location.href);
    for (const [key, value] of Object.entries(change)) {
      if (value === null || value === undefined || value === "") url.searchParams.delete(key);
      else url.searchParams.set(key, value);
    }
    window.history.replaceState(null, "", `${url.pathname}${url.search}`);
  } catch { /* a sandboxed frame: the view still works, the URL just does not follow */ }
}

/** The link to open an add sheet from anywhere (a page load lands on it). */
export const addHref = (kind: "mcp" | "skill") => `?area=skills&add=${kind}`;
